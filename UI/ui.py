from __future__ import annotations

import argparse
import queue
import csv
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

from AutoR3D.axis import (
    AxisScore,
    global_axis_grid,
    q_to_spherical_angles,
    refine_rotation_axis,
    score_projection_axis_with_map,
)
from AutoR3D.cred2 import CRED2Parameters
from AutoR3D.cred2 import find_cred2_file, parse_cred2_parameters
from .fonts import select_ui_fonts
from AutoR3D.images import FrameRecord, FrameStats, discover_frames, extract_diffraction_points, inspect_frame, load_image
from AutoR3D.observations import DEFAULT_FRAME_WORKERS, SparsePixels
from AutoR3D.slices import (
    FAMILIES as SLICE_FAMILIES,
    SLICE_HEADER_HEIGHT,
    SliceSpec,
    build_slice,
    render_slice_image,
    save_slice_npz,
    slice_title,
)
from AutoR3D.xds import XdsReference, load_xds_reference
from AutoR3D.reference import load_reference
from AutoDials.results import reflection_metadata
from UI.autodials import AutoDialsTab
from config.autocrys_common import external_target_path
from .structure_viewer import StructureViewer


AUTO_SOLVE_OLEX2_DEFAULT = Path("/mnt/c/Program Files/Olex2-1.5/olex2.exe")

UI_BG = "#0c121c"
UI_PANEL = "#141d2b"
UI_PANEL_ALT = "#1b2739"
UI_TEXT = "#e7edf7"
UI_MUTED = "#a0aec4"
UI_ACCENT = "#5aa8ff"
UI_ACCENT_ACTIVE = "#83c2ff"
UI_BORDER = "#2a3b55"
UI_FONT_SIZE = 13
UI_HEADING_SIZE = 14
INDEXED_POINT_TOLERANCE_RLU = 0.1
XDS_CLUSTER_COLORS = (
    "#5aa8ff",
    "#6ad087",
    "#f4c95d",
    "#ff8d8d",
    "#c88bff",
    "#7ee0d8",
    "#ffb86f",
    "#9fb2ff",
)
IMPORTANT_CONSOLE_LINE = re.compile(
    r"(?:\b(?:error|failed|failure|warning|warn|complete|completed|finished|success|skipped|exit)\b|"
    r"错误|失败|警告|完成|跳过)",
    re.IGNORECASE,
)


def _normalize_path(text: str) -> Path:
    value = text.strip().strip('"')
    if not value:
        return Path()
    path = Path(value)
    if path.exists():
        return path
    if value.startswith("/") and not value.startswith("//"):
        wsl_path = Path(r"\\wsl.localhost\Ubuntu" + value.replace("/", "\\"))
        if wsl_path.exists():
            return wsl_path
    return path


def _autocrys_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _path_text(path: Path) -> str:
    return str(path)


def _display_resolution(value: object) -> str:
    text = str(value or "NA").strip()
    if not text or text.upper() == "NA":
        return "NA"
    try:
        number = float(text)
    except ValueError:
        return text
    rendered = f"{number:.2f}".rstrip("0").rstrip(".")
    return rendered if "." in rendered else f"{rendered}.0"


def _split_names(text: str) -> list[str]:
    cleaned = text.replace(",", " ").replace(";", " ")
    return [part.strip() for part in cleaned.split() if part.strip()]


def _float_from_var(var: tk.StringVar, label: str) -> float:
    try:
        return float(var.get())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number.") from exc


def _int_from_var(var: tk.StringVar, label: str) -> int:
    value = _float_from_var(var, label)
    if value <= 0:
        raise ValueError(f"{label} must be positive.")
    return int(round(value))


def _integer_from_var(var: tk.StringVar, label: str) -> int:
    value = _float_from_var(var, label)
    rounded = int(round(value))
    if abs(value - rounded) > 1e-9:
        raise ValueError(f"{label} must be an integer.")
    return rounded


def _optional_float_from_var(var: tk.StringVar, label: str) -> float | None:
    text = var.get().strip().lower()
    if not text or text == "auto":
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, blank, or auto.") from exc
    if value < 0:
        raise ValueError(f"{label} must be zero or positive.")
    return value


def _parse_xds_header_fields(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    sg = None
    cell = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("!SPACE_GROUP_NUMBER="):
            sg = line.split("=", 1)[1].strip()
        elif line.startswith("!UNIT_CELL_CONSTANTS="):
            cell = line.split("=", 1)[1].strip()
    return sg, cell


def _image_to_uint8(
    array: np.ndarray,
    brightness: float,
    contrast: float,
    gamma: float,
) -> np.ndarray:
    values = array.astype(np.float32, copy=False)
    if values.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    positive = values[values > 0]
    if positive.size:
        low = float(np.percentile(positive, 1.0))
        high = float(np.percentile(positive, 99.5))
    else:
        low = float(np.min(values))
        high = float(np.max(values))
    if high <= low:
        high = low + 1.0
    view = np.clip((values - low) / (high - low), 0.0, 1.0)
    view = np.clip((view - 0.5) * max(contrast, 0.01) + 0.5, 0.0, 1.0)
    view = np.power(view, 1.0 / max(gamma, 0.01))
    view = np.clip(view * max(brightness, 0.0), 0.0, 1.0)
    return (view * 255.0).astype(np.uint8)


class AutoR3DUI:
    def __init__(
        self,
        root: tk.Tk,
        dataset_dir: str | None = None,
        frames_dir: str = "diff",
        ai_assistant: bool | None = None,
        console_verbose: bool = False,
    ) -> None:
        from .ai_assistant import Config

        self.root = root
        self._after_ids: set[str] = set()
        self._destroyed = False
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self._ai_config = Config()
        self.ai_assistant_enabled = (
            self._ai_config.assistant_enabled if ai_assistant is None else bool(ai_assistant)
        )
        self.console_verbose = bool(console_verbose)
        self.root.title("AutoCrys + AI" if self.ai_assistant_enabled else "AutoCrys")
        # Sized for the 1600x1000 WSLg desktop while leaving room for window
        # decorations.  The larger typography otherwise makes the dense XDS
        # controls unnecessarily cramped at the old 1440px width.
        self.root.geometry("1560x930")
        self.root.minsize(1280, 760)
        self._ui_font_family, self._ui_mono_family = select_ui_fonts(self.root)
        self._apply_theme()

        self.autocrys_root = _autocrys_root()
        self.data_root = self.autocrys_root / "Data"
        self.dataset_var = tk.StringVar(value=dataset_dir or str(self.data_root))
        self.frames_dir = frames_dir
        self.center_x_var = tk.StringVar(value="258")
        self.center_y_var = tk.StringVar(value="258")
        self.mask_radius_var = tk.StringVar(value="15")
        self.mask_edge_guard_var = tk.StringVar(value="5")
        self.spot_size_var = tk.StringVar(value="12")
        self.threshold_var = tk.StringVar(value="30")
        self.output_var = tk.StringVar(value=str(self.data_root / "AutoR3D"))
        self._output_path_is_custom = False
        self.voxel_size_var = tk.StringVar(value="0.01")
        self.max_peaks_var = tk.StringVar(value="2000")
        self.reconstruction_info_var = tk.StringVar(
            value="Import a dataset containing cRED2 parameters and diff/frame_*.tif first."
        )
        self.brightness_var = tk.DoubleVar(value=1.0)
        self.contrast_var = tk.DoubleVar(value=1.0)
        self.gamma_var = tk.DoubleVar(value=1.0)
        self.frame_index_var = tk.DoubleVar(value=0.0)
        self.peak_progress_var = tk.DoubleVar(value=0.0)
        self.axis_var = tk.StringVar(value="142.0")
        self.axis_info_var = tk.StringVar(value="Import a dataset with XDS output to load the refined rotation axis.")
        self.xds_reference: XdsReference | None = None
        self.indexed_observations_path: Path | None = None
        self._indexed_hkl: np.ndarray | None = None
        self._indexed_intensity: np.ndarray | None = None
        self.slice_family_var = tk.StringVar(value="hk0")
        self.slice_layer_var = tk.StringVar(value="0")
        self.slice_thickness_var = tk.StringVar(value="0.20")
        self.slice_step_var = tk.StringVar(value="0.05")
        self.slice_cell_var = tk.BooleanVar(value=True)
        self.slice_threshold_var = tk.DoubleVar(value=0.0)
        self.slice_threshold_label_var = tk.StringVar(value="0%")
        self.slice_info_var = tk.StringVar(value="Run Reconstruction on a dataset with XDS output to preview hkl slices.")
        self._slice_photo: ImageTk.PhotoImage | None = None
        self._slice_data = None
        self.slice_zoom = 1.0
        self.embedded_axis_labels: tuple[str, str, str] = ("qx", "qy", "qz")
        self.embedded_space = "q"
        self.status_var = tk.StringVar(value="Ready")
        self.frame_info_var = tk.StringVar(value="No dataset loaded")
        self.xds_root_var = tk.StringVar(value=str(self.autocrys_root / "Data"))
        self.xds_datasets_var = tk.StringVar(value="")
        self.xds_mode_var = tk.StringVar(value="list")
        self.xds_all_var = tk.BooleanVar(value=False)
        self.xds_dry_run_var = tk.BooleanVar(value=False)
        self.xds_json_var = tk.BooleanVar(value=False)
        self.xds_cell_var = tk.StringVar(value="")
        self.xds_sg_var = tk.StringVar(value="")
        self.xds_resolution_var = tk.StringVar(value="")
        self.xds_merge_name_var = tk.StringVar(value="")
        self.xds_cluster_method_var = tk.StringVar(value="both")
        self.xds_progress_var = tk.DoubleVar(value=0.0)
        self.xds_progress_text_var = tk.StringVar(value="Idle")
        self.xds_progress_total = 0
        self.xds_allow_sg_mismatch_var = tk.BooleanVar(value=False)
        self.xds_run_merge_var = tk.BooleanVar(value=False)
        self.solve_root_var = tk.StringVar(value=str(self.data_root))
        self.solve_dataset_var = tk.StringVar(value="")
        self.solve_hkl_var = tk.StringVar(value="")
        self.solve_res_var = tk.StringVar(value=str(self.data_root / "initial.res"))
        self.solve_workdir_var = tk.StringVar(value=str(self.data_root))
        self.solve_basename_var = tk.StringVar(value="")
        self.solve_composition_var = tk.StringVar(value="")
        self.solve_formula_var = tk.StringVar(value="")
        self.solve_z_var = tk.StringVar(value="")
        self.solve_cell_var = tk.StringVar(value="")
        self.solve_sg_var = tk.StringVar(value="")
        self.solve_mode_var = tk.StringVar(value="prepare")
        self.solve_no_force_sg_var = tk.BooleanVar(value=False)
        self.solve_force_sg_var = tk.BooleanVar(value=True)
        self.solve_dry_run_var = tk.BooleanVar(value=True)
        self.solve_json_var = tk.BooleanVar(value=False)
        self.solve_hkl_choice_var = tk.StringVar(value="")
        self.solve_hkl_choices: list[Path] = []
        self.r3d_source_var = tk.StringVar(value="auto")
        self.r3d_run_var = tk.StringVar(value="")

        self.dataset_path: Path | None = None
        self.params: CRED2Parameters | None = None
        self.detector_shape: tuple[int, int] | None = None
        self.frames: list[FrameRecord] = []
        self.frame_stats: dict[int, FrameStats] = {}
        self.current_index = 0
        self.current_array: np.ndarray | None = None
        self.peak_points_by_index: dict[int, list[tuple[tuple[float, float], float]]] = {}
        self.last_mask_radius: float | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.axis_photo: ImageTk.PhotoImage | None = None
        self.axis_projection_image: Image.Image | None = None
        self.display_transform = (1.0, 0.0, 0.0)
        self.panel_path: Path | None = None
        self.embedded_panel_data: dict[str, dict[str, np.ndarray]] = {}
        self.embedded_reciprocal_basis: np.ndarray | None = None
        self.embedded_point_kind_var = tk.StringVar(value="observations")
        self.embedded_display_var = tk.StringVar(value="spot")
        self.embedded_threshold_var = tk.DoubleVar(value=0.0)
        self.embedded_threshold_label_var = tk.StringVar(value="0%")
        self.embedded_point_size_var = tk.DoubleVar(value=2.0)
        self.embedded_brightness_var = tk.DoubleVar(value=3.0)
        self.embedded_contrast_var = tk.DoubleVar(value=3.0)
        self.embedded_gamma_var = tk.DoubleVar(value=1.0)
        self.embedded_cell_var = tk.BooleanVar(value=True)
        self.embedded_indexed_var = tk.BooleanVar(value=False)
        self.embedded_yaw = -0.65
        self.embedded_pitch = 0.55
        self.embedded_roll = 0.0
        self.embedded_view_frame = np.eye(3, dtype=np.float64)
        self.embedded_zoom = 1.0
        self.embedded_pan = (0.0, 0.0)
        self.embedded_drag_start: tuple[int, int, np.ndarray, str] | None = None
        self.embedded_pan_start: tuple[int, int, float, float] | None = None
        self.reconstruction_running = False
        self.xds_available_datasets: list[dict[str, str]] = []
        self.xds_selected_datasets: list[dict[str, str]] = []
        self.xds_summary_rows: dict[str, dict[str, str]] = {}
        self.xds_cluster_result: dict[str, object] | None = None
        self.xds_active_cluster_method: str | None = None
        self.xds_cluster_cutoff: float | None = None
        self.xds_cluster_cutoffs: dict[str, float] = {}
        self.xds_last_clusters: list[list[str]] = []
        self.xds_dendrogram_layout: dict[str, object] = {}
        self._ui_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._schedule(80, self._drain_ui_queue)
        if dataset_dir:
            self.import_dataset()

    def _schedule(self, delay_ms: int, callback) -> str | None:
        """Schedule a Tk callback that is automatically cancelled on close."""
        if self._destroyed:
            return None
        after_id: str | None = None

        def run() -> None:
            if after_id is not None:
                self._after_ids.discard(after_id)
            if not self._destroyed:
                callback()

        after_id = self.root.after(delay_ms, run)
        self._after_ids.add(after_id)
        return after_id

    def _on_root_destroy(self, event: tk.Event) -> None:
        if event.widget is not self.root or self._destroyed:
            return
        self._destroyed = True
        for after_id in tuple(self._after_ids):
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()

    def _apply_theme(self) -> None:
        self.root.configure(background=UI_BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        default_font = (self._ui_font_family, UI_FONT_SIZE)
        heading_font = (self._ui_font_family, UI_HEADING_SIZE, "bold")
        self._console_font = (self._ui_mono_family, UI_FONT_SIZE)
        # Tcl font descriptions require braces around family names containing
        # spaces (for example the Song Ti alias used by classic X11 Tk).
        tk_font_spec = f"{{{self._ui_font_family}}} {UI_FONT_SIZE}"
        self.root.option_add("*Font", tk_font_spec)
        self.root.option_add("*Listbox.font", tk_font_spec)

        style.configure(
            ".",
            background=UI_BG,
            foreground=UI_TEXT,
            fieldbackground=UI_PANEL,
            bordercolor=UI_BORDER,
            lightcolor=UI_BORDER,
            darkcolor=UI_BORDER,
            troughcolor=UI_PANEL_ALT,
            font=default_font,
        )
        style.configure("TFrame", background=UI_BG)
        style.configure("TLabelframe", background=UI_BG, bordercolor=UI_BORDER)
        style.configure("TLabelframe.Label", background=UI_BG, foreground=UI_TEXT, font=heading_font)
        style.configure("TLabel", background=UI_BG, foreground=UI_TEXT)
        style.configure("Muted.TLabel", background=UI_BG, foreground=UI_MUTED)
        style.configure("TButton", background=UI_PANEL_ALT, foreground=UI_TEXT, borderwidth=1, focusthickness=0, padding=(10, 6))
        style.map(
            "TButton",
            background=[("active", UI_ACCENT), ("pressed", UI_ACCENT_ACTIVE)],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )
        style.configure("TCheckbutton", background=UI_BG, foreground=UI_TEXT)
        style.configure("TRadiobutton", background=UI_BG, foreground=UI_TEXT)
        style.configure("TEntry", fieldbackground=UI_PANEL, foreground=UI_TEXT, insertcolor=UI_TEXT)
        style.configure("TCombobox", fieldbackground=UI_PANEL, foreground=UI_TEXT)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", UI_PANEL)],
            selectbackground=[("readonly", UI_ACCENT)],
            selectforeground=[("readonly", "#ffffff")],
        )
        style.configure("TNotebook", background=UI_BG, borderwidth=0, tabmargins=(6, 6, 6, 0))
        style.configure("TNotebook.Tab", background=UI_PANEL, foreground=UI_MUTED, padding=(16, 10), borderwidth=0)
        style.map(
            "TNotebook.Tab",
            background=[("selected", UI_PANEL_ALT), ("active", UI_PANEL_ALT)],
            foreground=[("selected", UI_TEXT), ("active", UI_TEXT)],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=UI_ACCENT,
            troughcolor=UI_PANEL_ALT,
            bordercolor=UI_BORDER,
            thickness=12,
        )
        style.configure("Dark.Vertical.TScrollbar", background=UI_PANEL_ALT, troughcolor=UI_BG, arrowcolor=UI_TEXT)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=0)

        self._build_global_console().grid(row=0, column=1, sticky="nsew")

        self._notebook = ttk.Notebook(self.root)
        self._notebook.grid(row=0, column=0, sticky="nsew")
        self._last_workflow_tab = "AutoXDS"
        self._workflow_tabs: dict[str, ttk.Frame] = {}
        self._workflow_tabs["AutoXDS"] = self._build_autoxds_tab(self._notebook)
        self._notebook.add(self._workflow_tabs["AutoXDS"], text="AutoXDS")
        self._workflow_tabs["AutoDials"] = AutoDialsTab(self._notebook, self)
        self._notebook.add(self._workflow_tabs["AutoDials"], text="AutoDials")
        # Standalone peak search remains available internally to AutoR3D, but
        # its top-level workflow is hidden until it is ready for users again.
        self._peak_search_tab = self._build_peak_search_tab(self._notebook)
        # Rotation-axis search/refinement stays internal until that workflow is
        # ready to be exposed again.
        self._workflow_tabs["AutoR3D"] = self._build_reconstruction_tab(self._notebook)
        self._notebook.add(self._workflow_tabs["AutoR3D"], text="AutoR3D")
        self._workflow_tabs["AutoSolve"] = self._build_autosolve_tab(self._notebook)
        self._notebook.add(self._workflow_tabs["AutoSolve"], text="AutoSolve")
        if self.ai_assistant_enabled:
            self._assistant_tab = self._build_ai_tab(self._notebook)
            self._notebook.add(self._assistant_tab, text="Assistant")

        if self.ai_assistant_enabled:
            self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        status = ttk.Label(self.root, textvariable=self.status_var, anchor="w", padding=(10, 6), relief="groove")
        status.grid(row=1, column=0, columnspan=2, sticky="ew")

        if self.ai_assistant_enabled:
            self._init_ai_assistant()

    def _build_global_console(self) -> ttk.Frame:
        width = 340
        column = ttk.Frame(self.root, width=width, padding=(0, 10, 10, 10))
        column.grid_propagate(False)
        column.columnconfigure(0, weight=1)
        column.rowconfigure(1, weight=1)
        header = ttk.Frame(column)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Console").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="Clear", command=self._clear_console).grid(
            row=0,
            column=2,
            sticky="e",
        )
        log_wrap = ttk.Frame(column)
        log_wrap.grid(row=1, column=0, sticky="nsew")
        log_wrap.columnconfigure(0, weight=1)
        log_wrap.rowconfigure(0, weight=1)
        self.console_log = tk.Text(
            log_wrap,
            wrap="word",
            background=UI_PANEL,
            foreground=UI_TEXT,
            insertbackground=UI_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            highlightcolor=UI_ACCENT,
            font=self._console_font,
            spacing1=3,
            spacing2=3,
            spacing3=8,
            width=36,
            padx=10,
            pady=8,
        )
        self.console_log.grid(row=0, column=0, sticky="nsew")
        console_scroll = ttk.Scrollbar(log_wrap, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.console_log.yview)
        console_scroll.grid(row=0, column=1, sticky="ns")
        self.console_log.configure(yscrollcommand=console_scroll.set)
        self.axis_log = self.console_log
        self.reconstruction_log = self.console_log
        return column

    def _build_ai_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        header = ttk.Frame(tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="AutoCrys Assistant").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="Clear", command=self._clear_ai_messages).grid(row=0, column=1, sticky="e")

        transcript = ttk.Frame(tab)
        transcript.grid(row=1, column=0, sticky="nsew")
        transcript.columnconfigure(0, weight=1)
        transcript.rowconfigure(0, weight=1)
        self.ai_message_log = tk.Text(
            transcript,
            wrap="word",
            background=UI_PANEL,
            foreground=UI_TEXT,
            insertbackground=UI_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            highlightcolor=UI_ACCENT,
            font=self._console_font,
            spacing1=3,
            spacing2=3,
            spacing3=8,
            padx=14,
            pady=14,
        )
        self.ai_message_log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            transcript,
            orient="vertical",
            style="Dark.Vertical.TScrollbar",
            command=self.ai_message_log.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.ai_message_log.configure(yscrollcommand=scrollbar.set)
        self._build_ai_container(tab).grid(row=2, column=0, sticky="ew", pady=(8, 0))
        return tab

    def _build_ai_container(self, parent: tk.Widget) -> ttk.Frame:
        from .ai_assistant import AIAssistantPanel, AIContext, LogMonitor, SkillMapper, KnowledgeBase, ArtifactAnalyzer

        self._ai_context = AIContext()
        self._ai_monitor = LogMonitor()
        self._ai_skills = SkillMapper(self.autocrys_root)
        self._ai_knowledge = KnowledgeBase(self.autocrys_root)
        self._ai_analyzer = ArtifactAnalyzer(self.autocrys_root)

        def on_run_command(command: list[str], skill=None):
            skill_id = str(getattr(skill, "skill_id", "") or "agent_task")
            target = self._agent_skill_target(skill_id)
            label = f"{target or 'Agent'} · {skill_id}"
            self._console_log(f"[Agent] $ {self._command_text(command)}", detail=True)
            self._run_command_async(
                label,
                command,
                result_target={"tab": target, "skill_id": skill_id},
            )

        def on_execute_fix(fix_id: str, fix_params: dict):
            self._handle_ai_fix(fix_id, fix_params)

        panel = AIAssistantPanel(
            parent,
            context=self._ai_context,
            monitor=self._ai_monitor,
            knowledge=self._ai_knowledge,
            skill_mapper=self._ai_skills,
            config=self._ai_config,
            run_command_callback=on_run_command,
            execute_fix_callback=on_execute_fix,
            analyzer=self._ai_analyzer,
            context_provider=self._collect_ai_action_context,
            message_widget=self.ai_message_log,
            task_result_callback=self._handle_agent_feedback,
            font_family=self._ui_font_family,
            mono_family=self._ui_mono_family,
            verbose_console=self.console_verbose,
        )
        self._ai_panel = panel
        return panel

    def _collect_ai_action_context(self) -> dict:
        """Snapshot UI values for artifact inspection and skill preflight."""
        visible_tab = self._notebook.tab(self._notebook.select(), "text") if hasattr(self, "_notebook") else ""
        tab = self._last_workflow_tab if visible_tab == "Assistant" else visible_tab
        values: dict[str, object] = {
            "active_tab": tab,
            "visible_tab": visible_tab,
            "status_text": self.status_var.get() if hasattr(self, "status_var") else "",
        }
        if tab == "AutoXDS":
            root = self.xds_root_var.get().strip() or str(self.autocrys_root / "Data")
            selected = [str(item.get("name", "")) for item in self.xds_selected_datasets if item.get("name")]
            name = selected[0] if selected else self.xds_datasets_var.get().strip().split(" ")[0]
            path = None
            if self.xds_selected_datasets:
                raw = self.xds_selected_datasets[0].get("path") or self.xds_selected_datasets[0].get("relative_path")
                if raw:
                    path = str(raw)
            values.update({
                "root": root,
                "xds_root": root,
                "dataset": name,
                "dataset_name": name,
                "dataset_path": path or (str(Path(root) / name) if name else None),
                "datasets": selected,
                "cell": self.xds_cell_var.get().strip(),
                "sg": self.xds_sg_var.get().strip(),
                "method": self.xds_cluster_method_var.get().strip() or "both",
                "hca_cutoff": self.xds_cluster_cutoff,
                "hca_method": self.xds_active_cluster_method,
                "hca_clusters": [list(cluster) for cluster in self.xds_last_clusters],
                "selected_summary_rows": {
                    dataset_name: dict(self.xds_summary_rows.get(dataset_name, {}))
                    for dataset_name in selected
                    if dataset_name in self.xds_summary_rows
                },
                "panel_text": self.xds_center_text.get("1.0", "end").strip()[-6000:]
                if hasattr(self, "xds_center_text")
                else "",
            })
        elif tab == "AutoDials":
            panel = self._workflow_tabs["AutoDials"]
            paths = panel.selected()
            values.update(root=panel.root_var.get(), datasets=list(map(str, paths)),
                          dataset_path=str(paths[0]) if paths else None, cell=panel.cell_var.get(),
                          sg=panel.sg_var.get(), result=panel.active_result)
        elif tab == "AutoSolve":
            workdir = self.solve_workdir_var.get().strip()
            dataset = self.solve_dataset_var.get().strip()
            hkl = self.solve_hkl_var.get().strip()
            res = self.solve_res_var.get().strip()
            values.update({
                "root": self.solve_root_var.get().strip(),
                "dataset": dataset,
                "dataset_name": self.solve_basename_var.get().strip() or (Path(dataset).name if dataset else ""),
                "dataset_path": workdir or hkl or dataset or None,
                "workdir": workdir or None,
                "hkl": hkl or None,
                "res": res if res and _normalize_path(res).is_file() else None,
                "basename": self.solve_basename_var.get().strip(),
                "composition": self.solve_composition_var.get().strip(),
                "cell": self.solve_cell_var.get().strip(),
                "sg": self.solve_sg_var.get().strip(),
                "preview_text": self.solve_preview.get("1.0", "end").strip()[-6000:]
                if hasattr(self, "solve_preview")
                else "",
            })
        else:
            dataset = self.dataset_path or (_normalize_path(self.dataset_var.get()) if self.dataset_var.get().strip() else None)
            values.update({
                "dataset": str(dataset) if dataset else "",
                "dataset_name": dataset.name if dataset else "",
                "dataset_path": str(dataset) if dataset else None,
                "frames": self.frames_dir,
                "output": self.output_var.get().strip(),
                "axis": self.axis_var.get().strip(),
                "center_x": self.center_x_var.get().strip(),
                "center_y": self.center_y_var.get().strip(),
                "frame_info": self.frame_info_var.get(),
                "reconstruction_info": self.reconstruction_info_var.get(),
            })
        return values

    def _clear_console(self) -> None:
        self.console_log.delete("1.0", "end")
        if self.ai_assistant_enabled and hasattr(self, "_ai_monitor"):
            self._ai_monitor.clear()
            self._ai_context.diagnosis_count = 0

    def _clear_ai_messages(self) -> None:
        if hasattr(self, "ai_message_log"):
            self.ai_message_log.delete("1.0", "end")

    def _init_ai_assistant(self) -> None:
        self._ai_context.update_tab(self._notebook.tab(self._notebook.select(), "text"))
        self._ai_context.set_idle()
        self._ai_context.update_action_context(self._collect_ai_action_context())
        self.status_var.trace_add("write", lambda *_args: self._notify_ai("status_changed", text=self.status_var.get()))

    def _on_tab_changed(self, event: object = None) -> None:
        if not self.ai_assistant_enabled:
            return
        tab_name = self._notebook.tab(self._notebook.select(), "text")
        if tab_name != "Assistant":
            self._last_workflow_tab = tab_name
            self._ai_context.update_tab(tab_name)
        self._ai_context.update_action_context(self._collect_ai_action_context())
        if hasattr(self, "_ai_panel"):
            self._schedule(150, self._ai_panel._request_analysis)

    def _notify_ai(self, event_type: str, **data) -> None:
        if not self.ai_assistant_enabled:
            return
        try:
            if event_type == "tab_changed":
                self._ai_context.update_tab(data.get("tab", ""))
            elif event_type == "dataset_selected":
                self._ai_context.update_dataset(data.get("path"), data.get("name"))
            elif event_type == "operation_started":
                operation_context = self._collect_ai_action_context()
                operation_context["_diagnosis_start"] = self._ai_monitor.diagnosis_count
                self._ai_context.update_action_context(operation_context)
                self._ai_context.start_operation(data.get("op", ""), data.get("cmd"))
            elif event_type == "operation_result":
                self._ai_context.record_operation_result(data.get("op", ""), data.get("result"))
            elif event_type == "operation_finished":
                self._ai_context.finish_operation(data.get("op", ""), data.get("success", True), data.get("error"))
            elif event_type == "status_changed":
                self._ai_context.update_status(data.get("text", ""))
            elif event_type == "console_output":
                self._ai_context.append_console(data.get("line", ""))
                if self._ai_monitor.active:
                    self._ai_monitor.feed_single(data.get("line", ""), self._ai_context.active_tab)
            elif event_type == "hca_updated":
                self._ai_context.update_hca_result(data.get("result"), data.get("cutoff"))
            elif event_type == "summary_updated":
                self._ai_context.update_summary_rows(data.get("rows", {}))
        except Exception:
            pass

    def _handle_ai_fix(self, fix_id: str, fix_params: dict) -> None:
        if fix_id == "xds_recovery":
            self._console_log("[AI] Triggering XDS recovery mode...")
        elif fix_id == "set_max_processors":
            self._console_log("[AI] Set MAXIMUM_NUMBER_OF_PROCESSORS=1 in XDS.INP and rerun.")
        elif fix_id == "fix_xscale_order":
            self._console_log("[AI] XSCALE order fix not yet automated from UI.")
        elif fix_id == "try_p1_solve":
            self._console_log("[AI] Suggesting P-1 space group for structure solution.")
        elif fix_id == "allow_sg_mismatch":
            self.xds_allow_sg_mismatch_var.set(True)
            self._console_log("[AI] Enabled --allow-sg-mismatch flag.")
        else:
            self._console_log(f"[AI] Unknown fix: {fix_id}")

    def _build_autoxds_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(0, weight=0)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        # The Root row contains a label, path entry, and Browse button.  At the
        # larger UI font 330px leaves the entry almost completely collapsed.
        left = ttk.Frame(tab, width=390)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 14))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)

        center = ttk.Frame(tab)
        center.grid(row=0, column=1, sticky="nsew")
        center.columnconfigure(0, weight=1)
        center.rowconfigure(1, weight=1)

        controls = ttk.LabelFrame(left, text="AutoXDS", padding=10)
        controls.grid(row=0, column=0, sticky="new")
        controls.columnconfigure(1, weight=1)

        root_row = ttk.Frame(controls)
        root_row.grid(row=0, column=0, columnspan=3, sticky="ew")
        root_row.columnconfigure(1, weight=1)
        ttk.Label(root_row, text="Root").grid(row=0, column=0, sticky="w")
        self.xds_root_entry = ttk.Entry(root_row, textvariable=self.xds_root_var)
        self.xds_root_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(root_row, text="Browse", command=self.xds_browse_root).grid(row=0, column=2, sticky="e", padx=(8, 0))

        ttk.Label(controls, text="Datasets").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.xds_datasets_var).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(
            controls,
            text="Choose dataset folders (supports multi-select).",
            anchor="w",
            wraplength=280,
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Button(controls, text="Browse Datasets", command=self.xds_browse_datasets).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(controls, text="Select All", command=self.xds_select_all).grid(row=3, column=2, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Label(controls, text="Unit Cell").grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.xds_cell_var).grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(10, 0))
        ttk.Label(controls, text="Space Group").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.xds_sg_var).grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(controls, text="Resolution").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.xds_resolution_var).grid(row=6, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(controls, text="Clustering").grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            controls,
            textvariable=self.xds_cluster_method_var,
            values=("both", "unit-cell", "cc1"),
            state="readonly",
        ).grid(row=7, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))

        actions = ttk.LabelFrame(left, text="Actions", padding=10)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="Set Cell/SG/Res", command=self.xds_set_cell_sg_res).grid(row=0, column=0, sticky="ew")
        ttk.Button(actions, text="Dataset List", command=self.xds_show_dataset_list).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(actions, text="Process & Convert", command=self.xds_process_convert).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="HCA Run", command=self.xds_hca_run).grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Merge & Conv", command=self.xds_merge_and_convert).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.xds_progressbar = ttk.Progressbar(actions, variable=self.xds_progress_var, mode="determinate", maximum=100)
        self.xds_progressbar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(actions, textvariable=self.xds_progress_text_var, style="Muted.TLabel").grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(4, 0),
        )

        self.xds_method_bar = ttk.Frame(center)
        self.xds_method_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.xds_method_bar.columnconfigure(2, weight=1)
        self.xds_unit_cell_button = ttk.Button(
            self.xds_method_bar,
            text="UnitCell",
            command=lambda: self.xds_set_cluster_method("unit-cell"),
        )
        self.xds_cc_button = ttk.Button(
            self.xds_method_bar,
            text="CC",
            command=lambda: self.xds_set_cluster_method("cc1"),
        )
        self.xds_unit_cell_button.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.xds_cc_button.grid(row=0, column=1, sticky="w")
        self.xds_method_bar.grid_remove()

        self.xds_center_stack = ttk.LabelFrame(center, text="AutoXDS Details", padding=8)
        self.xds_center_stack.grid(row=1, column=0, sticky="nsew")
        self.xds_center_stack.columnconfigure(0, weight=1)
        self.xds_center_stack.rowconfigure(0, weight=1)
        self.xds_center_text = tk.Text(
            self.xds_center_stack,
            wrap="word",
            background=UI_PANEL,
            foreground=UI_TEXT,
            insertbackground=UI_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            highlightcolor=UI_ACCENT,
            font=self._console_font,
            spacing1=3,
            spacing2=3,
            spacing3=8,
            padx=14,
            pady=14,
        )
        self.xds_center_text.grid(row=0, column=0, sticky="nsew")
        self.xds_dendrogram_canvas = tk.Canvas(self.xds_center_stack, background=UI_BG, highlightthickness=0)
        self.xds_dendrogram_canvas.grid(row=0, column=0, sticky="nsew")
        self.xds_dendrogram_canvas.bind("<Configure>", lambda _event: self.xds_render_dendrogram())
        self.xds_dendrogram_canvas.bind("<Button-1>", self.xds_on_dendrogram_click)
        self.xds_dendrogram_canvas.grid_remove()

        self.xds_show_text(
            "AutoXDS ready.\n\nSelect a working directory, choose datasets, then run Process & Convert or HCA Run.",
            "Status is now written to the global Console panel.",
        )
        return tab

    def xds_show_text(self, center: str, status: str | None = None, *, keep_methods: bool = False) -> None:
        if hasattr(self, "xds_dendrogram_canvas"):
            self.xds_dendrogram_canvas.grid_remove()
        if hasattr(self, "xds_center_text"):
            self.xds_center_text.grid()
            self.xds_center_text.tkraise()
            self.xds_center_text.delete("1.0", "end")
            self.xds_center_text.insert("end", center.rstrip() + "\n")
        if hasattr(self, "xds_method_bar") and not keep_methods:
            self.xds_method_bar.grid_remove()
        if status is not None:
            self.xds_show_status(status)

    def xds_show_status(self, text: str) -> None:
        message = text.rstrip()
        if not message:
            return
        first_line = message.splitlines()[0]
        self.status_var.set(first_line)
        displayed = message if self.console_verbose else first_line
        self._console_log(f"[AutoXDS] {displayed}")

    def xds_progress_begin(self, label: str, total: int = 0) -> None:
        if not hasattr(self, "xds_progressbar"):
            return
        self.xds_progressbar.stop()
        self.xds_progress_total = max(0, int(total))
        if self.xds_progress_total > 0:
            self.xds_progressbar.configure(mode="determinate", maximum=self.xds_progress_total)
            self.xds_progress_var.set(0.0)
            self.xds_progress_text_var.set(f"{label} (0/{self.xds_progress_total})")
        else:
            self.xds_progressbar.configure(mode="indeterminate", maximum=100)
            self.xds_progress_var.set(0.0)
            self.xds_progress_text_var.set(label)
            self.xds_progressbar.start(12)

    def xds_progress_step(self, index: int, label: str | None = None) -> None:
        if not hasattr(self, "xds_progressbar"):
            return
        if self.xds_progress_total > 0:
            current = min(max(0, int(index)), self.xds_progress_total)
            self.xds_progress_var.set(float(current))
            if label:
                self.xds_progress_text_var.set(f"{label} ({current}/{self.xds_progress_total})")
        elif label:
            self.xds_progress_text_var.set(label)

    def xds_progress_end(self, label: str) -> None:
        if not hasattr(self, "xds_progressbar"):
            return
        self.xds_progressbar.stop()
        if self.xds_progress_total > 0:
            self.xds_progress_var.set(float(self.xds_progress_total))
            self.xds_progressbar.configure(mode="determinate", maximum=self.xds_progress_total)
        else:
            self.xds_progressbar.configure(mode="determinate", maximum=100)
            self.xds_progress_var.set(0.0)
        self.xds_progress_total = 0
        self.xds_progress_text_var.set(label)

    def xds_root(self) -> Path:
        root = _normalize_path(self.xds_root_var.get())
        if not root.exists():
            raise FileNotFoundError(f"Working directory does not exist: {root}")
        return root

    def xds_script(self) -> Path:
        return self.autocrys_root / "AutoXDS" / "scripts" / "auto_xds.py"

    def xds_cluster_method(self) -> str:
        value = self.xds_cluster_method_var.get().strip().lower().replace("_", "-").replace(" ", "")
        if not value:
            return "both"
        aliases = {
            "unitcell": "unit-cell",
            "cell": "unit-cell",
            "unit-cell": "unit-cell",
            "cc": "cc1",
            "cc1": "cc1",
            "both": "both",
        }
        if value not in aliases:
            raise ValueError("Clustering must be UnitCell, CC, Both, or blank.")
        return aliases[value]

    def xds_parse_json(self, text: str) -> dict[str, object]:
        text = text.strip()
        if not text:
            raise ValueError("AutoXDS produced no JSON output.")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    def xds_load_available_datasets(self) -> list[dict[str, str]]:
        command = [
            sys.executable,
            str(self.xds_script()),
            "list",
            "--root",
            str(self.xds_root()),
            "--json",
        ]
        completed = subprocess.run(
            [str(part) for part in command],
            cwd=str(self.autocrys_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "AutoXDS list failed").strip())
        result = self.xds_parse_json(completed.stdout)
        datasets = [dict(item) for item in result.get("datasets", [])]
        self.xds_available_datasets = datasets
        return datasets

    def xds_browse_root(self) -> None:
        initial = _normalize_path(self.xds_root_var.get())
        initial_dir = str(initial if initial.exists() else self.autocrys_root)
        selected = filedialog.askdirectory(initialdir=initial_dir, parent=self.root)
        if not selected:
            return
        self.xds_root_var.set(selected)
        self.xds_selected_datasets = []
        self.xds_datasets_var.set("")
        self.xds_cluster_result = None
        self.xds_cluster_cutoffs.clear()
        self.xds_show_text("Working directory selected.\n\nUse Browse or Select all to choose datasets.", "")

    def xds_set_selected_datasets(self, datasets: list[dict[str, str]]) -> None:
        self.xds_selected_datasets = datasets
        self.xds_datasets_var.set(" ".join(str(item.get("name", "")) for item in datasets))
        duplicate_names = sorted({item["name"] for item in datasets if sum(1 for other in datasets if other["name"] == item["name"]) > 1})
        status = f"Selected {len(datasets)} dataset(s)."
        if duplicate_names:
            status += "\nWarning: duplicate dataset names selected; cluster/merge summary matching uses dataset names."
        self.xds_show_status(status)
        if datasets:
            first = datasets[0]
            ds_name = first.get("name", "")
            ds_path_raw = first.get("path", "") or first.get("relative_path", "")
            self._notify_ai("dataset_selected", name=ds_name, path=Path(ds_path_raw))

    def xds_select_all(self) -> None:
        try:
            datasets = self.xds_load_available_datasets()
            self.xds_set_selected_datasets(datasets)
            self.xds_show_dataset_list()
        except Exception as exc:
            messagebox.showerror("AutoXDS", str(exc), parent=self.root)
            self.status_var.set(f"AutoXDS select all failed: {exc}")

    def xds_browse_datasets(self) -> None:
        try:
            datasets = self.xds_load_available_datasets()
        except Exception as exc:
            messagebox.showerror("AutoXDS", str(exc), parent=self.root)
            return
        try:
            root = self.xds_root()
        except Exception as exc:
            messagebox.showerror("AutoXDS", str(exc), parent=self.root)
            return
        direct_datasets: list[dict[str, str]] = []
        for item in datasets:
            raw = str(item.get("path", "") or item.get("relative_path", "")).strip()
            if not raw:
                continue
            path_obj = _normalize_path(raw)
            try:
                relative = path_obj.relative_to(root)
            except ValueError:
                relative_text = str(item.get("relative_path", "")).strip().strip("/").strip("\\")
                if not relative_text:
                    continue
                relative = Path(relative_text)
            if len(relative.parts) == 1:
                direct_datasets.append(item)
        datasets = direct_datasets
        if not datasets:
            messagebox.showinfo("AutoXDS", "No direct child datasets found under current root.", parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Select AutoXDS datasets")
        dialog.geometry("720x430")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        listbox = tk.Listbox(
            dialog,
            selectmode="extended",
            background=UI_PANEL,
            foreground=UI_TEXT,
            selectbackground=UI_ACCENT,
            selectforeground="#ffffff",
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            highlightcolor=UI_ACCENT,
        )
        listbox.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        for item in datasets:
            listbox.insert("end", f"{item.get('name', '')}    {item.get('relative_path', item.get('path', ''))}")

        chosen: list[dict[str, str]] = []

        def accept() -> None:
            chosen.extend(datasets[int(index)] for index in listbox.curselection())
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Select", command=accept).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).grid(row=0, column=2, padx=(8, 0))
        self.root.wait_window(dialog)
        if chosen:
            self.xds_set_selected_datasets(chosen)
            self.xds_show_dataset_list()

    def xds_selected_names(self) -> list[str]:
        names = _split_names(self.xds_datasets_var.get())
        if not names and self.xds_selected_datasets:
            names = [str(item.get("name", "")) for item in self.xds_selected_datasets if item.get("name")]
        if not names:
            raise ValueError("Select at least one dataset.")
        return names

    def xds_summary_path(self) -> Path:
        root = self.xds_root()
        return root / "Data" / "summary.txt" if (root / "Data").exists() else root / "summary.txt"

    def xds_read_summary_rows(self) -> dict[str, dict[str, str]]:
        path = self.xds_summary_path()
        rows: dict[str, dict[str, str]] = {}
        if not path.exists():
            self.xds_summary_rows = rows
            return rows
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                name = row.get("Dataset", "")
                if name:
                    rows[name] = dict(row)
        self.xds_summary_rows = rows
        return rows

    def xds_show_dataset_list(self) -> None:
        names = _split_names(self.xds_datasets_var.get())
        if not names and self.xds_selected_datasets:
            names = [str(item.get("name", "")) for item in self.xds_selected_datasets if item.get("name")]
        if not names:
            self.xds_show_text("No datasets selected.", "No datasets selected.")
            return
        by_name = {str(item.get("name", "")): item for item in self.xds_available_datasets + self.xds_selected_datasets}
        lines = ["Selected datasets", ""]
        for index, name in enumerate(names, 1):
            item = by_name.get(name, {})
            location = item.get("path") or item.get("relative_path") or "manual entry"
            lines.append(f"{index}. {name}")
            lines.append(f"   {location}")
        self.xds_show_text("\n".join(lines), f"{len(names)} dataset(s) selected.")

    def xds_is_failed_dataset(self, name: str) -> tuple[bool, str]:
        rows = self.xds_summary_rows or self.xds_read_summary_rows()
        row = rows.get(name)
        if not row:
            return False, ""
        final = str(row.get("Final", "")).strip()
        xdsconv = str(row.get("Xdsconv", "")).strip()
        output = str(row.get("DatasetHkl", row.get("Output", ""))).strip()
        status_blob = f"{final} {xdsconv}".lower()
        failed_tokens = ("fail", "error", "missing", "traceback", "not found")
        success_tokens = ("ok", "finish", "done", "success")
        failed = any(token in status_blob for token in failed_tokens)
        successful = any(token in status_blob for token in success_tokens)
        if failed and not successful:
            reason = final or xdsconv or "failed"
            return True, reason
        if output and output not in {"NA", "na", ""}:
            return False, ""
        return False, ""

    def xds_append_dataset_args(self, command: list[str], names: list[str]) -> None:
        for name in names:
            command.extend(["--dataset", name])

    def xds_run_json_async(self, label: str, command: list[str], callback, dataset_names: list[str] | None = None) -> None:
        self.status_var.set(f"Running {label}...")
        self._notify_ai("operation_started", op=label, cmd=self._command_text(command))
        self.xds_show_status(f"{label} running...\n\n$ {self._command_text(command)}")
        names = list(dataset_names or [])
        self.xds_progress_begin(label, len(names))
        for index, name in enumerate(names, 1):
            self._console_log(f"[{label}] queued {index}/{len(names)}: {name}", detail=True)

        def worker() -> None:
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            seen_names: set[str] = set()

            def notify_line(text: str) -> None:
                line = text.rstrip()
                if not line:
                    return
                self._schedule(0, lambda msg=f"[{label}] {line}": self._console_log(msg, detail=True))
                if not names:
                    return
                lowered = line.lower()
                for name in names:
                    key = name.lower()
                    if key in seen_names:
                        continue
                    if key in lowered:
                        seen_names.add(key)
                        count = len(seen_names)
                        self._schedule(0, lambda c=count, n=name: self.xds_progress_step(c, f"{label}: {n}"))

            def stream_reader(stream, collector: list[str]) -> None:
                if stream is None:
                    return
                for raw in iter(stream.readline, ""):
                    collector.append(raw)
                    notify_line(raw)
                stream.close()

            try:
                process = subprocess.Popen(
                    [str(part) for part in command],
                    cwd=str(self.autocrys_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )

                out_thread = threading.Thread(target=stream_reader, args=(process.stdout, stdout_chunks), daemon=True)
                err_thread = threading.Thread(target=stream_reader, args=(process.stderr, stderr_chunks), daemon=True)
                out_thread.start()
                err_thread.start()

                returncode = process.wait()
                out_thread.join()
                err_thread.join()

                stdout_text = "".join(stdout_chunks)
                stderr_text = "".join(stderr_chunks)
                completed = subprocess.CompletedProcess(
                    args=command,
                    returncode=returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                )
                if completed.returncode != 0:
                    raise RuntimeError((completed.stderr or completed.stdout or f"{label} failed").strip())
                result = self.xds_parse_json(completed.stdout)
            except Exception as exc:
                self._schedule(0, lambda error=exc: self.xds_finish_json_error(label, error))
                return
            self._schedule(0, lambda: self.xds_finish_json_success(label, callback, result, completed, names))

        threading.Thread(target=worker, daemon=True).start()

    def xds_finish_json_success(
        self,
        label: str,
        callback,
        result: dict[str, object],
        completed: subprocess.CompletedProcess[str],
        dataset_names: list[str],
    ) -> None:
        try:
            callback(result, completed)
        finally:
            self._notify_ai("operation_result", op=label, result=result)
            self._notify_ai("operation_finished", op=label, success=True)
            if dataset_names:
                self.xds_progress_step(len(dataset_names), f"{label} complete")
            self.xds_progress_end(f"{label} complete")

    def xds_finish_json_error(self, label: str, error: Exception) -> None:
        self.status_var.set(f"{label} failed: {error}")
        self.xds_show_status(f"{label} failed.\n\n{error}")
        self._console_log(f"[{label}] ERROR: {error}")
        self._notify_ai("operation_finished", op=label, success=False, error=str(error))
        self.xds_progress_end(f"{label} failed")

    def xds_common_command(self, action: str) -> list[str]:
        return [
            sys.executable,
            str(self.xds_script()),
            action,
            "--root",
            str(self.xds_root()),
            "--json",
        ]

    def xds_validate_cell_sg_resolution(self) -> tuple[str, str, str]:
        cell = self.xds_cell_var.get().strip()
        sg = self.xds_sg_var.get().strip()
        resolution = self.xds_resolution_var.get().strip()
        if not cell:
            if sg:
                self.xds_sg_var.set("")
            return "", "", resolution
        if not sg:
            raise ValueError("Space Group is required when Unit Cell is specified.")
        return cell, sg, resolution

    def xds_set_cell_sg_res(self) -> None:
        try:
            names = self.xds_selected_names()
            cell, sg, resolution = self.xds_validate_cell_sg_resolution()
            command = self.xds_common_command("set-cell-sg")
            self.xds_append_dataset_args(command, names)
            if cell and sg:
                command.extend(["--declared-cell", cell, "--declared-sg", sg])
            else:
                command.append("--clear-declared-cell-sg")
            if resolution:
                command.extend(["--resolution", resolution])
        except Exception as exc:
            messagebox.showerror("AutoXDS", str(exc), parent=self.root)
            return

        self.xds_show_text("Updating XDS.INP constraints...", f"Updating {len(names)} dataset(s).")

        def done(result: dict[str, object], _completed: subprocess.CompletedProcess[str]) -> None:
            files = [str(path) for path in result.get("files", [])]
            self.status_var.set("AutoXDS Set Cell/SG/Res complete")
            self.xds_show_text(
                "Set Cell/SG/Res complete\n\n" + "\n".join(files),
                f"Updated {len(files)} XDS.INP file(s).",
            )

        self.xds_run_json_async("AutoXDS Set Cell/SG/Res", command, done, dataset_names=names)

    def xds_process_convert(self) -> None:
        try:
            names = self.xds_selected_names()
            command = self.xds_common_command("process")
            self.xds_append_dataset_args(command, names)
            cell = self.xds_cell_var.get().strip()
            sg = self.xds_sg_var.get().strip()
            if bool(cell) != bool(sg):
                raise ValueError("Unit Cell and Space Group must be both non-empty or both empty.")
            if cell and sg:
                command.extend(["--declared-cell", cell, "--declared-sg", sg])
            else:
                command.extend([
                    "--clear-declared-cell-sg",
                    "--preserve-original-cell-sg-on-first-run",
                ])
            resolution = self.xds_resolution_var.get().strip()
            if resolution:
                command.extend(["--resolution", resolution])
        except Exception as exc:
            messagebox.showerror("AutoXDS", str(exc), parent=self.root)
            return

        self.xds_show_text(
            "Process & Convert is running...\n\nAutoXDS will process selected datasets and export temp.hkl plus dataset_name.hkl.",
            f"Processing {len(names)} dataset(s).",
        )

        def done(result: dict[str, object], completed: subprocess.CompletedProcess[str]) -> None:
            rows = [dict(row) for row in result.get("rows", [])]
            self.xds_summary_rows.update({str(row.get("Dataset", "")): row for row in rows})
            lines = ["Process & Convert summary", ""]
            status_lines = []
            for index, row in enumerate(rows, 1):
                name = row.get("Dataset", "")
                final = row.get("Final", "")
                resolution = _display_resolution(row.get("Resolution", "NA"))
                lines.append(f"{name}")
                lines.append(f"  SG: {row.get('SG', 'NA')}")
                lines.append(f"  Cell: {row.get('Cell', 'NA')}")
                lines.append(
                    f"  ISa: {row.get('ISa', 'NA')}  R: {row.get('Rfactor', 'NA')}  "
                    f"Resolution: {resolution}  Completeness: {row.get('Completeness', 'NA')}"
                )
                lines.append(f"  XDSCONV: {row.get('Xdsconv', 'NA')}  Output: {row.get('DatasetHkl', row.get('Output', 'NA'))}")
                lines.append("")
                status_lines.append(f"{name}: {final}")
                self.xds_progress_step(index, f"Process & Convert: {name}")
                self._console_log(f"[AutoXDS Process] completed {index}/{max(len(rows), 1)}: {name} ({final})")
            if not rows:
                self._console_log("[AutoXDS Process] completed with no summary rows returned.")
            if completed.stderr:
                lines.append("Command log")
                lines.append(completed.stderr.strip())
            self.status_var.set("AutoXDS Process & Convert complete")
            self.xds_show_text("\n".join(lines), "\n".join(status_lines))

        self.xds_run_json_async("AutoXDS Process & Convert", command, done, dataset_names=names)

    def xds_hca_run(self) -> None:
        try:
            names = self.xds_selected_names()
            if len(names) < 2:
                raise ValueError("HCA Run needs at least two datasets.")
            valid_names: list[str] = []
            skipped: list[tuple[str, str]] = []
            for name in names:
                failed, reason = self.xds_is_failed_dataset(name)
                if failed:
                    skipped.append((name, reason))
                else:
                    valid_names.append(name)
            if skipped:
                for name, reason in skipped:
                    self._console_log(f"[AutoXDS HCA] skipped dataset {name}: {reason}")
            if len(valid_names) < 2:
                raise ValueError("HCA Run needs at least two valid datasets after skipping failed ones.")
            method = self.xds_cluster_method()
            command = self.xds_common_command("cluster")
            self.xds_append_dataset_args(command, valid_names)
            command.extend(["--cluster-method", method])
            dendrogram = self.autocrys_root / "AutoXDS" / "cache" / "autoxds_hca.svg"
            command.extend(["--dendrogram", str(dendrogram)])
        except Exception as exc:
            messagebox.showerror("AutoXDS", str(exc), parent=self.root)
            return

        self.xds_show_text(
            "HCA Run is running...\n\nThe dendrogram will appear here. Click a height on it to choose cutoff.",
            f"HCA method: {method} (datasets: {len(valid_names)})",
        )

        def done(result: dict[str, object], _completed: subprocess.CompletedProcess[str]) -> None:
            self.xds_cluster_result = result
            self.xds_cluster_cutoffs.clear()
            self.xds_summary_rows.update(self.xds_read_summary_rows())
            methods = list(dict(result.get("methods", {})).keys())
            self.xds_active_cluster_method = "unit-cell" if "unit-cell" in methods else (methods[0] if methods else None)
            self.xds_cluster_cutoff = None
            self.status_var.set("AutoXDS HCA complete; choose cutoff")
            self._notify_ai("hca_updated", result=result, cutoff=None)
            self._notify_ai("summary_updated", rows=self.xds_summary_rows)
            self.xds_update_method_buttons()
            if len(methods) > 1:
                self.xds_method_bar.grid()
            else:
                self.xds_method_bar.grid_remove()
            self.xds_center_text.grid_remove()
            self.xds_dendrogram_canvas.grid()
            self.xds_dendrogram_canvas.tk.call("raise", self.xds_dendrogram_canvas._w)
            self.root.after_idle(self.xds_render_dendrogram)
            self.xds_update_cluster_preview()

        self.xds_run_json_async("AutoXDS HCA Run", command, done, dataset_names=valid_names)

    def xds_set_cluster_method(self, method: str) -> None:
        if not self.xds_cluster_result:
            return
        methods = dict(self.xds_cluster_result.get("methods", {}))
        if method not in methods:
            return
        self.xds_active_cluster_method = method
        self.xds_cluster_cutoff = self.xds_cluster_cutoffs.get(method)
        self.xds_update_method_buttons()
        self.xds_center_text.grid_remove()
        self.xds_dendrogram_canvas.grid()
        self.xds_dendrogram_canvas.tk.call("raise", self.xds_dendrogram_canvas._w)
        self.xds_render_dendrogram()
        self.xds_update_cluster_preview()

    def xds_update_method_buttons(self) -> None:
        if not hasattr(self, "xds_unit_cell_button") or not hasattr(self, "xds_cc_button"):
            return
        active = self.xds_active_cluster_method
        self.xds_unit_cell_button.configure(state="disabled" if active == "unit-cell" else "normal")
        self.xds_cc_button.configure(state="disabled" if active == "cc1" else "normal")

    def xds_current_hca(self) -> tuple[list[str], list[dict[str, object]]]:
        if not self.xds_cluster_result or not self.xds_active_cluster_method:
            return [], []
        methods = dict(self.xds_cluster_result.get("methods", {}))
        method_result = dict(methods.get(self.xds_active_cluster_method, {}))
        labels = [str(value) for value in self.xds_cluster_result.get("datasets", [])]
        hca = dict(method_result.get("hca", {}))
        merges = [dict(item) for item in hca.get("merges", [])]
        return labels, merges

    def xds_render_dendrogram(self) -> None:
        if not hasattr(self, "xds_dendrogram_canvas"):
            return
        canvas = self.xds_dendrogram_canvas
        canvas.delete("all")
        width = max(1, int(canvas.winfo_width()))
        height = max(1, int(canvas.winfo_height()))
        labels, merges = self.xds_current_hca()
        canvas.create_rectangle(0, 0, width, height, fill="#0b1018", outline="")
        if not labels:
            canvas.create_text(width / 2, height / 2, text="No HCA result", fill="#c8d0d6")
            return
        left_margin = 70
        right_margin = 30
        top_margin = 58
        bottom_margin = 112
        plot_w = max(1, width - left_margin - right_margin)
        plot_h = max(1, height - top_margin - bottom_margin)
        max_distance = max([float(item.get("distance", 0.0)) for item in merges] + [1.0])
        max_distance *= 1.08
        baseline = top_margin + plot_h
        leaf_colors: dict[str, str] = {}
        if self.xds_cluster_cutoff is not None:
            cutoff_clusters = self.xds_clusters_at_cutoff(labels, merges, self.xds_cluster_cutoff)
            for cluster_index, cluster in enumerate(cutoff_clusters):
                color = XDS_CLUSTER_COLORS[cluster_index % len(XDS_CLUSTER_COLORS)]
                for dataset_name in cluster:
                    leaf_colors[dataset_name] = color

        def sy(distance: float) -> float:
            return top_margin + (max_distance - distance) / max(max_distance, 1e-9) * plot_h

        step_x = plot_w / max(1, len(labels) - 1)
        node_x = {label: left_margin + index * step_x for index, label in enumerate(labels)}
        node_y = {label: 0.0 for label in labels}
        node_color = {label: leaf_colors.get(label, "#7bb4ff") for label in labels}
        canvas.create_text(
            width / 2,
            24,
            text=f"AutoXDS HCA dendrogram ({self.xds_active_cluster_method or ''})",
            fill="#e8edf2",
            font=(self._ui_font_family, UI_HEADING_SIZE, "bold"),
        )
        canvas.create_line(left_margin, baseline, left_margin + plot_w, baseline, fill="#5d6670")
        canvas.create_line(left_margin - 12, top_margin, left_margin - 12, baseline, fill="#5d6670")
        for tick in range(5):
            value = max_distance * tick / 4.0
            y = sy(value)
            canvas.create_line(left_margin - 18, y, left_margin - 12, y, fill="#5d6670")
            canvas.create_text(left_margin - 22, y, text=f"{value:.3g}", fill="#cfd6dd", anchor="e", font=(self._ui_font_family, UI_FONT_SIZE - 1))

        for merge in merges:
            left_name = str(merge.get("left", ""))
            right_name = str(merge.get("right", ""))
            distance = float(merge.get("distance", 0.0))
            cluster_name = f"cluster_{merge.get('step')}"
            if left_name not in node_x or right_name not in node_x:
                continue
            x1 = node_x[left_name]
            x2 = node_x[right_name]
            y1 = sy(float(node_y[left_name]))
            y2 = sy(float(node_y[right_name]))
            y = sy(distance)
            left_color = node_color.get(left_name, "#7bb4ff")
            right_color = node_color.get(right_name, "#7bb4ff")
            if self.xds_cluster_cutoff is not None and distance <= self.xds_cluster_cutoff:
                branch_color = left_color
            else:
                branch_color = "#7b8ea8"
            canvas.create_line(x1, y1, x1, y, fill=branch_color, width=2)
            canvas.create_line(x2, y2, x2, y, fill=branch_color, width=2)
            canvas.create_line(x1, y, x2, y, fill=branch_color, width=2)
            node_x[cluster_name] = (x1 + x2) / 2.0
            node_y[cluster_name] = distance
            node_color[cluster_name] = left_color if distance <= (self.xds_cluster_cutoff or -1.0) else "#7b8ea8"

        for label_text in labels:
            x = node_x[label_text]
            canvas.create_text(
                x,
                baseline + 16,
                text=label_text,
                fill=leaf_colors.get(label_text, "#e8edf2"),
                anchor="n",
                font=(self._ui_font_family, UI_FONT_SIZE - 1),
            )
        if self.xds_cluster_cutoff is not None:
            y = sy(self.xds_cluster_cutoff)
            canvas.create_line(left_margin, y, left_margin + plot_w, y, fill="#ff6b6b", width=2, dash=(6, 4))
            canvas.create_text(left_margin + plot_w - 4, y - 8, text=f"cutoff={self.xds_cluster_cutoff:.4g}", fill="#ff9a9a", anchor="e")
        else:
            canvas.create_text(width / 2, height - 22, text="Click dendrogram height to choose cutoff", fill="#c8d0d6")
        self.xds_dendrogram_layout = {
            "top": top_margin,
            "baseline": baseline,
            "max_distance": max_distance,
        }

    def xds_on_dendrogram_click(self, event: tk.Event) -> None:
        if not self.xds_dendrogram_layout or not self.xds_active_cluster_method:
            return
        top = float(self.xds_dendrogram_layout["top"])
        baseline = float(self.xds_dendrogram_layout["baseline"])
        max_distance = float(self.xds_dendrogram_layout["max_distance"])
        y = min(max(float(event.y), top), baseline)
        cutoff = max_distance * (baseline - y) / max(baseline - top, 1e-9)
        self.xds_cluster_cutoff = max(0.0, cutoff)
        self.xds_cluster_cutoffs[self.xds_active_cluster_method] = self.xds_cluster_cutoff
        self._notify_ai("hca_updated", result=self.xds_cluster_result, cutoff=self.xds_cluster_cutoff)
        self.xds_render_dendrogram()
        self.xds_update_cluster_preview()

    def xds_clusters_at_cutoff(self, labels: list[str], merges: list[dict[str, object]], cutoff: float | None) -> list[list[str]]:
        if cutoff is None:
            return [[label] for label in labels]
        clusters: dict[str, list[str]] = {label: [label] for label in labels}
        for merge in merges:
            distance = float(merge.get("distance", 0.0))
            if distance > cutoff:
                break
            left = str(merge.get("left", ""))
            right = str(merge.get("right", ""))
            members = [str(value) for value in merge.get("members", [])]
            if not members:
                members = clusters.get(left, []) + clusters.get(right, [])
            clusters.pop(left, None)
            clusters.pop(right, None)
            clusters[f"cluster_{merge.get('step')}"] = members
        return list(clusters.values())

    def xds_parse_cell(self, value: str | None) -> list[float] | None:
        if not value:
            return None
        nums = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
        if len(nums) < 6:
            return None
        return [float(item) for item in nums[:6]]

    def xds_cluster_status_lines(self, clusters: list[list[str]]) -> list[str]:
        rows = self.xds_summary_rows or self.xds_read_summary_rows()
        lines: list[str] = []
        for index, cluster in enumerate(clusters, 1):
            lines.append(f"Cluster {index} ({len(cluster)} dataset{'s' if len(cluster) != 1 else ''})")
            lines.append("  " + ", ".join(cluster))
            sgs = sorted({rows.get(name, {}).get("SG", "NA") for name in cluster})
            cells = [self.xds_parse_cell(rows.get(name, {}).get("Cell")) for name in cluster]
            cells = [cell for cell in cells if cell is not None]
            if cells:
                average = [sum(values) / len(values) for values in zip(*cells)]
                lines.append("  Avg cell: " + " ".join(f"{value:.2f}" for value in average))
            else:
                lines.append("  Avg cell: NA")
            lines.append("  SG: " + ", ".join(sgs))
            lines.append("")
        return lines

    def xds_update_cluster_preview(self) -> None:
        labels, merges = self.xds_current_hca()
        clusters = self.xds_clusters_at_cutoff(labels, merges, self.xds_cluster_cutoff)
        self.xds_last_clusters = clusters
        method = self.xds_active_cluster_method or "NA"
        cutoff = "not selected" if self.xds_cluster_cutoff is None else f"{self.xds_cluster_cutoff:.6g}"
        lines = [f"HCA method: {method}", f"Cutoff: {cutoff}", ""]
        lines.extend(self.xds_cluster_status_lines(clusters))
        if self.xds_cluster_cutoff is None:
            lines.append("Click the dendrogram to choose a cutoff. Merge & Conv stays disabled logically until then.")
        self.xds_show_status("\n".join(lines))

    def xds_merge_and_convert(self) -> None:
        use_hca = bool(self.xds_cluster_result and self.xds_cluster_cutoff is not None)
        if use_hca:
            clusters = [cluster for cluster in self.xds_last_clusters if len(cluster) >= 2]
            skipped = [cluster for cluster in self.xds_last_clusters if len(cluster) < 2]
            if not clusters:
                messagebox.showwarning("AutoXDS", "No multi-dataset clusters at the current cutoff.", parent=self.root)
                return
            self.xds_show_text(
                f"Merge & Conv running for {len(clusters)} multi-dataset cluster(s)...",
                f"Skipped singleton clusters: {len(skipped)}",
                keep_methods=True,
            )
        else:
            try:
                names = self.xds_selected_names()
            except Exception as exc:
                messagebox.showerror("AutoXDS", str(exc), parent=self.root)
                return
            if len(names) < 2:
                messagebox.showwarning("AutoXDS", "Merge & Conv needs at least two selected datasets.", parent=self.root)
                return
            clusters = [names]
            skipped = []
            self.xds_show_text(
                "Merge & Conv running without HCA...\n\nAll selected datasets will be merged as one group.",
                f"Merging {len(names)} selected dataset(s).",
                keep_methods=True,
            )
        root_text = str(self.xds_root())
        script_text = str(self.xds_script())
        resolution_text = self.xds_resolution_var.get().strip()
        total_clusters = len(clusters)
        self.xds_progress_begin("AutoXDS Merge & Conv", total_clusters)
        self._notify_ai(
            "operation_started",
            op="AutoXDS Merge & Conv",
            cmd=f"merge {total_clusters} cluster(s)",
        )

        def worker() -> None:
            results: list[dict[str, object]] = []
            errors: list[str] = []
            for index, cluster in enumerate(clusters, 1):
                cluster_text = ", ".join(cluster)
                self._schedule(0, lambda i=index, total=total_clusters, t=cluster_text: self.xds_progress_step(i - 1, f"Merging cluster {i}/{total}: {t}"))
                self._schedule(0, lambda i=index, total=total_clusters, t=cluster_text: self._console_log(f"[AutoXDS Merge] running {i}/{total}: {t}"))
                command = [sys.executable, script_text, "merge", "--root", root_text, "--json"]
                self.xds_append_dataset_args(command, cluster)
                command.extend(["--merge-name", f"cluster_{index}", "--run"])
                if resolution_text:
                    command.extend(["--resolution", resolution_text])
                try:
                    completed = subprocess.run(
                        [str(part) for part in command],
                        cwd=str(self.autocrys_root),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    for raw_line in (completed.stdout + "\n" + completed.stderr).splitlines():
                        if raw_line.strip():
                            self._schedule(
                                0,
                                lambda line=raw_line: self._console_log(
                                    f"[AutoXDS Merge] {line}", detail=True
                                ),
                            )
                    if completed.returncode != 0:
                        raise RuntimeError((completed.stderr or completed.stdout or "merge failed").strip())
                    result = self.xds_parse_json(completed.stdout)
                    result["cluster_index"] = index
                    result["skipped_singletons"] = skipped
                    results.append(result)
                    self._schedule(0, lambda i=index, total=total_clusters: self.xds_progress_step(i, f"Merged cluster {i}/{total}"))
                except Exception as exc:
                    errors.append(f"cluster {index}: {exc}")
                    self._schedule(0, lambda i=index, total=total_clusters: self.xds_progress_step(i, f"Cluster {i}/{total} failed"))
            self._schedule(0, lambda: self.xds_finish_merge(results, skipped, errors))

        threading.Thread(target=worker, daemon=True).start()

    def xds_finish_merge(
        self,
        results: list[dict[str, object]],
        skipped: list[list[str]],
        errors: list[str],
    ) -> None:
        lines = ["Merge & Conv summary", ""]
        status_lines: list[str] = []
        for result in results:
            index = int(result.get("cluster_index", len(status_lines) + 1))
            datasets = [str(value) for value in result.get("merged_datasets", [])]
            ahkl = result.get("ahkl", "NA")
            hkl = result.get("hkl", "NA")
            best = result.get("best_dataset", "NA")
            lines.append(
                f"cluster {index} merged from {', '.join(datasets)}, "
                f"stored as {Path(str(ahkl)).name} and converted as {Path(str(hkl)).name} at Dataset {best}"
            )
            summary = str(result.get("xscale_summary", "")).strip()
            if summary:
                lines.append("")
                lines.append(summary)
            lines.append("")
            status_lines.append(f"cluster {index}: {result.get('status', 'NA')}")
        if skipped:
            lines.append("Skipped singleton clusters")
            for cluster in skipped:
                lines.append("  " + ", ".join(cluster))
            lines.append("")
        if errors:
            lines.append("Errors")
            lines.extend(errors)
            status_lines.extend(errors)
        self.status_var.set("AutoXDS Merge & Conv complete" if not errors else "AutoXDS Merge & Conv finished with errors")
        self.xds_show_text("\n".join(lines), "\n".join(status_lines), keep_methods=True)
        self.xds_progress_end("AutoXDS Merge & Conv complete" if not errors else "AutoXDS Merge & Conv finished with errors")
        operation_result = {"results": results, "skipped_singletons": skipped, "errors": errors}
        self._notify_ai("operation_result", op="AutoXDS Merge & Conv", result=operation_result)
        self._notify_ai(
            "operation_finished",
            op="AutoXDS Merge & Conv",
            success=not errors,
            error="; ".join(errors) if errors else None,
        )

    def _build_autosolve_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(0, weight=0)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab, width=430)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        box = ttk.LabelFrame(left, text="AutoSolve", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        workdir_row = ttk.Frame(box)
        workdir_row.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Label(workdir_row, text="Working directory").grid(row=0, column=0, sticky="w")
        workdir_row.columnconfigure(0, weight=1)
        ttk.Button(workdir_row, text="Browse", command=self.solve_browse_workdir).grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.solve_workdir_entry = ttk.Entry(workdir_row, textvariable=self.solve_workdir_var)
        self.solve_workdir_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        ttk.Label(box, text="HKL file").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.solve_hkl_combo = ttk.Combobox(box, textvariable=self.solve_hkl_choice_var, state="readonly")
        self.solve_hkl_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.solve_hkl_combo.bind("<<ComboboxSelected>>", lambda _event: self.solve_on_hkl_selected())

        res_row = ttk.Frame(box)
        res_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        res_row.columnconfigure(1, weight=1)
        ttk.Label(res_row, text="Structure .res/.ins").grid(row=0, column=0, sticky="w")
        self.solve_res_entry = ttk.Entry(res_row, textvariable=self.solve_res_var)
        self.solve_res_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(res_row, text="Browse", command=self.solve_browse_res).grid(row=0, column=2, sticky="e", padx=(8, 0))

        ttk.Label(box, text="Cell").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.solve_cell_var).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(box, text="Space Group").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.solve_sg_var).grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Label(box, text="Composition").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.solve_composition_var).grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Checkbutton(
            box,
            text="Force space group",
            variable=self.solve_force_sg_var,
            command=self.solve_on_toggle_force_sg,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(
            box,
            text="Open Olex2",
            command=self.solve_open_structure_in_olex2,
        ).grid(row=6, column=2, sticky="w", pady=(10, 0))

        buttons = ttk.LabelFrame(left, text="Actions", padding=8)
        buttons.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Solve (SHELXT)", command=self.solve_run).grid(row=0, column=0, sticky="ew")
        ttk.Button(buttons, text="Refine (SHELXL)", command=self.solve_refine).grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.solve_result_tabs = ttk.Notebook(right)
        self.solve_result_tabs.grid(row=0, column=0, sticky="nsew")
        if self._ai_config.structure_viewer_enabled:
            structure_tab = ttk.Frame(self.solve_result_tabs, padding=8)
            structure_tab.columnconfigure(0, weight=1)
            structure_tab.rowconfigure(0, weight=1)
            self.solve_result_tabs.add(structure_tab, text="Structure")
            self.structure_viewer = StructureViewer(
                structure_tab,
                show_unit_cell=self._ai_config.structure_viewer_show_unit_cell,
                bond_tolerance=self._ai_config.structure_viewer_bond_tolerance,
                grow_steps_per_click=self._ai_config.structure_viewer_grow_steps,
            )
            self.structure_viewer.grid(row=0, column=0, sticky="nsew")

        self.solve_output_tab = ttk.Frame(self.solve_result_tabs, padding=8)
        self.solve_output_tab.columnconfigure(0, weight=1)
        self.solve_output_tab.rowconfigure(1, weight=1)
        self.solve_result_tabs.add(self.solve_output_tab, text="Output")
        ttk.Label(self.solve_output_tab, text="SHELXT Output").grid(row=0, column=0, sticky="w")
        self.solve_preview = tk.Text(
            self.solve_output_tab,
            height=18,
            wrap="word",
            background=UI_PANEL,
            foreground=UI_TEXT,
            insertbackground=UI_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            highlightcolor=UI_ACCENT,
            font=self._console_font,
            padx=10,
            pady=8,
        )
        self.solve_preview.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self._set_preview(self.solve_preview, None)
        return tab

    def solve_log(self, message: str) -> None:
        self._console_log(f"[AutoSolve] {message}", detail=True)

    def solve_browse_workdir(self) -> None:
        initial = _normalize_path(self.solve_workdir_var.get())
        initial_dir = str(initial if initial.exists() else self.autocrys_root)
        selected = filedialog.askdirectory(initialdir=initial_dir, parent=self.root)
        if not selected:
            return
        dataset = Path(selected)
        self.solve_workdir_var.set(str(dataset))
        self.solve_dataset_var.set(dataset.name)
        try:
            self.solve_scan_hkl_files()
        except Exception as exc:
            self.solve_hkl_choices = []
            self.solve_hkl_combo.configure(values=[])
            self.solve_hkl_choice_var.set("")
            self.solve_hkl_var.set("")
            self.solve_log(f"Browse failed: {exc}")
            messagebox.showerror("AutoSolve", str(exc), parent=self.root)

    def solve_browse_res(self) -> None:
        initial = _normalize_path(self.solve_res_var.get())
        initial_dir = str(initial.parent if initial.exists() else _normalize_path(self.solve_workdir_var.get()))
        selected = filedialog.askopenfilename(
            initialdir=initial_dir if Path(initial_dir).exists() else str(self.autocrys_root),
            filetypes=[("SHELX structure", "*.res *.ins"), ("SHELX result", "*.res"), ("SHELX input", "*.ins"), ("All files", "*.*")],
            parent=self.root,
        )
        if selected:
            self.solve_res_var.set(selected)
            self.solve_load_structure(show_error=True)

    def solve_load_structure(self, show_error: bool = False) -> None:
        if not hasattr(self, "structure_viewer"):
            return
        path = _normalize_path(self.solve_res_var.get())
        if not path.is_file() or path.suffix.lower() not in {".res", ".ins"}:
            if show_error:
                messagebox.showerror("Structure viewer", "Choose an existing .res or .ins file.", parent=self.root)
            return
        try:
            model = self.structure_viewer.load(path)
        except (OSError, ValueError, RuntimeError) as exc:
            self.structure_viewer.show_error(str(exc))
            self.solve_log(f"Structure viewer: {exc}")
            if show_error:
                messagebox.showerror("Structure viewer", str(exc), parent=self.root)
            return
        self.solve_log(f"Model loaded: {path.name}; {len(model.atoms)} atoms; {len(model.bonds)} bonds")
        self.solve_result_tabs.select(self.structure_viewer.master)

    def solve_choose_dataset_dir(self, candidates: list[Path]) -> Path | None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Select dataset folder")
        dialog.geometry("560x380")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        listbox = tk.Listbox(
            dialog,
            selectmode="browse",
            background=UI_PANEL,
            foreground=UI_TEXT,
            selectbackground=UI_ACCENT,
            selectforeground="#ffffff",
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_BORDER,
            highlightcolor=UI_ACCENT,
        )
        listbox.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        for candidate in candidates:
            listbox.insert("end", str(candidate))
        if candidates:
            listbox.selection_set(0)

        chosen: list[Path] = []

        def accept() -> None:
            selection = listbox.curselection()
            if selection:
                chosen.append(candidates[int(selection[0])])
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Select", command=accept).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).grid(row=0, column=2, padx=(8, 0))
        self.root.wait_window(dialog)
        return chosen[0] if chosen else None

    def dials_to_r3d(self, dataset: Path, run: Path) -> None:
        self.dataset_var.set(str(dataset))
        self.r3d_source_var.set("dials")
        self.r3d_run_var.set(str(run))
        self.output_var.set(str(run / "AutoR3D"))
        self._output_path_is_custom = True
        self._notebook.select(self._workflow_tabs["AutoR3D"])
        self.import_dataset()

    def dials_to_solve(self, hkl: Path) -> None:
        self.solve_workdir_var.set(str(hkl.parent))
        self.solve_scan_hkl_files()
        index = self.solve_hkl_choices.index(hkl)
        self.solve_hkl_choice_var.set(self.solve_hkl_combo.cget("values")[index])
        self.solve_on_hkl_selected()
        self._notebook.select(self._workflow_tabs["AutoSolve"])
        self.solve_log("DIALS result selected. Enter the real unit-cell composition before solving.")

    def solve_scan_hkl_files(self) -> None:
        selected = _normalize_path(self.solve_workdir_var.get())
        if not selected.exists():
            raise FileNotFoundError(f"Working directory does not exist: {selected}")

        if (selected / "summary.json").exists() or (selected / "diff").exists():
            dataset = selected
        else:
            candidates = [
                child
                for child in sorted(selected.iterdir())
                if child.is_dir() and (child / "diff").exists()
            ]
            if not candidates:
                raise FileNotFoundError(
                    f"No dataset folder with diff/p found under: {selected}. Please choose a specific dataset folder."
                )
            if len(candidates) == 1:
                dataset = candidates[0]
            else:
                chosen = self.solve_choose_dataset_dir(candidates)
                if chosen is None:
                    raise ValueError("Dataset selection cancelled")
                dataset = chosen

        self.solve_workdir_var.set(str(dataset))
        self.solve_dataset_var.set(dataset.name)
        p_dir = dataset / "diff" / "p"
        from AutoDials.service import successful_runs
        files = sorted(p_dir.glob("*.hkl")) if p_dir.exists() else []
        if (dataset / "summary.json").is_file():
            files = sorted(dataset.glob("*.hkl"))
        else:
            for run in successful_runs(dataset, require_complete=False):
                files.extend(sorted(run.glob("*.hkl")))
        self.solve_hkl_choices = files
        values = [str(path.relative_to(dataset)) for path in files]
        self.solve_hkl_combo.configure(values=values, state="readonly")
        if not files:
            self.solve_hkl_choice_var.set("")
            self.solve_hkl_var.set("")
            self.solve_log(f"Found 0 .hkl files in {p_dir}")
            raise FileNotFoundError(f"No case-sensitive .hkl files found in {p_dir}")
        self.solve_hkl_choice_var.set(values[0])
        self.solve_hkl_var.set(str(files[0]))
        self.solve_log("Found .hkl files: " + ", ".join(values))
        self.solve_on_hkl_selected()

    def solve_on_hkl_selected(self) -> None:
        if not self.solve_hkl_choices:
            return
        selected = self.solve_hkl_choice_var.get().strip()
        values = list(self.solve_hkl_combo.cget("values"))
        match = self.solve_hkl_choices[values.index(selected)] if selected in values else self.solve_hkl_choices[0]
        self.solve_hkl_var.set(str(match))
        self.solve_basename_var.set(match.stem)
        self.solve_workdir_var.set(str(match.parent if (match.parent / "summary.json").exists() else match.parent.parent.parent))
        self.solve_res_var.set("")
        candidates = [match.with_suffix(".res"), match.with_name(f"{match.stem}_a.res")]
        found = next((path for path in candidates if path.exists()), None)
        if found:
            self.solve_res_var.set(str(found))
        self.solve_refresh_cell_sg(match)
        if found:
            self.solve_load_structure()

    def solve_refresh_cell_sg(self, selected_hkl: Path) -> None:
        self.solve_cell_var.set("")
        self.solve_sg_var.set("")
        metadata = reflection_metadata(selected_hkl)
        if metadata:
            self.solve_cell_var.set(" ".join(f"{v:.6f}" for v in metadata["cell"]))
            self.solve_sg_var.set(str(metadata["space_group_number"]))
            self.solve_log(f"DIALS final cell / space group: {metadata['source']}")
            return
        xds_ascii = selected_hkl.parent / "XDS_ASCII.HKL"
        same_name_ahkl = selected_hkl.with_suffix(".ahkl")
        source = xds_ascii if xds_ascii.exists() else same_name_ahkl
        sg, cell = _parse_xds_header_fields(source)
        if sg:
            self.solve_sg_var.set(sg)
        if cell:
            self.solve_cell_var.set(cell)
        self.solve_log(
            f"Header source: {source.name}; SPACE_GROUP_NUMBER={sg or 'NA'}; UNIT_CELL_CONSTANTS={cell or 'NA'}"
        )

    def solve_on_toggle_force_sg(self) -> None:
        if self.solve_force_sg_var.get():
            self.solve_log(f"space group restrained to: {self.solve_sg_var.get().strip() or 'NA'}")
        else:
            self.solve_log("space group restraint disabled")

    def solve_open_structure_in_olex2(self) -> None:
        try:
            if not AUTO_SOLVE_OLEX2_DEFAULT.is_file():
                raise FileNotFoundError(f"Olex2 executable not found: {AUTO_SOLVE_OLEX2_DEFAULT}")
            if not hasattr(self, "structure_viewer") or self.structure_viewer.model is None:
                raise ValueError("No structure is currently displayed in the Structure tab.")
            result = self.structure_viewer.model.source.resolve()
            if not result.is_file() or result.suffix.lower() != ".res":
                raise ValueError("The structure currently displayed is not an existing .res file.")
            target = external_target_path(AUTO_SOLVE_OLEX2_DEFAULT, result)
            subprocess.Popen(
                [str(AUTO_SOLVE_OLEX2_DEFAULT), target],
                start_new_session=True,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self.status_var.set(f"Open Olex2 failed: {exc}")
            self.solve_log(f"Open Olex2 failed: {exc}")
            messagebox.showerror("Open Olex2", str(exc), parent=self.root)
            return
        self.status_var.set(f"Opened {result.name} in Olex2")
        self.solve_log(f"Opened displayed RES in Olex2: {result}")

    def solve_refine(self) -> None:
        try:
            res_path = _normalize_path(self.solve_res_var.get())
            hkl_path = _normalize_path(self.solve_hkl_var.get())
            if not res_path.exists() or res_path.suffix.lower() != ".res":
                raise ValueError("Choose an existing initial .res file before refining.")
            if not hkl_path.exists() or hkl_path.suffix.lower() != ".hkl":
                raise ValueError("Choose the matching .hkl file before refining.")
            command = [
                sys.executable,
                str(self.autocrys_root / "AutoSolve" / "scripts" / "auto_shelxl.py"),
                "--res", str(res_path),
                "--hkl", str(hkl_path),
                "--json",
            ]
        except Exception as exc:
            messagebox.showerror("SHELXL refinement", str(exc), parent=self.root)
            self.status_var.set(f"SHELXL setup failed: {exc}")
            return
        self._set_preview(self.solve_preview, command)
        self._run_command_async("SHELXL refinement", command)

    def solve_run(self) -> None:
        try:
            hkl_path = _normalize_path(self.solve_hkl_var.get())
            if not hkl_path.exists() or hkl_path.suffix != ".hkl":
                raise ValueError("Select a valid case-sensitive .hkl file from dataset/diff/p")
            composition = self.solve_composition_var.get().strip()
            if not composition:
                raise ValueError("Composition is required")
            workdir = hkl_path.parent
            basename = hkl_path.stem
            sg = self.solve_sg_var.get().strip()
            cell = self.solve_cell_var.get().strip()
            command = [
                sys.executable,
                str(self.autocrys_root / "AutoSolve" / "scripts" / "auto_shelxt.py"),
                "prepare",
                "--json",
                "--hkl",
                str(hkl_path),
                "--workdir",
                str(workdir),
                "--basename",
                basename,
                "--composition",
                composition,
            ]
            if cell:
                command.extend(["--cell", cell])
            if self.solve_force_sg_var.get():
                if not sg:
                    raise ValueError("Force space group enabled but SG is empty")
                command.extend(["--sg", sg])
            else:
                command.append("--no-force-sg")
        except Exception as exc:
            messagebox.showerror("AutoSolve", str(exc), parent=self.root)
            return

        self.status_var.set("AutoSolve preparing INS and running SHELXT...")
        self._set_preview(self.solve_preview, None)
        self.solve_result_tabs.select(self.solve_output_tab)
        self.solve_log(f"Using cell {cell or 'NA'}, SG {sg or 'NA'} to start solve")
        if self.solve_force_sg_var.get():
            self.solve_log(f"space group restrained to: {sg}")

        def worker() -> None:
            try:
                prepared = subprocess.run(
                    [str(part) for part in command],
                    cwd=str(self.autocrys_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if prepared.returncode != 0:
                    raise RuntimeError((prepared.stderr or prepared.stdout or "prepare failed").strip())
                payload = self.xds_parse_json(prepared.stdout)
                shelxt_sg = str(payload.get("shelxt_sg", "")).strip()
                shelxt_command = ["shelxt", basename]
                if shelxt_sg and shelxt_sg != "NA":
                    shelxt_command.append(f"-s{shelxt_sg}")
                run = subprocess.run(
                    shelxt_command,
                    cwd=str(workdir),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                output = (run.stdout or "") + ("\n" + run.stderr if run.stderr else "")
                def show_output() -> None:
                    self.solve_preview.configure(state="normal")
                    self.solve_preview.delete("1.0", "end")
                    self.solve_preview.insert("end", output.strip() + "\n")
                    self.solve_preview.see("end")
                    self.solve_preview.configure(state="disabled")

                self._schedule(0, show_output)
                if run.returncode != 0:
                    raise RuntimeError(f"SHELXT exited with code {run.returncode}")
                res = workdir / f"{basename}.res"
                if not res.exists():
                    for suffix in "abcdefghij":
                        candidate = workdir / f"{basename}_{suffix}.res"
                        if candidate.exists():
                            res = candidate
                            break
                self._schedule(0, lambda result=res: self.solve_finish_ok(cell, sg, result))
            except Exception as exc:
                self._schedule(0, lambda error=exc: self.solve_finish_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def solve_finish_ok(self, cell: str, sg: str, result_path: Path | None = None) -> None:
        self.status_var.set("AutoSolve solve complete")
        self.solve_log(f"Tried solve with cell {cell or 'NA'}, SG {sg or 'NA'}")
        if result_path is not None and result_path.exists():
            self.solve_res_var.set(str(result_path))
            self.solve_load_structure()
        self.solve_log("solve complete")

    def solve_finish_error(self, error: Exception) -> None:
        self.status_var.set(f"AutoSolve failed: {error}")
        self.solve_log(f"solve failed: {error}")
        messagebox.showerror("AutoSolve", str(error), parent=self.root)

    def _build_peak_search_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(0, weight=0)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab, width=350)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self._build_workdir_controls(left).grid(row=0, column=0, sticky="ew")
        self._build_peak_controls(left).grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(left, textvariable=self.frame_info_var, anchor="w", wraplength=320).grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(12, 0),
        )

        self.display_outer = ttk.Frame(right)
        self.display_outer.grid(row=0, column=0, sticky="nsew")
        self.display_outer.bind("<Configure>", self._resize_square_canvas)

        self.canvas = tk.Canvas(self.display_outer, background="#0b0d10", highlightthickness=0)
        self.canvas.place(relx=0.5, rely=0.5, anchor="center", width=516, height=516)
        self.canvas.bind("<Configure>", lambda _event: self.render_frame())

        controls = ttk.Frame(right)
        controls.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Frame").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.frame_scale = ttk.Scale(
            controls,
            from_=0,
            to=0,
            orient="horizontal",
            variable=self.frame_index_var,
            command=self._on_frame_scale,
        )
        self.frame_scale.grid(row=0, column=1, sticky="ew")
        self.frame_label = ttk.Label(controls, text="0 / 0", width=12, anchor="e")
        self.frame_label.grid(row=0, column=2, sticky="e", padx=(8, 0))

        self._add_display_slider(controls, "Brightness", self.brightness_var, 0.1, 4.0, 1)
        self._add_display_slider(controls, "Contrast", self.contrast_var, 0.1, 4.0, 2)
        self._add_display_slider(controls, "Gamma", self.gamma_var, 0.2, 3.0, 3)
        return tab

    def _build_rotation_axis_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(0, weight=0)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab, width=350)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        box = ttk.LabelFrame(left, text="Rotation Axis (XDS)", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)
        ttk.Label(
            box,
            textvariable=self.axis_info_var,
            wraplength=315,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(box, text="Axis (AutoR3D deg)").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(box, textvariable=self.axis_var, width=10).grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(8, 0),
            pady=(12, 0),
        )
        ttk.Button(box, text="Set Axis", command=self.set_axis).grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(8, 0),
        )

        self.axis_canvas = tk.Canvas(right, background="#0b0d10", highlightthickness=0)
        self.axis_canvas.grid(row=0, column=0, sticky="nsew")
        self.axis_canvas.bind("<Configure>", lambda _event: self._render_axis_projection())

        self._axis_log("Run Peak Search first, then use Global Search, Refine Axis, or Set Axis.")
        return tab

    def _build_reconstruction_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(0, weight=0)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)
        left = ttk.Frame(tab, width=350)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        section = ttk.LabelFrame(left, text="Reconstruction", padding=8)
        section.grid(row=0, column=0, sticky="ew")
        section.columnconfigure(1, weight=1)

        ttk.Label(section, text="Dataset").grid(row=0, column=0, sticky="w")
        ttk.Entry(section, textvariable=self.dataset_var).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(section, text="Browse Dataset", command=self.browse_dataset).grid(
            row=1, column=0, sticky="ew", pady=(8, 0)
        )
        ttk.Button(section, text="Import Dataset", command=self.import_dataset).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(section, text="Output").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(section, textvariable=self.output_var).grid(
            row=2, column=1, sticky="ew", padx=(8, 0), pady=(12, 0)
        )
        ttk.Button(section, text="Browse", command=self.browse_output).grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(8, 0),
        )

        ttk.Label(section, text="Voxel").grid(row=4, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(section, textvariable=self.voxel_size_var, width=8).grid(
            row=4,
            column=1,
            sticky="ew",
            padx=(8, 0),
            pady=(12, 0),
        )
        ttk.Label(section, text="Max peaks").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(section, textvariable=self.max_peaks_var, width=8).grid(
            row=5,
            column=1,
            sticky="ew",
            padx=(8, 0),
            pady=(8, 0),
        )
        ttk.Label(section, textvariable=self.reconstruction_info_var, anchor="w", wraplength=315).grid(
            row=6,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 0),
        )
        self.reconstruction_button = ttk.Button(
            section,
            text="Run Reconstruction",
            command=self.run_reconstruction,
            state="disabled",
        )
        self.reconstruction_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.open_panel_button = ttk.Button(section, text="Load 3D Panel", command=self.open_panel, state="disabled")
        self.open_panel_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.reconstruction_progress = ttk.Progressbar(section, orient="horizontal", mode="indeterminate")
        self.reconstruction_progress.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(section, text="Geometry source").grid(row=10, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(section, textvariable=self.r3d_source_var, values=("auto", "xds", "dials", "manual"), state="readonly", width=15).grid(row=10, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(section, text="DIALS run").grid(row=11, column=0, sticky="w")
        ttk.Entry(section, textvariable=self.r3d_run_var, width=16).grid(row=11, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(section, text="Blank run = latest. Re-import after changing source.", wraplength=315).grid(row=12, column=0, columnspan=2, sticky="w")

        viewer_tabs = ttk.Notebook(right)
        viewer_tabs.grid(row=0, column=0, sticky="nsew")
        viewer = ttk.Frame(viewer_tabs)
        viewer.columnconfigure(0, weight=1)
        viewer.rowconfigure(1, weight=1)
        viewer_tabs.add(viewer, text="3D Panel")
        slices_tab = ttk.Frame(viewer_tabs, padding=8)
        viewer_tabs.add(slices_tab, text="Slices")
        self._build_slices_subtab(slices_tab)

        panel_controls = ttk.Frame(viewer)
        panel_controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.embedded_panel_controls = panel_controls
        for col in range(12):
            panel_controls.columnconfigure(col, weight=1)

        ttk.Label(panel_controls, text="Threshold").grid(row=0, column=0, sticky="w", pady=(6, 0))
        self.embedded_threshold_scale = ttk.Scale(
            panel_controls,
            from_=0.0,
            to=100.0,
            orient="horizontal",
            variable=self.embedded_threshold_var,
            command=self._on_embedded_threshold,
        )
        self.embedded_threshold_scale.grid(
            row=0, column=1, columnspan=10, sticky="ew", padx=(6, 10), pady=(6, 0)
        )
        ttk.Label(
            panel_controls,
            textvariable=self.embedded_threshold_label_var,
            width=5,
            anchor="e",
        ).grid(row=0, column=11, sticky="e", pady=(6, 0))

        self._add_embedded_slider(
            panel_controls, "Size", self.embedded_point_size_var, 1.0, 10.0, 1, 0, columnspan=2
        )
        self._add_embedded_slider(
            panel_controls, "Brightness", self.embedded_brightness_var, 0.0, 3.0, 1, 3, columnspan=2
        )
        self._add_embedded_slider(
            panel_controls, "Contrast", self.embedded_contrast_var, 0.1, 3.0, 1, 6, columnspan=2
        )
        self._add_embedded_slider(
            panel_controls, "Gamma", self.embedded_gamma_var, 0.2, 3.0, 1, 9, columnspan=2
        )
        ttk.Label(panel_controls, text="View").grid(row=2, column=0, sticky="w", pady=(6, 0))
        for column, (label, axis) in enumerate((("a* (A)", "a"), ("b* (B)", "b"), ("c* (C)", "c")), start=1):
            ttk.Button(
                panel_controls,
                text=label,
                command=lambda selected=axis: self.set_embedded_axis_view(selected),
            ).grid(row=2, column=column, sticky="ew", padx=(4, 0), pady=(6, 0))
        ttk.Button(panel_controls, text="Iso (I)", command=lambda: self.set_embedded_axis_view("iso")).grid(
            row=2, column=4, sticky="ew", padx=(4, 0), pady=(6, 0)
        )
        ttk.Button(panel_controls, text="Fit (F)", command=self.fit_embedded_panel_view).grid(
            row=2, column=5, sticky="ew", padx=(4, 0), pady=(6, 0)
        )
        ttk.Checkbutton(
            panel_controls,
            text="Cell/grid",
            variable=self.embedded_cell_var,
            command=self.render_embedded_panel,
        ).grid(row=2, column=6, columnspan=2, sticky="w", padx=(10, 0), pady=(6, 0))
        self.embedded_indexed_check = ttk.Checkbutton(
            panel_controls,
            text="Indexed",
            variable=self.embedded_indexed_var,
            command=self.render_embedded_panel,
            state="disabled",
        )
        self.embedded_indexed_check.grid(
            row=2, column=8, columnspan=2, sticky="w", padx=(10, 0), pady=(6, 0)
        )
        self.reconstruction_canvas = tk.Canvas(viewer, background="#0b0d10", highlightthickness=0)
        self.reconstruction_canvas.grid(row=1, column=0, sticky="nsew")
        self.reconstruction_canvas.bind("<Configure>", lambda _event: self.render_embedded_panel())
        self.reconstruction_canvas.bind("<ButtonPress-1>", self._embedded_drag_start)
        self.reconstruction_canvas.bind("<B1-Motion>", self._embedded_drag_move)
        self.reconstruction_canvas.bind("<ButtonPress-2>", self._embedded_pan_start_event)
        self.reconstruction_canvas.bind("<B2-Motion>", self._embedded_pan_move)
        self.reconstruction_canvas.bind("<ButtonPress-3>", self._embedded_pan_start_event)
        self.reconstruction_canvas.bind("<B3-Motion>", self._embedded_pan_move)
        self.reconstruction_canvas.bind("<MouseWheel>", self._embedded_zoom)
        self.reconstruction_canvas.bind("<Button-4>", lambda event: self._embedded_zoom_steps(event, 1))
        self.reconstruction_canvas.bind("<Button-5>", lambda event: self._embedded_zoom_steps(event, -1))
        self.reconstruction_canvas.bind("<Double-Button-1>", lambda _event: self.reset_embedded_panel_view())
        self.reconstruction_canvas.bind("<KeyPress>", self._embedded_keypress)

        self._reconstruction_log(
            "Ready. This run uses peak picking with a circular 12 px local maximum footprint, "
            "center mask 15 px, center peak exclusion 20 px, and the current rotation axis."
        )
        self.render_embedded_panel()
        return tab

    def _build_slices_subtab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew")
        for col in range(12):
            controls.columnconfigure(col, weight=1)

        ttk.Label(controls, text="Family").grid(row=0, column=0, sticky="w")
        family = ttk.Combobox(
            controls,
            textvariable=self.slice_family_var,
            values=SLICE_FAMILIES,
            width=6,
            state="readonly",
        )
        family.grid(row=0, column=1, sticky="ew", padx=(6, 10))
        ttk.Label(controls, text="Layer").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.slice_layer_var, width=5).grid(
            row=0, column=3, sticky="ew", padx=(6, 10)
        )
        ttk.Label(controls, text="Thickness r.l.u.").grid(row=0, column=4, sticky="w")
        ttk.Entry(controls, textvariable=self.slice_thickness_var, width=7).grid(
            row=0, column=5, sticky="ew", padx=(6, 10)
        )
        ttk.Label(controls, text="Step r.l.u.").grid(row=0, column=6, sticky="w")
        ttk.Entry(controls, textvariable=self.slice_step_var, width=7).grid(
            row=0, column=7, sticky="ew", padx=(6, 10)
        )
        ttk.Button(controls, text="Preview", command=self.preview_slice).grid(
            row=0, column=8, sticky="ew", padx=(6, 6)
        )
        ttk.Button(controls, text="Export PNG", command=self.export_slice_png).grid(row=0, column=9, sticky="ew")
        ttk.Checkbutton(
            controls,
            text="Cell/grid",
            variable=self.slice_cell_var,
            command=self._rerender_current_slice,
        ).grid(row=0, column=10, columnspan=2, sticky="w", padx=(10, 0))
        ttk.Label(controls, text="Threshold").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Scale(
            controls,
            from_=0.0,
            to=100.0,
            orient="horizontal",
            variable=self.slice_threshold_var,
            command=self._on_slice_threshold,
        ).grid(row=1, column=1, columnspan=10, sticky="ew", padx=(6, 10), pady=(8, 0))
        ttk.Label(controls, textvariable=self.slice_threshold_label_var, width=5, anchor="e").grid(
            row=1, column=11, sticky="e", pady=(8, 0)
        )

        ttk.Label(parent, textvariable=self.slice_info_var, anchor="w", wraplength=980).grid(
            row=1, column=0, sticky="ew", pady=(8, 6)
        )
        self.slice_canvas = tk.Canvas(parent, background="#0b0d10", highlightthickness=0)
        self.slice_canvas.grid(row=2, column=0, sticky="nsew")
        self.slice_canvas.bind("<Configure>", lambda _event: self._render_slice_canvas())
        self.slice_canvas.bind("<MouseWheel>", self._slice_zoom_event)
        self.slice_canvas.bind("<Button-4>", lambda event: self._slice_zoom_steps(event, 1))
        self.slice_canvas.bind("<Button-5>", lambda event: self._slice_zoom_steps(event, -1))
        self.slice_canvas.bind("<Double-Button-1>", lambda _event: self._reset_slice_zoom())

    def _slice_spec(self) -> SliceSpec:
        layer = _integer_from_var(self.slice_layer_var, "Slice layer")
        thickness = _float_from_var(self.slice_thickness_var, "Slice thickness")
        step = _float_from_var(self.slice_step_var, "Slice step")
        return SliceSpec(
            family=self.slice_family_var.get(),
            layer=layer,
            thickness_rlu=thickness,
            step_rlu=step,
        )

    def _ensure_indexed_observations(self) -> bool:
        if self._indexed_hkl is not None and self._indexed_intensity is not None:
            return True
        path = self.indexed_observations_path
        if path is None or not Path(path).is_file():
            return False
        with np.load(path) as data:
            self._indexed_hkl = data["hkl"].astype(np.float64)
            self._indexed_intensity = data["intensity"].astype(np.float64)
        return True

    def preview_slice(self) -> None:
        try:
            if not self._ensure_indexed_observations():
                raise RuntimeError("Run Reconstruction on a dataset with XDS orientation first.")
            spec = self._slice_spec()
            data = build_slice(self._indexed_hkl, self._indexed_intensity, spec)
            self._slice_data = data
            self.slice_zoom = 1.0
            self._slice_image = render_slice_image(
                data,
                show_cell=bool(self.slice_cell_var.get()),
                threshold_percent=float(self.slice_threshold_var.get()),
            )
            self._render_slice_canvas()
            self.slice_info_var.set(
                f"{slice_title(data)} | points in slab: {data.point_count} | "
                f"grid: {data.shape[1]} x {data.shape[0]} | "
                f"effective step: {data.effective_step_rlu:.4g} r.l.u. | "
                f"total intensity: {data.total_intensity:.4g}"
            )
        except Exception as exc:
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Slice preview failed: {exc}")

    def _rerender_current_slice(self) -> None:
        data = getattr(self, "_slice_data", None)
        if data is None:
            return
        self._slice_image = render_slice_image(
            data,
            show_cell=bool(self.slice_cell_var.get()),
            threshold_percent=float(self.slice_threshold_var.get()),
        )
        self._render_slice_canvas()

    def _on_slice_threshold(self, value: str) -> None:
        self.slice_threshold_label_var.set(f"{float(value):.0f}%")
        self._rerender_current_slice()

    def _render_slice_canvas(self) -> None:
        image = getattr(self, "_slice_image", None)
        if not hasattr(self, "slice_canvas") or image is None:
            return
        canvas = self.slice_canvas
        width = max(1, int(canvas.winfo_width()))
        height = max(1, int(canvas.winfo_height()))
        if image.width <= 0 or image.height <= 0:
            return
        scale = max(0.05, min(width / image.width, height / image.height)) * self.slice_zoom
        resized = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.NEAREST,
        )
        self._slice_photo = ImageTk.PhotoImage(resized)
        canvas.delete("all")
        scaled_header = resized.height * SLICE_HEADER_HEIGHT / max(image.height, 1)
        canvas.create_image(
            width // 2,
            height / 2.0 - scaled_header / 2.0,
            image=self._slice_photo,
            anchor="center",
        )

    def _slice_zoom_event(self, event: tk.Event) -> None:
        self.slice_canvas.focus_set()
        self._slice_zoom_steps(event, 1 if getattr(event, "delta", 0) > 0 else -1)

    def _slice_zoom_steps(self, _event: tk.Event, steps: int) -> None:
        factor = 1.15 if steps > 0 else 1.0 / 1.15
        self.slice_zoom = float(np.clip(self.slice_zoom * factor, 0.25, 12.0))
        self._render_slice_canvas()

    def _reset_slice_zoom(self) -> None:
        self.slice_zoom = 1.0
        self._render_slice_canvas()

    def export_slice_png(self) -> None:
        try:
            if not self._ensure_indexed_observations():
                raise RuntimeError("Run Reconstruction on a dataset with XDS orientation first.")
            spec = self._slice_spec()
            data = build_slice(self._indexed_hkl, self._indexed_intensity, spec)
            selected = filedialog.asksaveasfilename(
                parent=self.root,
                title="Export slice",
                initialfile=f"slice_{spec.family}_{spec.layer}.png",
                defaultextension=".png",
                filetypes=[("PNG image", "*.png")],
            )
            if not selected:
                return
            render_slice_image(
                data,
                show_cell=bool(self.slice_cell_var.get()),
                threshold_percent=float(self.slice_threshold_var.get()),
            ).save(selected)
            save_slice_npz(Path(selected).with_suffix(".npz"), data)
            self.status_var.set(f"Slice exported: {selected}")
        except Exception as exc:
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Slice export failed: {exc}")

    def _build_workdir_controls(self, parent: ttk.Frame) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="Work Directory", padding=8)
        box.columnconfigure(0, weight=1)
        self.path_entry = ttk.Entry(box, textvariable=self.dataset_var)
        self.path_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(box, text="Browse", command=self.browse_dataset).grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(box, text="Import", command=self.import_dataset).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        return box

    def _build_peak_controls(self, parent: ttk.Frame) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="Peak Parameters", padding=8)
        for col in range(4):
            box.columnconfigure(col, weight=1)

        ttk.Label(box, text="Center X").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.center_x_var, width=8).grid(row=0, column=1, sticky="ew", padx=(6, 8))
        ttk.Label(box, text="Center Y").grid(row=0, column=2, sticky="w")
        ttk.Entry(box, textvariable=self.center_y_var, width=8).grid(row=0, column=3, sticky="ew", padx=(6, 0))

        ttk.Label(box, text="Mask radius").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.mask_radius_var, width=8).grid(
            row=1,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(6, 0),
            pady=(8, 0),
        )

        ttk.Label(box, text="Edge guard").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.mask_edge_guard_var, width=8).grid(
            row=2,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(6, 0),
            pady=(8, 0),
        )

        bad = ttk.LabelFrame(box, text="Bad Points", padding=6)
        bad.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        bad.columnconfigure(0, weight=1)
        ttk.Button(bad, text="Configure", state="disabled").grid(row=0, column=0, sticky="ew")

        ttk.Label(box, text="Spot size").grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(box, textvariable=self.spot_size_var, width=8).grid(
            row=4,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(6, 0),
            pady=(10, 0),
        )
        ttk.Label(box, text="Threshold").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.threshold_var, width=8).grid(
            row=5,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(6, 0),
            pady=(8, 0),
        )
        self.peak_search_button = ttk.Button(box, text="Peak Search", command=self.run_peak_search)
        self.peak_search_button.grid(
            row=6,
            column=0,
            columnspan=4,
            sticky="ew",
            pady=(12, 0),
        )
        self.peak_progress = ttk.Progressbar(
            box,
            orient="horizontal",
            mode="determinate",
            maximum=100.0,
            variable=self.peak_progress_var,
        )
        self.peak_progress.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        return box

    def _add_display_slider(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.DoubleVar,
        low: float,
        high: float,
        row: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        scale = ttk.Scale(
            parent,
            from_=low,
            to=high,
            orient="horizontal",
            variable=variable,
            command=lambda _value: self.render_frame(),
        )
        scale.grid(row=row, column=1, sticky="ew", pady=(6, 0))
        value_label = ttk.Label(parent, width=6, anchor="e")
        value_label.grid(row=row, column=2, sticky="e", padx=(8, 0), pady=(6, 0))

        def update_label(*_args: object) -> None:
            value_label.configure(text=f"{variable.get():.2f}")

        variable.trace_add("write", update_label)
        update_label()

    def _add_embedded_slider(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.DoubleVar,
        low: float,
        high: float,
        row: int,
        column: int,
        *,
        columnspan: int = 1,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", pady=(6, 0))
        scale = ttk.Scale(
            parent,
            from_=low,
            to=high,
            orient="horizontal",
            variable=variable,
            command=lambda _value: self.render_embedded_panel(),
        )
        scale.grid(
            row=row,
            column=column + 1,
            columnspan=columnspan,
            sticky="ew",
            padx=(6, 10),
            pady=(6, 0),
        )

    def _on_embedded_threshold(self, value: str) -> None:
        self.embedded_threshold_label_var.set(f"{float(value):.0f}%")
        self.render_embedded_panel()

    def _console_log(self, text: str, clear: bool = False, *, detail: bool = False) -> None:
        if not hasattr(self, "console_log"):
            return
        if clear:
            self.console_log.delete("1.0", "end")
        if self.ai_assistant_enabled:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped:
                    self._notify_ai("console_output", line=stripped)

        rendered = text.rstrip()
        if detail and not self.console_verbose:
            important = [line for line in rendered.splitlines() if IMPORTANT_CONSOLE_LINE.search(line)]
            rendered = "\n".join(important)
        if not rendered:
            if self.console_verbose and not text:
                self.console_log.insert("end", "\n")
                self.console_log.see("end")
            return
        self.console_log.insert("end", rendered + "\n")
        self.console_log.see("end")

    def _reconstruction_log(self, text: str, clear: bool = False) -> None:
        self._console_log(text, clear=clear)

    def _browse_directory_var(self, variable: tk.StringVar) -> None:
        initial = _normalize_path(variable.get())
        initial_dir = str(initial if initial.exists() else self.autocrys_root)
        selected = filedialog.askdirectory(initialdir=initial_dir, parent=self.root)
        if selected:
            variable.set(selected)

    def _browse_file_var(self, variable: tk.StringVar, filetypes: list[tuple[str, str]]) -> None:
        initial = _normalize_path(variable.get())
        initial_dir = str(initial.parent if initial.exists() else self.autocrys_root)
        selected = filedialog.askopenfilename(initialdir=initial_dir, filetypes=filetypes, parent=self.root)
        if selected:
            variable.set(selected)

    def _command_text(self, command: list[str]) -> str:
        return subprocess.list2cmdline([str(part) for part in command])

    def _set_preview(self, widget: tk.Text, command: list[str] | None, error: Exception | None = None) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if error is not None:
            widget.insert("end", f"Cannot build command:\n{error}\n")
        elif command is not None:
            widget.insert("end", self._command_text(command) + "\n")
        widget.configure(state="disabled")

    def _update_xds_preview(self, action: str) -> None:
        if not hasattr(self, "xds_preview"):
            return
        try:
            command = self._build_autoxds_command(action)
        except Exception as exc:
            self._set_preview(self.xds_preview, None, exc)
            return
        self._set_preview(self.xds_preview, command)

    def _update_solve_preview(self, action: str) -> None:
        if not hasattr(self, "solve_preview"):
            return
        try:
            command = self._build_autosolve_command(action)
        except Exception as exc:
            self._set_preview(self.solve_preview, None, exc)
            return
        self._set_preview(self.solve_preview, command)

    def _run_command_async(
        self,
        label: str,
        command: list[str],
        *,
        result_target: dict[str, str] | None = None,
    ) -> None:
        self.status_var.set(f"Running {label}...")
        self._console_log("")
        self._console_log(f"[{label}]")
        self._console_log(f"$ {self._command_text(command)}", detail=True)
        self._notify_ai("operation_started", op=label, cmd=self._command_text(command))

        def worker() -> None:
            try:
                output_lines: list[str] = []
                process = subprocess.Popen(
                    [str(part) for part in command],
                    cwd=str(self.autocrys_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                with process.stdout:
                    for line in process.stdout:
                        stripped = line.rstrip("\r\n")
                        if stripped:
                            output_lines.append(stripped)
                            self._ui_queue.put(("command_output", stripped))
                returncode = process.wait()
                completed = subprocess.CompletedProcess(command, returncode, "\n".join(output_lines), "")
            except Exception as exc:
                self._ui_queue.put(("command_finished", label, None, exc, result_target))
                return
            self._ui_queue.put(("command_finished", label, completed, None, result_target))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                event = self._ui_queue.get_nowait()
                if event[0] == "command_output":
                    self._console_log(event[1], detail=True)
                elif event[0] == "command_finished":
                    _kind, label, completed, error, result_target = event
                    self._finish_command(label, completed, error, result_target=result_target)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self._schedule(80, self._drain_ui_queue)

    def _finish_command(
        self,
        label: str,
        completed: subprocess.CompletedProcess[str] | None,
        error: Exception | None,
        *,
        result_target: dict[str, str] | None = None,
    ) -> None:
        if error is not None:
            self.status_var.set(f"{label} failed: {error}")
            self._console_log(f"ERROR: {error}")
            self._notify_ai("operation_finished", op=label, success=False, error=str(error))
            if result_target:
                self._show_agent_task_result(result_target, label, completed, error)
            return
        assert completed is not None
        if completed.stdout:
            try:
                operation_result: object = self.xds_parse_json(completed.stdout)
            except Exception:
                operation_result = {"stdout_tail": completed.stdout[-16000:]}
            self._notify_ai("operation_result", op=label, result=operation_result)
        if completed.returncode == 0:
            self.status_var.set(f"{label} complete")
            self._notify_ai("operation_finished", op=label, success=True)
        else:
            self.status_var.set(f"{label} exited with code {completed.returncode}")
            self._notify_ai("operation_finished", op=label, success=False)
        self._console_log(f"[exit {completed.returncode}]")
        if result_target:
            self._show_agent_task_result(result_target, label, completed, None)

    @staticmethod
    def _agent_skill_target(skill_id: str) -> str | None:
        if skill_id in {
            "list_datasets", "process_xds", "process_xds_declared", "process_xds_all",
            "set_cell_sg", "cluster_hca", "cluster_with_dendrogram", "merge_datasets",
            "merge_and_run",
        }:
            return "AutoXDS"
        if skill_id in {
            "solve_shelxt", "solve_shelxt_with_olex2", "solve_prepare", "classify_result",
            "refine_shelxl",
        }:
            return "AutoSolve"
        if skill_id in {"run_r3d", "run_r3d_refine_axis"}:
            return "AutoR3D"
        return None

    def _select_workflow_tab(self, tab_name: str) -> None:
        tab = getattr(self, "_workflow_tabs", {}).get(tab_name)
        if tab is not None:
            self._notebook.select(tab)

    def _show_agent_task_result(
        self,
        target: dict[str, str],
        label: str,
        completed: subprocess.CompletedProcess[str] | None,
        error: Exception | None,
    ) -> None:
        tab_name = str(target.get("tab") or "")
        if not tab_name:
            return
        self._select_workflow_tab(tab_name)
        if error is not None:
            body = f"Agent task failed\n\n{error}"
        else:
            assert completed is not None
            output = (completed.stdout or completed.stderr or "").strip()
            try:
                parsed = self.xds_parse_json(output) if output else {}
            except Exception:
                parsed = None
            rendered = json.dumps(parsed, ensure_ascii=False, indent=2) if parsed is not None else output
            status = "completed" if completed.returncode == 0 else f"failed (exit {completed.returncode})"
            body = f"{label} {status}"
            if rendered:
                body += "\n\n" + rendered[-30000:]

        self._render_agent_result(tab_name, body)

    def _handle_agent_feedback(self, payload: dict) -> None:
        tab_name = str(payload.get("module") or "")
        operation = str(payload.get("operation") or "Agent task")
        success = bool(payload.get("success", False))
        summary = str(payload.get("summary") or "").strip()
        output = str(payload.get("output") or "").strip()
        state = "completed" if success else "failed"
        body = f"{operation} {state}"
        if summary:
            body += "\n\n" + summary
        if output and output != summary:
            body += "\n\n" + output[-30000:]
        self._select_workflow_tab(tab_name)
        self._render_agent_result(tab_name, body)

    def _render_agent_result(self, tab_name: str, body: str) -> None:
        if tab_name == "AutoXDS":
            self.xds_show_text(body, body.splitlines()[0])
        elif tab_name == "AutoSolve":
            self.solve_preview.configure(state="normal")
            self.solve_preview.delete("1.0", "end")
            self.solve_preview.insert("end", body.rstrip() + "\n")
            self.solve_preview.see("end")
            self.solve_preview.configure(state="disabled")
            self.status_var.set(body.splitlines()[0])
        elif tab_name == "AutoR3D":
            self.reconstruction_info_var.set(body[:1000])
            self.status_var.set(body.splitlines()[0])

    def _build_autoxds_command(self, action: str) -> list[str]:
        script = self.autocrys_root / "AutoXDS" / "scripts" / "auto_xds.py"
        root = self.xds_root_var.get().strip() or str(self.autocrys_root / "Data")
        command = [sys.executable, str(script), action, "--root", root]
        if self.xds_json_var.get():
            command.append("--json")
        if self.xds_dry_run_var.get() and action != "list":
            command.append("--dry-run")

        datasets = _split_names(self.xds_datasets_var.get())
        if action == "list":
            return command

        if action == "process":
            if self.xds_all_var.get():
                command.append("--all")
            elif datasets:
                for dataset in datasets:
                    command.extend(["--dataset", dataset])
            else:
                raise ValueError("AutoXDS Process needs a dataset name or All datasets.")
            cell = self.xds_cell_var.get().strip()
            sg = self.xds_sg_var.get().strip()
            if not cell:
                self.xds_sg_var.set("")
                command.extend([
                    "--clear-declared-cell-sg",
                    "--preserve-original-cell-sg-on-first-run",
                ])
            elif not sg:
                raise ValueError("Space Group is required when Unit Cell is specified.")
            else:
                command.extend(["--declared-cell", cell, "--declared-sg", sg])
            self._append_optional(command, "--resolution", self.xds_resolution_var.get())
            return command

        if action == "set-cell-sg":
            if not datasets:
                raise ValueError("Set Cell/SG needs at least one dataset name.")
            for dataset in datasets:
                command.extend(["--dataset", dataset])
            cell = self.xds_cell_var.get().strip()
            sg = self.xds_sg_var.get().strip()
            if not cell:
                self.xds_sg_var.set("")
                command.append("--clear-declared-cell-sg")
            elif not sg:
                raise ValueError("Space Group is required when Unit Cell is specified.")
            else:
                command.extend(["--declared-cell", cell, "--declared-sg", sg])
            self._append_optional(command, "--resolution", self.xds_resolution_var.get())
            return command

        if action == "cluster":
            if len(datasets) < 2:
                raise ValueError("Cluster needs at least two dataset names.")
            for dataset in datasets:
                command.extend(["--dataset", dataset])
            command.extend(["--cluster-method", self.xds_cluster_method_var.get()])
            return command

        if action == "merge":
            if len(datasets) < 2:
                raise ValueError("Merge needs at least two dataset names.")
            for dataset in datasets:
                command.extend(["--dataset", dataset])
            self._append_optional(command, "--merge-name", self.xds_merge_name_var.get())
            self._append_optional(command, "--resolution", self.xds_resolution_var.get())
            if self.xds_allow_sg_mismatch_var.get():
                command.append("--allow-sg-mismatch")
            command.extend(["--cluster", self.xds_cluster_method_var.get()])
            if self.xds_run_merge_var.get():
                command.append("--run")
            return command

        raise ValueError(f"Unknown AutoXDS action: {action}")

    def _build_autosolve_command(self, action: str) -> list[str]:
        script = self.autocrys_root / "AutoSolve" / "scripts" / "auto_shelxt.py"
        root = self.solve_root_var.get().strip() or str(self.autocrys_root)
        command = [sys.executable, str(script), action, "--root", root]

        has_target = any(
            value.get().strip()
            for value in (self.solve_dataset_var, self.solve_hkl_var, self.solve_workdir_var)
        )
        if not has_target:
            raise ValueError("AutoSolve needs Dataset, HKL, or Workdir.")

        self._append_optional(command, "--dataset", self.solve_dataset_var.get())
        self._append_optional(command, "--hkl", self.solve_hkl_var.get())
        self._append_optional(command, "--workdir", self.solve_workdir_var.get())
        self._append_optional(command, "--basename", self.solve_basename_var.get())
        if action in {"prepare", "solve"}:
            self._append_optional(command, "--composition", self.solve_composition_var.get())
            self._append_optional(command, "--formula", self.solve_formula_var.get())
            self._append_optional(command, "--z", self.solve_z_var.get())
            self._append_optional(command, "--cell", self.solve_cell_var.get())
            self._append_optional(command, "--sg", self.solve_sg_var.get())
        if self.solve_no_force_sg_var.get():
            command.append("--no-force-sg")
        if self.solve_dry_run_var.get():
            command.append("--dry-run")
        if self.solve_json_var.get():
            command.append("--json")
        return command

    def _append_optional(self, command: list[str], option: str, value: str) -> None:
        cleaned = value.strip()
        if cleaned:
            command.extend([option, cleaned])

    def run_autoxds(self, action: str) -> None:
        try:
            command = self._build_autoxds_command(action)
        except Exception as exc:
            self._update_xds_preview(action)
            messagebox.showerror("AutoXDS", str(exc), parent=self.root)
            self.status_var.set(f"AutoXDS setup failed: {exc}")
            return
        self._update_xds_preview(action)
        self._run_command_async(f"AutoXDS {action}", command)

    def run_autosolve(self, action: str) -> None:
        try:
            command = self._build_autosolve_command(action)
        except Exception as exc:
            self._update_solve_preview(action)
            messagebox.showerror("AutoSolve", str(exc), parent=self.root)
            self.status_var.set(f"AutoSolve setup failed: {exc}")
            return
        self._update_solve_preview(action)
        self._run_command_async(f"AutoSolve {action}", command)

    def _panel_output_root(self) -> Path:
        if self.panel_path is not None:
            return self.panel_path.parent.parent
        output = _normalize_path(self.output_var.get())
        dataset = self.dataset_path or _normalize_path(self.dataset_var.get())
        if not output.is_absolute():
            output = dataset / output
        return output

    def _load_embedded_panel_data(self, outputs: dict | None = None) -> None:
        root = self._panel_output_root()
        report_path = root / "qc_report.json"
        report: dict = {}
        if report_path.is_file():
            try:
                loaded_report = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(loaded_report, dict):
                    report = loaded_report
            except (OSError, ValueError, TypeError):
                report = {}
        if outputs is None:
            current_outputs = report.get("exports", {}).get("outputs")
            if isinstance(current_outputs, dict):
                outputs = current_outputs
        basis_value = (report.get("crystal") or {}).get("reciprocal_basis")
        if basis_value is None:
            basis_value = (report.get("xds") or {}).get("reciprocal_basis")
        self.embedded_reciprocal_basis = self._validated_reciprocal_basis(basis_value)
        if outputs is not None:
            observations_path = Path(outputs["spot_observations_npz"])
            peaks_path = Path(outputs["peak_candidates_json"])
            indexed_value = outputs.get("indexed_observations_npz")
            indexed_path = Path(indexed_value) if indexed_value else None
        else:
            root = self._panel_output_root()
            observations_path = root / "observations" / "spot_observations.npz"
            peaks_path = root / "peaks" / "peak_candidates.json"
            candidate = root / "observations" / "indexed_observations.npz"
            indexed_path = candidate if candidate.is_file() else None
        if not observations_path.exists() or not peaks_path.exists():
            raise FileNotFoundError("Run reconstruction before loading the embedded 3D panel.")

        with np.load(observations_path) as data:
            obs_q = data["q"].astype(np.float64)
            obs_i = data["intensity"].astype(np.float64)
            obs_frame = data["frame"].astype(np.int32)

        obs_hkl: np.ndarray | None = None
        if indexed_path is not None and Path(indexed_path).is_file():
            with np.load(indexed_path) as data:
                candidate_hkl = data["hkl"].astype(np.float64)
            if candidate_hkl.shape[0] == obs_q.shape[0]:
                obs_hkl = candidate_hkl

        if obs_q.shape[0] > 15000:
            keep = np.argpartition(obs_i, -15000)[-15000:]
            obs_q = obs_q[keep]
            obs_i = obs_i[keep]
            obs_frame = obs_frame[keep]
            if obs_hkl is not None:
                obs_hkl = obs_hkl[keep]

        payload = json.loads(peaks_path.read_text(encoding="utf-8"))
        peak_rows = payload.get("peaks", [])
        peak_hkl: np.ndarray | None = None
        if peak_rows:
            peak_q = np.asarray([row["q"] for row in peak_rows], dtype=np.float64)
            peak_i = np.asarray([row.get("intensity", row.get("local_sum", 1.0)) for row in peak_rows], dtype=np.float64)
            peak_frame = np.asarray(
                [
                    row.get("frame_min") if row.get("frame_min") is not None else 0
                    for row in peak_rows
                ],
                dtype=np.int32,
            )
            if all(row.get("hkl") is not None for row in peak_rows):
                peak_hkl = np.asarray([row["hkl"] for row in peak_rows], dtype=np.float64)
        else:
            peak_q = np.empty((0, 3), dtype=np.float64)
            peak_i = np.empty((0,), dtype=np.float64)
            peak_frame = np.empty((0,), dtype=np.int32)

        if obs_hkl is not None:
            self.embedded_space = "q (XDS indexed)"
            self.embedded_axis_labels = (
                ("a*", "b*", "c*")
                if self.embedded_reciprocal_basis is not None
                else ("qx", "qy", "qz")
            )
            observation_positions = obs_q
            peak_positions = peak_q
            observation_indexed = self._indexed_point_mask(obs_hkl)
            peak_indexed = self._indexed_point_mask(peak_hkl) if peak_hkl is not None else None
            if hasattr(self, "embedded_indexed_check"):
                self.embedded_indexed_check.configure(state="normal")
        else:
            self.embedded_space = "q"
            self.embedded_axis_labels = ("qx", "qy", "qz")
            observation_positions = obs_q
            peak_positions = peak_q
            observation_indexed = None
            peak_indexed = None
            self.embedded_indexed_var.set(False)
            if hasattr(self, "embedded_indexed_check"):
                self.embedded_indexed_check.configure(state="disabled")

        self.embedded_panel_data = {
            "observations": {
                "q": observation_positions,
                "intensity": obs_i,
                "frame": obs_frame,
                "indexed": observation_indexed,
            },
            "peaks": {
                "q": peak_positions,
                "intensity": peak_i,
                "frame": peak_frame,
                "indexed": peak_indexed,
            },
        }
        self.set_embedded_axis_view("iso")

    @staticmethod
    def _indexed_point_mask(hkl: np.ndarray | None) -> np.ndarray:
        """Return points whose h, k and l are each within 0.1 r.l.u. of an integer."""
        if hkl is None:
            return np.empty((0,), dtype=bool)
        values = np.asarray(hkl, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != 3:
            return np.zeros((values.shape[0] if values.ndim else 0,), dtype=bool)
        finite = np.all(np.isfinite(values), axis=1)
        residual = np.abs(values - np.rint(values))
        return finite & np.all(residual <= INDEXED_POINT_TOLERANCE_RLU + 1e-12, axis=1)

    @staticmethod
    def _validated_reciprocal_basis(value: object) -> np.ndarray | None:
        try:
            basis = np.asarray(value, dtype=np.float64)
        except (TypeError, ValueError):
            return None
        if basis.shape != (3, 3) or not np.all(np.isfinite(basis)):
            return None
        if abs(float(np.linalg.det(basis))) <= 1e-15:
            return None
        return basis

    @staticmethod
    def _embedded_view_angles(direction: np.ndarray) -> tuple[float, float]:
        vector = np.asarray(direction, dtype=np.float64)
        norm = float(np.linalg.norm(vector))
        if vector.shape != (3,) or not np.isfinite(norm) or norm <= 0:
            raise ValueError("View direction must be a finite nonzero 3-vector.")
        x, y, z = vector / norm
        yaw = float(np.arctan2(x, y))
        pitch = float(np.arctan2(-z, np.hypot(x, y)))
        return yaw, pitch

    @staticmethod
    def _embedded_view_frame(direction: np.ndarray, up_direction: np.ndarray) -> np.ndarray:
        forward = np.asarray(direction, dtype=np.float64)
        up = np.asarray(up_direction, dtype=np.float64)
        if forward.shape != (3,) or up.shape != (3,):
            raise ValueError("View direction and up direction must be 3-vectors.")
        forward_norm = float(np.linalg.norm(forward))
        if not np.all(np.isfinite(forward)) or forward_norm <= 0:
            raise ValueError("View direction must be finite and nonzero.")
        forward = forward / forward_norm
        up = up - float(np.dot(up, forward)) * forward
        up_norm = float(np.linalg.norm(up))
        if not np.all(np.isfinite(up)) or up_norm <= 1e-12:
            candidates = np.eye(3, dtype=np.float64)
            up = min(candidates, key=lambda candidate: abs(float(np.dot(candidate, forward))))
            up = up - float(np.dot(up, forward)) * forward
            up_norm = float(np.linalg.norm(up))
        up = up / up_norm
        right = np.cross(forward, up)
        right /= np.linalg.norm(right)
        up = np.cross(right, forward)
        up /= np.linalg.norm(up)
        return np.column_stack([right, up, forward])

    @staticmethod
    def _rotate_embedded_frame(frame: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
        unit_axis = np.asarray(axis, dtype=np.float64).copy()
        unit_axis /= np.linalg.norm(unit_axis)
        x, y, z = unit_axis
        cross = np.asarray(
            [[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64
        )
        cosine = float(np.cos(angle))
        sine = float(np.sin(angle))
        rotation = (
            cosine * np.eye(3, dtype=np.float64)
            + (1.0 - cosine) * np.outer(unit_axis, unit_axis)
            + sine * cross
        )
        rotated = rotation @ np.asarray(frame, dtype=np.float64)
        return AutoR3DUI._embedded_view_frame(rotated[:, 2], rotated[:, 1])

    def reset_embedded_panel_view(self) -> None:
        self.embedded_zoom = 1.0
        self.embedded_pan = (0.0, 0.0)
        self.set_embedded_axis_view("iso")

    def fit_embedded_panel_view(self) -> None:
        self.embedded_zoom = 1.0
        self.embedded_pan = (0.0, 0.0)
        self.render_embedded_panel()

    def set_embedded_axis_view(self, axis: str) -> None:
        if axis not in {"a", "b", "c", "iso"}:
            return
        basis = self.embedded_reciprocal_basis
        axes = np.eye(3, dtype=np.float64) if basis is None else basis
        if axis == "iso":
            unit_axes = axes / np.linalg.norm(axes, axis=0, keepdims=True)
            direction = unit_axes.sum(axis=1)
        else:
            direction = axes[:, {"a": 0, "b": 1, "c": 2}[axis]]
        up_direction = axes[:, 1] if axis == "c" else axes[:, 2]
        self.embedded_view_frame = self._embedded_view_frame(direction, up_direction)
        self.embedded_yaw, self.embedded_pitch = self._embedded_view_angles(direction)
        self.embedded_roll = 0.0
        self.embedded_pan = (0.0, 0.0)
        self.render_embedded_panel()

    def _embedded_keypress(self, event: tk.Event) -> None:
        key = str(getattr(event, "keysym", "")).lower()
        if key in {"a", "b", "c"}:
            self.set_embedded_axis_view(key)
        elif key == "i":
            self.set_embedded_axis_view("iso")
        elif key == "f":
            self.fit_embedded_panel_view()
        elif key == "r":
            self.reset_embedded_panel_view()
        elif key == "g":
            self.embedded_cell_var.set(not bool(self.embedded_cell_var.get()))
            self.render_embedded_panel()

    def _embedded_drag_start(self, event: tk.Event) -> None:
        self.reconstruction_canvas.focus_set()
        state = int(getattr(event, "state", 0))
        mode = (
            "roll"
            if state & 0x0001
            else "vertical"
            if state & 0x0008
            else "horizontal"
            if state & 0x0004
            else "orbit"
        )
        self.embedded_drag_start = (
            int(event.x),
            int(event.y),
            self.embedded_view_frame.copy(),
            mode,
        )

    def _embedded_drag_move(self, event: tk.Event) -> None:
        if self.embedded_drag_start is None:
            return
        start_x, start_y, frame, mode = self.embedded_drag_start
        dx = float(event.x) - start_x
        dy = float(event.y) - start_y
        rotation_speed = 0.006
        if mode == "roll":
            rotated = self._rotate_embedded_frame(frame, frame[:, 2], -dx * rotation_speed)
            self.embedded_roll = self._wrap_rotation_angle(-dx * rotation_speed)
        elif mode == "vertical":
            rotated = self._rotate_embedded_frame(frame, frame[:, 0], -dy * rotation_speed)
        elif mode == "horizontal":
            rotated = self._rotate_embedded_frame(frame, frame[:, 1], -dx * rotation_speed)
        else:
            rotated = self._rotate_embedded_frame(frame, frame[:, 1], -dx * rotation_speed)
            rotated = self._rotate_embedded_frame(
                rotated, rotated[:, 0], -dy * rotation_speed
            )
        self.embedded_view_frame = rotated
        self.embedded_yaw, self.embedded_pitch = self._embedded_view_angles(rotated[:, 2])
        if mode != "roll":
            self.embedded_roll = 0.0
        self.render_embedded_panel()

    @staticmethod
    def _wrap_rotation_angle(value: float) -> float:
        return float((value + np.pi) % (2.0 * np.pi) - np.pi)

    def _embedded_zoom(self, event: tk.Event) -> None:
        self.reconstruction_canvas.focus_set()
        steps = 1 if getattr(event, "delta", 0) > 0 else -1
        self._embedded_zoom_steps(event, steps)

    def _embedded_zoom_steps(self, _event: tk.Event, steps: int) -> None:
        factor = 1.12 if steps > 0 else 0.89
        self.embedded_zoom = float(np.clip(self.embedded_zoom * factor, 0.2, 8.0))
        self.render_embedded_panel()

    def _embedded_pan_start_event(self, event: tk.Event) -> None:
        self.reconstruction_canvas.focus_set()
        self.embedded_pan_start = (
            int(event.x),
            int(event.y),
            float(self.embedded_pan[0]),
            float(self.embedded_pan[1]),
        )

    def _embedded_pan_move(self, event: tk.Event) -> None:
        if self.embedded_pan_start is None:
            return
        start_x, start_y, pan_x, pan_y = self.embedded_pan_start
        self.embedded_pan = (
            pan_x + float(event.x) - start_x,
            pan_y + float(event.y) - start_y,
        )
        self.render_embedded_panel()

    def _project_embedded_points(
        self,
        q: np.ndarray,
        width: int,
        height: int,
        extent: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        frame = np.asarray(self.embedded_view_frame, dtype=np.float64)
        screen_qx = q @ frame[:, 0]
        screen_qy = q @ frame[:, 1]
        depth = q @ frame[:, 2]
        scale = min(width, height) * 0.42 * self.embedded_zoom / max(extent, 1e-9)
        screen_x = width / 2.0 + self.embedded_pan[0] + screen_qx * scale
        screen_y = height / 2.0 + self.embedded_pan[1] - screen_qy * scale
        return screen_x, screen_y, depth

    @staticmethod
    def _hex_color(rgb: tuple[int, int, int]) -> str:
        r, g, b = (int(np.clip(value, 0, 255)) for value in rgb)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _intensity_color(self, value: float, low: float, high: float) -> tuple[int, int, int]:
        log_low = float(np.log1p(max(low, 0.0)))
        log_high = float(np.log1p(max(high, 0.0)))
        log_value = float(np.log1p(max(value, 0.0)))
        signal = float(
            np.clip((log_value - log_low) / max(log_high - log_low, 1e-9), 0.0, 1.0)
        )
        gamma = max(float(self.embedded_gamma_var.get()), 0.01)
        signal = signal ** (1.0 / gamma)
        contrast = max(float(self.embedded_contrast_var.get()), 0.01)
        # Keep both endpoints fixed so lowering contrast never makes a zero-signal
        # observation visible against the dark canvas.
        signal = float(np.clip(signal, 0.0, 1.0)) ** (1.0 / contrast)
        brightness = max(float(self.embedded_brightness_var.get()), 0.0)
        signal = float(np.clip(signal * brightness, 0.0, 1.0))
        opacity = signal ** 1.5
        background = np.array([11.0, 13.0, 16.0], dtype=np.float64)
        point_color = np.array([235.0, 246.0, 255.0], dtype=np.float64)
        color = background * (1.0 - opacity) + point_color * opacity
        return tuple(int(v) for v in color)

    def _draw_embedded_point(self, canvas: tk.Canvas, x: float, y: float, radius: float, rgb: tuple[int, int, int]) -> None:
        color = self._hex_color(rgb)
        canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")

    def render_embedded_panel(self) -> None:
        if not hasattr(self, "reconstruction_canvas"):
            return
        canvas = self.reconstruction_canvas
        canvas.delete("all")
        width = max(1, int(canvas.winfo_width()))
        height = max(1, int(canvas.winfo_height()))
        canvas.create_rectangle(0, 0, width, height, fill="#0b0d10", outline="")

        kind = self.embedded_point_kind_var.get()
        data = self.embedded_panel_data.get(kind)
        if data is None or data["q"].size == 0:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Run Reconstruction, then Load 3D Panel",
                fill="#9aa3ad",
                anchor="center",
            )
            return

        q = data["q"]
        intensity = data["intensity"]
        threshold = float(self.embedded_threshold_var.get()) / 100.0 * float(np.max(intensity) if intensity.size else 1.0)
        keep = intensity >= threshold
        indexed_only = bool(self.embedded_indexed_var.get())
        if indexed_only:
            indexed = data.get("indexed")
            if indexed is None or np.asarray(indexed).shape != keep.shape:
                keep &= False
            else:
                keep &= np.asarray(indexed, dtype=bool)
        q = q[keep]
        intensity = intensity[keep]
        if q.size == 0:
            message = "No indexed points pass the current filter" if indexed_only else "No points pass the current filter"
            canvas.create_text(width / 2, height / 2, text=message, fill="#9aa3ad")
            return

        extent = float(max(np.max(np.abs(q)), 1e-6))
        axis_extent = extent * 1.15
        reciprocal_basis = self.embedded_reciprocal_basis
        if reciprocal_basis is not None and bool(self.embedded_cell_var.get()):
            axis_extent = max(
                axis_extent,
                1.2 * max(float(np.linalg.norm(reciprocal_basis[:, index])) for index in range(3)),
            )
            cell_colors = ("#ee5253", "#49c969", "#448bff")
            for axis in range(3):
                other = [index for index in range(3) if index != axis]
                for first in (-1.0, 0.0, 1.0):
                    for second in (-1.0, 0.0, 1.0):
                        start = np.zeros(3, dtype=np.float64)
                        end = np.zeros(3, dtype=np.float64)
                        start[axis] = -1.0
                        end[axis] = 1.0
                        start[other[0]] = end[other[0]] = first
                        start[other[1]] = end[other[1]] = second
                        q_segment = np.vstack([start, end]) @ reciprocal_basis.T
                        px, py, _depth = self._project_embedded_points(q_segment, width, height, axis_extent)
                        canvas.create_line(
                            px[0], py[0], px[1], py[1], fill=cell_colors[axis], width=1
                        )

        label_x, label_y, label_z = self.embedded_axis_labels
        axis_directions = (
            np.eye(3, dtype=np.float64)
            if reciprocal_basis is None
            else reciprocal_basis / np.linalg.norm(reciprocal_basis, axis=0, keepdims=True)
        )
        axes = [
            (np.zeros(3), axis_directions[:, 0] * axis_extent, "#ee5253", label_x),
            (np.zeros(3), axis_directions[:, 1] * axis_extent, "#49c969", label_y),
            (np.zeros(3), axis_directions[:, 2] * axis_extent, "#448bff", label_z),
        ]
        for start, end, color, label in axes:
            px, py, _depth = self._project_embedded_points(np.vstack([start, end]), width, height, axis_extent)
            canvas.create_line(px[0], py[0], px[1], py[1], fill=color, width=2)
            canvas.create_text(px[1], py[1], text=label, fill=color, anchor="center")

        sx, sy, depth = self._project_embedded_points(q, width, height, axis_extent)
        order = np.argsort(depth)
        positive = intensity[intensity > 0]
        if positive.size:
            low = float(np.percentile(positive, 5.0))
            high = float(np.percentile(positive, 99.0))
        else:
            low = float(np.min(intensity))
            high = float(np.max(intensity) + 1.0)
        radius = max(1.0, float(self.embedded_point_size_var.get()))
        if kind == "peaks":
            radius *= 1.35

        for idx in order:
            x = float(sx[idx])
            y = float(sy[idx])
            if x < -20 or y < -20 or x > width + 20 or y > height + 20:
                continue
            color = self._intensity_color(float(intensity[idx]), low, high)
            self._draw_embedded_point(canvas, x, y, radius, color)

        canvas.create_text(
            12,
            12,
            text=(
                f"{q.shape[0]} points | {self.embedded_space}"
                f"{' | indexed' if indexed_only else ''} | "
                "a* red  b* green  c* blue | drag orbit | Shift roll | Alt vertical | "
                "Ctrl horizontal | right pan | wheel zoom"
            ),
            fill="#c8d0d6",
            anchor="nw",
        )
        canvas.create_text(
            width - 12,
            height - 12,
            text=f"zoom {self.embedded_zoom:.2f}x",
            fill="#9aa3ad",
            anchor="se",
        )

    def browse_dataset(self) -> None:
        initial = str(_normalize_path(self.dataset_var.get())) if self.dataset_var.get().strip() else ""
        selected = filedialog.askdirectory(parent=self.root, initialdir=initial or None)
        if selected:
            self.dataset_var.set(selected)
            self.dataset_path = None
            self.params = None
            self.reconstruction_button.configure(state="disabled")
            self.reconstruction_info_var.set("Click Import Dataset before reconstruction.")

    def browse_output(self) -> None:
        dataset = self.dataset_path
        initial = dataset if dataset is not None and dataset.exists() else None
        selected = filedialog.askdirectory(parent=self.root, initialdir=str(initial) if initial else None)
        if selected:
            self.output_var.set(selected)
            self._output_path_is_custom = True

    def import_dataset(self) -> None:
        self.reconstruction_button.configure(state="disabled")
        try:
            raw_dataset = self.dataset_var.get().strip()
            if not raw_dataset:
                raise RuntimeError(
                    "Choose a dataset folder first. It must contain the cRED2 parameter file "
                    "and diff/frame_*.tif."
                )
            dataset = _normalize_path(raw_dataset)
            if not dataset.is_dir():
                raise FileNotFoundError(f"Directory does not exist: {dataset}")
            params = parse_cred2_parameters(find_cred2_file(dataset))
            if not self._output_path_is_custom:
                self.output_var.set(str(self.data_root / "AutoR3D" / dataset.name))
            frames = discover_frames(dataset, frames_dir=self.frames_dir)
            stats: dict[int, FrameStats] = {}
            first_nonzero: int | None = None
            for index, record in enumerate(frames):
                frame_stats = inspect_frame(record)
                stats[record.frame_number] = frame_stats
                if first_nonzero is None and not frame_stats.is_zero:
                    first_nonzero = index
            first_nonzero = 0 if first_nonzero is None else first_nonzero
            first_array = load_image(frames[first_nonzero].path)
            self.indexed_observations_path = None
            self._indexed_hkl = None
            self._indexed_intensity = None
            self.xds_reference = load_reference(dataset, self.r3d_source_var.get(), self.r3d_run_var.get().strip() or None)
            self._resolved_dials_run = Path(self.xds_reference.files["DIALS"]).parent if getattr(self.xds_reference, "source", None) == "DIALS" else None
            self._imported_reference_choice = (self.r3d_source_var.get(), self.r3d_run_var.get())
            if self.xds_reference is not None:
                reference = self.xds_reference
                self.axis_var.set(f"{reference.rotation_axis_deg:.3f}")
                if reference.beam_center_px is not None:
                    self.center_x_var.set(f"{reference.beam_center_px[0]:.2f}")
                    self.center_y_var.set(f"{reference.beam_center_px[1]:.2f}")
                else:
                    self.center_x_var.set(f"{first_array.shape[1] / 2.0:.1f}")
                    self.center_y_var.set(f"{first_array.shape[0] / 2.0:.1f}")
                cell_text = (
                    " ".join(f"{value:.2f}" for value in reference.cell_constants)
                    if reference.cell_constants
                    else "n/a"
                )
                self.axis_info_var.set(
                    f"{getattr(reference, 'source', 'XDS')} axis {reference.axis_angle_deg:.2f} deg "
                    f"(AutoR3D {reference.rotation_axis_deg:.2f} deg, sign {reference.rotation_sign:+.0f})\n"
                    f"cell: {cell_text} | SG {reference.space_group_number if reference.space_group_number else 'n/a'}\n"
                    f"files: {', '.join(sorted(reference.files))}"
                )
                self._console_log(
                    f"{getattr(reference, 'source', 'XDS')} reference loaded: axis {reference.axis_angle_deg:.3f} deg, cell {cell_text}",
                    clear=False,
                )
            else:
                self.axis_var.set("142.0")
                if self.center_x_var.get().strip() == "258" and self.center_y_var.get().strip() == "258":
                    self.center_x_var.set(f"{first_array.shape[1] / 2.0:.1f}")
                    self.center_y_var.set(f"{first_array.shape[0] / 2.0:.1f}")
                self.axis_info_var.set("No processing reference selected; using the manual axis.")
            parameter_text = f"cRED2: {params.number_of_frames} frames, {params.oscillation_angle_deg:.4f} deg/frame"

            self.dataset_path = dataset
            self.params = params
            self.detector_shape = tuple(int(v) for v in first_array.shape)
            self.frames = frames
            self.frame_stats = stats
            self.peak_points_by_index.clear()
            self.peak_progress_var.set(0.0)
            self.current_index = first_nonzero
            self.frame_index_var.set(float(first_nonzero))
            self.frame_scale.configure(from_=0, to=max(len(frames) - 1, 0))
            self._load_frame(first_nonzero)
            zero_count = sum(1 for item in stats.values() if item.is_zero)
            self.status_var.set(f"Imported {len(frames)} frames from {dataset}")
            self._notify_ai("dataset_selected", name=dataset.name, path=dataset)
            self.frame_info_var.set(f"{parameter_text}\nZero placeholders: {zero_count}")
            self.reconstruction_info_var.set(
                f"Ready: {len(frames)} frames, {zero_count} zero placeholders, axis {self.axis_var.get()} deg"
            )
            self.reconstruction_button.configure(state="normal")
            self._console_log(
                f"Imported {len(frames)} frames from {dataset}\nZero placeholders: {zero_count}",
                clear=True,
            )
            try:
                root = self._panel_output_root()
                if (root / "observations" / "spot_observations.npz").exists() and (
                    root / "peaks" / "peak_candidates.json"
                ).exists():
                    self.panel_path = root / "panel" / "index.html"
                    self.open_panel_button.configure(state="normal")
                    self._load_embedded_panel_data()
            except Exception:
                pass
        except Exception as exc:
            self.dataset_path = None
            self.params = None
            self.frames = []
            self.reconstruction_button.configure(state="disabled")
            self.reconstruction_info_var.set("Dataset import failed. Check the cRED2 parameter file and diff frames.")
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Import failed: {exc}")

    def _on_frame_scale(self, value: str) -> None:
        if not self.frames:
            return
        index = int(round(float(value)))
        index = max(0, min(index, len(self.frames) - 1))
        if index != self.current_index:
            self._load_frame(index)

    def _load_frame(self, index: int) -> None:
        if not self.frames:
            return
        self.current_index = max(0, min(index, len(self.frames) - 1))
        record = self.frames[self.current_index]
        self.current_array = load_image(record.path)
        stats = self.frame_stats.get(record.frame_number)
        if stats is None:
            stats = inspect_frame(record)
            self.frame_stats[record.frame_number] = stats
        self.frame_label.configure(text=f"{self.current_index + 1} / {len(self.frames)}")
        peak_count = len(self.peak_points_by_index.get(self.current_index, []))
        zero_text = "zero" if stats.is_zero else "nonzero"
        self.status_var.set(
            f"Frame {record.frame_number}: {zero_text}, max {stats.max_intensity:.6g}, peaks {peak_count}"
        )
        self.render_frame()

    def run_peak_search(self) -> None:
        if not self.frames or self.current_array is None:
            messagebox.showwarning("AutoR3D", "Import a dataset first.", parent=self.root)
            return
        try:
            center_x = _float_from_var(self.center_x_var, "Center X")
            center_y = _float_from_var(self.center_y_var, "Center Y")
            mask_radius = _float_from_var(self.mask_radius_var, "Mask radius")
            spot_size = _int_from_var(self.spot_size_var, "Spot size")
            threshold = _float_from_var(self.threshold_var, "Threshold")
            mask_edge_guard = _optional_float_from_var(self.mask_edge_guard_var, "Edge guard")
            close_radius = max(1.0, spot_size * 2.0 / 3.0)
            self.peak_search_button.configure(state="disabled")
            self.peak_search_button.configure(text="Searching...")
            self._notify_ai("operation_started", op="AutoR3D Peak Search", cmd=None)
            self._console_log(
                f"Peak search started: circular footprint {spot_size}px, threshold {threshold:g}, "
                f"center mask {mask_radius:g}px",
                clear=True,
            )
            self.peak_points_by_index.clear()
            self.peak_progress_var.set(0.0)
            self.last_mask_radius = mask_radius
            processed = 0
            skipped = 0
            frames_with_peaks = 0
            total_peaks = 0
            backgrounds: list[float] = []
            sigmas: list[float] = []
            total_frames = len(self.frames)
            for index, record in enumerate(self.frames):
                stats = self.frame_stats.get(record.frame_number)
                if stats is None:
                    stats = inspect_frame(record)
                    self.frame_stats[record.frame_number] = stats

                array = self.current_array if index == self.current_index else load_image(record.path)
                self.current_index = index
                self.current_array = array
                self.frame_index_var.set(float(index))
                self.frame_label.configure(text=f"{index + 1} / {total_frames}")

                if stats.is_zero:
                    self.peak_points_by_index[index] = []
                    skipped += 1
                    frame_peak_count = 0
                else:
                    points, background, sigma = extract_diffraction_points(
                        array,
                        threshold=threshold,
                        filter_size=spot_size,
                        center_x=center_x,
                        center_y=center_y,
                        center_mask_radius=mask_radius,
                        min_distance_px=close_radius,
                        max_spots=10000,
                        mask_edge_guard_px=mask_edge_guard,
                        footprint_shape="circle",
                    )
                    self.peak_points_by_index[index] = points
                    processed += 1
                    total_peaks += len(points)
                    frames_with_peaks += int(bool(points))
                    backgrounds.append(background)
                    sigmas.append(sigma)
                    frame_peak_count = len(points)

                progress = 100.0 * float(index + 1) / float(total_frames)
                self.peak_progress_var.set(progress)
                self.status_var.set(
                    f"Peak search {index + 1}/{total_frames} | frame {record.frame_number}: "
                    f"{frame_peak_count} spots, total {total_peaks}, skipped {skipped} zero frames"
                )
                self.render_frame()
                self.root.update_idletasks()

            background_text = f"{float(np.mean(backgrounds)):.4g}" if backgrounds else "n/a"
            sigma_text = f"{float(np.mean(sigmas)):.4g}" if sigmas else "n/a"
            self.status_var.set(
                f"Peak search complete: {total_peaks} spots in {frames_with_peaks}/{processed} "
                f"nonzero frames, skipped {skipped}; mean background {background_text}, sigma {sigma_text}"
            )
            self._console_log(
                f"Peak search complete: {total_peaks} spots in {frames_with_peaks}/{processed} "
                f"nonzero frames, skipped {skipped}; mean background {background_text}, sigma {sigma_text}"
            )
            self._notify_ai(
                "operation_result",
                op="AutoR3D Peak Search",
                result={
                    "total_peaks": total_peaks,
                    "frames_with_peaks": frames_with_peaks,
                    "processed_nonzero_frames": processed,
                    "skipped_zero_frames": skipped,
                    "mean_background": background_text,
                    "mean_sigma": sigma_text,
                },
            )
            self._notify_ai("operation_finished", op="AutoR3D Peak Search", success=True)
            self.render_frame()
        except Exception as exc:
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Peak search failed: {exc}")
            self._console_log(f"Peak search failed: {exc}")
            self._notify_ai("operation_finished", op="AutoR3D Peak Search", success=False, error=str(exc))
        finally:
            self.peak_search_button.configure(state="normal")
            self.peak_search_button.configure(text="Peak Search")

    def _build_reconstruction_args(self) -> argparse.Namespace:
        if getattr(self, "_imported_reference_choice", None) != (self.r3d_source_var.get(), self.r3d_run_var.get()):
            raise RuntimeError("Geometry source changed. Click Import Dataset again before reconstruction.")
        if self.dataset_path is None or self.params is None or not self.frames:
            raise RuntimeError(
                "Import a dataset first. The dataset folder must contain the cRED2 parameter file "
                "and diff/frame_*.tif."
            )
        dataset = self.dataset_path
        raw_dataset = self.dataset_var.get().strip()
        if not raw_dataset or _normalize_path(raw_dataset).resolve() != dataset.resolve():
            raise RuntimeError("The dataset path changed. Click Import Dataset again before reconstruction.")
        output = _normalize_path(self.output_var.get())
        if not output.is_absolute():
            output = dataset / output
        center_x = _float_from_var(self.center_x_var, "Center X")
        center_y = _float_from_var(self.center_y_var, "Center Y")
        mask_radius = _float_from_var(self.mask_radius_var, "Mask radius")
        spot_size = _int_from_var(self.spot_size_var, "Spot size")
        threshold = _float_from_var(self.threshold_var, "Threshold")
        axis_deg = _float_from_var(self.axis_var, "Axis")
        voxel_size = _float_from_var(self.voxel_size_var, "Voxel")
        if voxel_size <= 0:
            raise ValueError("Voxel must be positive.")
        max_peaks = _int_from_var(self.max_peaks_var, "Max peaks")
        slice_thickness = _float_from_var(self.slice_thickness_var, "Slice thickness")
        slice_step = _float_from_var(self.slice_step_var, "Slice step")
        mask_edge_guard = _optional_float_from_var(self.mask_edge_guard_var, "Edge guard")
        if mask_edge_guard is None:
            mask_edge_guard = max(0.0, 20.0 - mask_radius)
        rotation_sign = float(self.xds_reference.rotation_sign) if self.xds_reference is not None else -1.0
        return argparse.Namespace(
            dataset_dir=dataset,
            reference_source=self.r3d_source_var.get(),
            dials_run=getattr(self, "_resolved_dials_run", None),
            frames=self.frames_dir,
            pattern="frame_*.tif",
            out=output,
            voxel_size=voxel_size,
            center_x=center_x,
            center_y=center_y,
            rotation_axis_deg=axis_deg,
            axis_candidates=None,
            refine_axis=False,
            axis_refine_range_deg=3.0,
            axis_refine_coarse_step_deg=0.25,
            axis_refine_fine_step_deg=0.05,
            no_axis_scan=False,
            rotation_sign=rotation_sign,
            slice_thickness_rlu=slice_thickness,
            slice_step_rlu=slice_step,
            no_slices=False,
            threshold_sigma=6.0,
            min_intensity=20.0,
            center_mask_radius=mask_radius,
            mask_file=None,
            mask_threshold=0.0,
            mask_invert=False,
            no_mask_preview=False,
            max_pixels_per_frame=2500,
            preprocess="peaks",
            peak_threshold=threshold,
            peak_filter_size=spot_size,
            peak_footprint="circle",
            close_point_radius=max(1.0, spot_size * 2.0 / 3.0),
            mask_edge_guard_px=mask_edge_guard,
            max_spots_per_frame=1000,
            frame_workers=DEFAULT_FRAME_WORKERS,
            no_spot_log=False,
            max_frames=None,
            gridding="trilinear",
            peak_percentile=99.5,
            max_peaks=max_peaks,
            peak_radius_voxels=2.5,
            min_peak_hit_count=0.1,
            panel_observations=20000,
            ply_observations=50000,
            vti_max_dim=128,
        )

    def run_reconstruction(self) -> None:
        if self.reconstruction_running:
            return
        try:
            args = self._build_reconstruction_args()
        except Exception as exc:
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Reconstruction setup failed: {exc}")
            return
        self.reconstruction_running = True
        self.reconstruction_button.configure(state="disabled", text="Running...")
        self.open_panel_button.configure(state="disabled")
        self.reconstruction_progress.start(12)
        self.status_var.set("Running full 3D reconstruction...")
        self._notify_ai("operation_started", op="AutoR3D Reconstruction", cmd=None)
        axis_label = getattr(self.xds_reference, "source", "XDS") if self.xds_reference is not None else "manual"
        self._reconstruction_log(
            "Running full reconstruction with circular peak footprint, cRED2 angles, and "
            f"{axis_label} rotation axis {args.rotation_axis_deg:.4f} deg (sign {args.rotation_sign:+.0f}); "
            f"{args.frame_workers} frame workers.",
            clear=True,
        )

        def progress_callback(state: str, _key: str, label: str, elapsed: float | None) -> None:
            def update() -> None:
                if state == "started":
                    self.status_var.set(f"Reconstruction: {label}...")
                elif elapsed is not None:
                    self._reconstruction_log(f"{label}: {elapsed:.3f} s")

            self._schedule(0, update)

        args.progress_callback = progress_callback

        def worker() -> None:
            try:
                from AutoR3D.cli import run_workflow

                report = run_workflow(args)
                self._schedule(0, lambda result=report: self._finish_reconstruction(result, None))
            except Exception as exc:
                self._schedule(0, lambda error=exc: self._finish_reconstruction(None, error))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_reconstruction(self, report: dict | None, error: Exception | None) -> None:
        self.reconstruction_running = False
        self.reconstruction_progress.stop()
        self.reconstruction_button.configure(state="normal", text="Run Reconstruction")
        if error is not None:
            self.status_var.set(f"Reconstruction failed: {error}")
            self.reconstruction_info_var.set("Reconstruction failed.")
            self._reconstruction_log(f"Error: {error}", clear=True)
            self._notify_ai("operation_finished", op="AutoR3D Reconstruction", success=False, error=str(error))
            messagebox.showerror("AutoR3D", str(error), parent=self.root)
            return
        if report is None:
            return
        outputs = report["exports"]["outputs"]
        self.panel_path = Path(outputs["panel_html"])
        self.open_panel_button.configure(state="normal")
        observations = report["sparse_observations"]["count"]
        peak_count = report["peaks"]["count"]
        volume_shape = " x ".join(str(v) for v in report["volume"]["shape"])
        chosen_axis = report["axis_refinement"].get(
            "chosen_rotation_axis_deg",
            report["geometry"]["rotation_axis_deg"],
        )
        placeholder_count = report["frames"]["placeholder_frame_count"]
        axis_source = report.get("axis_refinement", {}).get("axis_source", "unknown")
        crystal = report.get("crystal", {})
        indexing = crystal.get("indexing") or {}
        residual = indexing.get("mean_abs_fractional_residual") or []
        cell = crystal.get("cell")
        self.status_var.set(f"Reconstruction complete: {observations} observations, {peak_count} peaks")
        self.reconstruction_info_var.set(f"Embedded panel ready: {self.panel_path}")
        log_lines = [
            "Reconstruction complete.",
            f"Embedded panel loaded from: {self.panel_path}",
            f"Observations: {observations}",
            f"Peak candidates: {peak_count}",
            f"Volume shape: {volume_shape}",
            f"Rotation axis: {chosen_axis:.4f} deg ({axis_source})",
            f"Zero placeholders preserved: {placeholder_count}",
        ]
        if cell:
            log_lines.append("Cell: " + " ".join(f"{float(value):.3f}" for value in cell))
        if residual:
            log_lines.append("hkl residual: " + " ".join(f"{float(value):.3f}" for value in residual) + " r.l.u.")
        timings = report.get("timings", {}).get("stage_seconds", {})
        if timings:
            log_lines.append("Stage timings:")
            log_lines.extend(f"  {name}: {float(seconds):.3f} s" for name, seconds in timings.items())
        log_lines.append(f"Total runtime: {float(report.get('runtime_seconds', 0.0)):.3f} s")
        log_lines.append(f"Summary: {outputs['summary_md']}")
        self._reconstruction_log("\n".join(log_lines), clear=True)
        self._notify_ai("operation_result", op="AutoR3D Reconstruction", result=report)
        self._notify_ai("operation_finished", op="AutoR3D Reconstruction", success=True)
        try:
            self._load_embedded_panel_data(outputs)
        except Exception as exc:
            self._reconstruction_log(f"Embedded panel load failed: {exc}")
        indexed = outputs.get("indexed_observations_npz")
        self.indexed_observations_path = Path(indexed) if indexed else None
        self._indexed_hkl = None
        self._indexed_intensity = None
        if self.indexed_observations_path is not None:
            try:
                self.preview_slice()
            except Exception as exc:
                self._reconstruction_log(f"Slice preview failed: {exc}")

    def open_panel(self) -> None:
        try:
            self._load_embedded_panel_data()
            self.open_panel_button.configure(state="normal")
            self.status_var.set("Loaded embedded 3D panel")
        except Exception as exc:
            messagebox.showwarning("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"3D panel load failed: {exc}")

    def _axis_log(self, text: str, clear: bool = False) -> None:
        if not hasattr(self, "axis_log"):
            return
        if clear:
            self.axis_log.delete("1.0", "end")
        self.axis_log.insert("end", text.rstrip() + "\n")
        self.axis_log.see("end")

    def _show_default_axis_map_after_peak_search(self) -> None:
        try:
            axis_deg = _float_from_var(self.axis_var, "Axis")
            score, q, weights = self._score_axis(axis_deg, stage="post_peak")
            self._show_axis_projection(axis_deg, score, q, weights)
            self._axis_log(
                f"Peak Search complete -> reconstructed full 3D map at axis {axis_deg:.4f} deg, "
                f"then spherical-projected map voxels. "
                f"direction score={score.projection_score:.6g} higher is better, fit={score.combined_score:.4f}, "
                f"map_voxels={score.occupied_voxels}, plotted={score.sampled_observations}",
                clear=True,
            )
            self.status_var.set(
                f"Peak search complete; reconstructed axis {axis_deg:.4f} deg map "
                f"with {score.occupied_voxels} voxels"
            )
        except Exception as exc:
            self._axis_log(f"Post-peak axis map reconstruction skipped: {exc}", clear=True)

    def _axis_inputs(self) -> tuple[SparsePixels, CRED2Parameters, tuple[int, int], float, float]:
        if self.params is None:
            raise RuntimeError("Import a dataset with cRED2 parameters first.")
        if self.detector_shape is None:
            raise RuntimeError("Import a dataset first.")
        if not self.frames:
            raise RuntimeError("Import a dataset first.")
        if len(self.peak_points_by_index) < len(self.frames):
            raise RuntimeError("Run Peak Search first so rotation-axis scoring has spot observations.")

        frame_values: list[np.ndarray] = []
        x_values: list[np.ndarray] = []
        y_values: list[np.ndarray] = []
        angle_values: list[np.ndarray] = []
        intensity_values: list[np.ndarray] = []
        background_values: list[np.ndarray] = []
        sigma_values: list[np.ndarray] = []
        thresholds: dict[int, float] = {}
        for index, record in enumerate(self.frames):
            points = self.peak_points_by_index.get(index, [])
            thresholds[record.frame_number] = 0.0
            if not points:
                continue
            coords = np.asarray([point for point, _intensity in points], dtype=np.float32)
            intensities = np.asarray([intensity for _point, intensity in points], dtype=np.float32)
            n = intensities.size
            frame_values.append(np.full(n, record.frame_number, dtype=np.int32))
            x_values.append(coords[:, 0])
            y_values.append(coords[:, 1])
            angle_values.append(np.full(n, self.params.angle_for_frame(record.frame_number), dtype=np.float32))
            intensity_values.append(intensities)
            background_values.append(np.zeros(n, dtype=np.float32))
            sigma_values.append(np.ones(n, dtype=np.float32))

        if not intensity_values:
            raise RuntimeError("Peak Search found no spots.")
        sparse = SparsePixels(
            frame=np.concatenate(frame_values),
            x=np.concatenate(x_values),
            y=np.concatenate(y_values),
            angle_deg=np.concatenate(angle_values),
            intensity=np.concatenate(intensity_values),
            background=np.concatenate(background_values),
            sigma=np.concatenate(sigma_values),
            thresholds=thresholds,
            mode="peaks",
        )
        center_x = _float_from_var(self.center_x_var, "Center X")
        center_y = _float_from_var(self.center_y_var, "Center Y")
        return sparse, self.params, self.detector_shape, center_x, center_y

    def _score_axis(self, axis_deg: float, stage: str = "single") -> tuple[AxisScore, np.ndarray, np.ndarray]:
        sparse, params, detector_shape, center_x, center_y = self._axis_inputs()
        score, q, weights, _volume = score_projection_axis_with_map(
            sparse,
            params=params,
            detector_shape=detector_shape,
            rotation_axis_deg=axis_deg,
            center_x=center_x,
            center_y=center_y,
            voxel_size=0.01,
            rotation_sign=-1.0,
            stage=stage,
        )
        return score, q, weights

    def _show_axis_projection(self, axis_deg: float, score: AxisScore, q: np.ndarray, weights: np.ndarray) -> None:
        self.axis_var.set(f"{axis_deg:.4g}")
        self.axis_projection_image = self._make_projection_image(q, weights, axis_deg, score)
        self._render_axis_projection()

    def set_axis(self) -> None:
        try:
            axis_deg = _float_from_var(self.axis_var, "Axis")
            score, q, weights = self._score_axis(axis_deg, stage="set")
            self._show_axis_projection(axis_deg, score, q, weights)
            self._axis_log(
                f"Set axis {axis_deg:.4f} deg | direction score={score.projection_score:.6g} higher is better "
                f"| fit={score.combined_score:.4f} | map_voxels={score.occupied_voxels} "
                f"| plotted={score.sampled_observations}",
                clear=True,
            )
        except Exception as exc:
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Set axis failed: {exc}")

    def global_axis_search(self) -> None:
        try:
            sparse, params, detector_shape, center_x, center_y = self._axis_inputs()
            axes = global_axis_grid(step_deg=10.0, range_deg=360.0)
            self._axis_log(
                "Global search: for each axis, reconstruct the full 3D map, then spherical-project map voxels.",
                clear=True,
            )
            self._axis_log("Axis range: 0..350 deg, step 10 deg")
            raw_scores: list[AxisScore] = []
            best_score: AxisScore | None = None
            best_q = np.empty((0, 3), dtype=np.float64)
            best_weights = np.empty((0,), dtype=np.float64)
            for axis_deg in axes:
                score, q, weights, _volume = score_projection_axis_with_map(
                    sparse,
                    params=params,
                    detector_shape=detector_shape,
                    rotation_axis_deg=axis_deg,
                    center_x=center_x,
                    center_y=center_y,
                    voxel_size=0.01,
                    rotation_sign=-1.0,
                    stage="global",
                )
                raw_scores.append(score)
                self._show_axis_projection(axis_deg, score, q, weights)
                self._axis_log(
                    f"{axis_deg:7.2f} deg  SLOI={score.projection_score:.6g} "
                    f"fit={score.combined_score:.4f} map_voxels={score.occupied_voxels} "
                    f"plotted={score.sampled_observations}"
                )
                self.root.update_idletasks()
                if best_score is None or score.combined_score > best_score.combined_score:
                    best_score = score
                    best_q = q
                    best_weights = weights

            if best_score is None:
                raise RuntimeError("Global search produced no axis scores.")
            self._show_axis_projection(best_score.rotation_axis_deg, best_score, best_q, best_weights)
            ranked = sorted(raw_scores, key=lambda item: item.combined_score, reverse=True)[:5]
            self._axis_log("\nTop axes:")
            for item in ranked:
                self._axis_log(
                    f"{item.rotation_axis_deg:7.2f} deg  "
                    f"SLOI={item.projection_score:.6g} fit={item.combined_score:.4f}"
                )
            self.status_var.set(f"Global axis search chose {best_score.rotation_axis_deg:.4f} deg")
        except Exception as exc:
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Global search failed: {exc}")

    def refine_axis(self) -> None:
        try:
            base_axis = _float_from_var(self.axis_var, "Axis")
            sparse, params, detector_shape, center_x, center_y = self._axis_inputs()
            result = refine_rotation_axis(
                sparse=sparse,
                params=params,
                detector_shape=detector_shape,
                base_axis_deg=base_axis,
                center_x=center_x,
                center_y=center_y,
                voxel_size=0.01,
                rotation_sign=-1.0,
                refine_enabled=True,
                refine_range_deg=3.0,
                coarse_step_deg=0.25,
                fine_step_deg=0.05,
            )
            chosen = result.chosen_axis_deg
            score, q, weights = self._score_axis(chosen, stage="refine")
            self._show_axis_projection(chosen, score, q, weights)
            self._axis_log(
                f"Refine from {base_axis:.4f} deg -> {chosen:.4f} deg | "
                f"SLOI={score.projection_score:.6g} lower is better | fit={score.combined_score:.4f}",
                clear=True,
            )
            for item in sorted(result.scores, key=lambda value: value.combined_score, reverse=True)[:10]:
                self._axis_log(
                    f"{item.stage:>6} {item.rotation_axis_deg:9.4f} deg  "
                    f"fit={item.combined_score:.4f} SLOI={item.projection_score:.6g}"
                )
            self.status_var.set(f"Refined rotation axis to {chosen:.4f} deg")
        except Exception as exc:
            messagebox.showerror("AutoR3D", str(exc), parent=self.root)
            self.status_var.set(f"Refine axis failed: {exc}")

    def _make_projection_image(
        self,
        q: np.ndarray,
        weights: np.ndarray,
        axis_deg: float,
        score: AxisScore,
    ) -> Image.Image:
        width = 720
        height = 360
        title_h = 42
        image = Image.new("RGB", (width, height + title_h), (13, 16, 20))
        panel_image = self._spherical_projection_panel(q, weights, width, height)
        image.paste(panel_image, (0, title_h))
        return image

    def _spherical_projection_panel(
        self,
        q: np.ndarray,
        weights: np.ndarray,
        width: int,
        height: int,
    ) -> Image.Image:
        if q.size == 0 or weights.size == 0:
            return Image.new("RGB", (width, height), (20, 24, 28))
        azimuth, elevation, _zenith = q_to_spherical_angles(q)
        valid = np.linalg.norm(q, axis=1) > 1e-12
        weights = weights[valid]
        if azimuth.size == 0 or weights.size == 0:
            return Image.new("RGB", (width, height), (20, 24, 28))
        hist, _az_edges, _el_edges = np.histogram2d(
            azimuth,
            elevation,
            bins=(width, height),
            range=[[-180.0, 180.0], [-90.0, 90.0]],
            weights=np.log1p(np.maximum(weights, 0.0)),
        )
        view = np.log1p(hist.T)
        positive = view[view > 0]
        if positive.size:
            low = float(np.percentile(positive, 1.0))
            high = float(np.percentile(positive, 99.5))
        else:
            low, high = 0.0, 1.0
        t = np.clip((view - low) / max(high - low, 1e-6), 0.0, 1.0)
        red = (255.0 * t).astype(np.uint8)
        green = (230.0 * np.sqrt(t)).astype(np.uint8)
        blue = (70.0 + 110.0 * t).astype(np.uint8)
        rgb = np.stack([red, green, blue], axis=-1)
        canvas = Image.fromarray(np.flipud(rgb), mode="RGB")
        return canvas

    def _render_axis_projection(self) -> None:
        if not hasattr(self, "axis_canvas"):
            return
        self.axis_canvas.delete("all")
        width = max(1, int(self.axis_canvas.winfo_width()))
        height = max(1, int(self.axis_canvas.winfo_height()))
        if self.axis_projection_image is None:
            self.axis_canvas.create_text(width / 2, height / 2, text="No axis projection", fill="#9aa3ad")
            return
        image = self.axis_projection_image.copy()
        scale = min(width / image.width, height / image.height)
        out_w = max(1, int(round(image.width * scale)))
        out_h = max(1, int(round(image.height * scale)))
        image = image.resize((out_w, out_h), resample=Image.Resampling.NEAREST)
        self.axis_photo = ImageTk.PhotoImage(image)
        self.axis_canvas.create_image((width - out_w) / 2, (height - out_h) / 2, image=self.axis_photo, anchor="nw")
        title = (
            f"Axis {self.axis_var.get()} deg | 3D map -> spherical projection | "
            "lower SLOI is better"
        )
        self.axis_canvas.create_text(width / 2, 18, text=title, fill="#e8edf2", anchor="center")
        self.axis_canvas.create_text(width / 2, height - 14, text="azimuth -180..180 deg", fill="#c5ced6", anchor="center")
        self.axis_canvas.create_text(70, height / 2, text="elevation -90..90 deg", fill="#c5ced6", anchor="center", angle=90)

    def _resize_square_canvas(self, event: tk.Event) -> None:
        size = max(80, min(int(event.width), int(event.height)))
        self.canvas.place(relx=0.5, rely=0.5, anchor="center", width=size, height=size)
        self.render_frame()

    def render_frame(self) -> None:
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("all")
        width = max(1, int(self.canvas.winfo_width()))
        height = max(1, int(self.canvas.winfo_height()))
        if self.current_array is None:
            self.canvas.create_text(width / 2, height / 2, text="No frame", fill="#9aa3ad")
            return

        image_u8 = _image_to_uint8(
            self.current_array,
            brightness=float(self.brightness_var.get()),
            contrast=float(self.contrast_var.get()),
            gamma=float(self.gamma_var.get()),
        )
        image = Image.fromarray(image_u8, mode="L").convert("RGB")
        img_w, img_h = image.size
        scale = min(width / img_w, height / img_h)
        out_w = max(1, int(round(img_w * scale)))
        out_h = max(1, int(round(img_h * scale)))
        offset_x = (width - out_w) / 2.0
        offset_y = (height - out_h) / 2.0
        image = image.resize((out_w, out_h), resample=Image.Resampling.NEAREST)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(offset_x, offset_y, image=self.photo, anchor="nw")
        self.display_transform = (scale, offset_x, offset_y)
        self._draw_peak_overlay()

    def _draw_peak_overlay(self) -> None:
        if self.current_array is None:
            return
        scale, offset_x, offset_y = self.display_transform
        try:
            center_x = _float_from_var(self.center_x_var, "Center X")
            center_y = _float_from_var(self.center_y_var, "Center Y")
            mask_radius = _float_from_var(self.mask_radius_var, "Mask radius")
            spot_size = _int_from_var(self.spot_size_var, "Spot size")
            mask_edge_guard = _optional_float_from_var(self.mask_edge_guard_var, "Edge guard")
        except ValueError:
            return
        cx = offset_x + center_x * scale
        cy = offset_y + center_y * scale
        guard = spot_size / 2.0 if mask_edge_guard is None else mask_edge_guard
        radius = max(1.0, (mask_radius + max(0.0, guard)) * scale)
        self.canvas.create_oval(
            cx - radius,
            cy - radius,
            cx + radius,
            cy + radius,
            outline="#f0b94a",
            width=2,
        )
        points = self.peak_points_by_index.get(self.current_index, [])
        for (x, y), intensity in points:
            px = offset_x + x * scale
            py = offset_y + y * scale
            spot_radius = max(3.0, min(9.0, 2.5 + np.log1p(float(intensity)) * 0.45))
            self.canvas.create_oval(
                px - spot_radius,
                py - spot_radius,
                px + spot_radius,
                py + spot_radius,
                outline="#65f09a",
                width=2,
            )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="autocrys", description="AutoCrys desktop UI")
    parser.add_argument("dataset_dir", nargs="?", default=None)
    parser.add_argument("--frames", default="diff")
    args = parser.parse_args(argv)

    root = tk.Tk()
    # The ordinary launcher follows assistant.enabled and uses the compact
    # Console. Explicit AI/base/main variants override these choices.
    AutoR3DUI(
        root,
        dataset_dir=args.dataset_dir,
        frames_dir=args.frames,
        ai_assistant=None,
        console_verbose=False,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
