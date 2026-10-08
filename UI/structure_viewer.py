"""Small Tk canvas crystal model viewer used by the AutoSolve tab."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import ttk

import numpy as np

from AutoSolve.structure_model import (
    StructureModel,
    covalent_radius,
    element_color,
    grow_structure,
    parse_shelx,
    unit_cell_edges,
)


VIEW_BG = "#0b0f16"
VIEW_TEXT = "#d8e2f0"
VIEW_MUTED = "#8795a8"
CELL_COLOR = "#66809e"


def _shade(color: str, factor: float) -> str:
    values = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    adjusted = [int(np.clip(value * factor, 0, 255)) for value in values]
    return "#" + "".join(f"{value:02x}" for value in adjusted)


class StructureViewer(ttk.Frame):
    """Olex-inspired, intentionally minimal ball-and-stick viewer."""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        show_unit_cell: bool = True,
        bond_tolerance: float = 0.45,
        grow_steps_per_click: int = 2,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.model: StructureModel | None = None
        self.base_model: StructureModel | None = None
        self.bond_tolerance = float(bond_tolerance)
        self.grow_steps_per_click = max(1, min(int(grow_steps_per_click), 4))
        self.show_cell_var = tk.BooleanVar(value=bool(show_unit_cell))
        self.info_var = tk.StringVar(value="Choose a SHELX .res or .ins file to view the model.")
        self.rotation = self._euler_rotation(-0.65, 0.48, 0.0)
        self.zoom = 1.0
        self.pan = (0.0, 0.0)
        self.grow_steps = 0
        self._drag: tuple[int, int, np.ndarray, str] | None = None
        self._pan_drag: tuple[int, int, float, float] | None = None
        self._projected_atoms: list[tuple[float, float, float, int]] = []

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, textvariable=self.info_var, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(header, text="Grow", width=7, command=self.grow).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(header, text="Reset", width=7, command=self.reset_grow).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(header, text="a", width=3, command=lambda: self.set_axis_view("a")).grid(row=0, column=3, padx=(4, 0))
        ttk.Button(header, text="b", width=3, command=lambda: self.set_axis_view("b")).grid(row=0, column=4, padx=(4, 0))
        ttk.Button(header, text="c", width=3, command=lambda: self.set_axis_view("c")).grid(row=0, column=5, padx=(4, 0))
        ttk.Checkbutton(
            header,
            text="▣",
            variable=self.show_cell_var,
            command=self.render,
            width=3,
        ).grid(row=0, column=6, padx=(4, 0))

        self.canvas = tk.Canvas(
            self,
            background=VIEW_BG,
            highlightthickness=1,
            highlightbackground="#2a3b55",
            relief="flat",
            takefocus=True,
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.render())
        self.canvas.bind("<ButtonPress-1>", self._start_rotate)
        self.canvas.bind("<B1-Motion>", self._rotate)
        self.canvas.bind("<ButtonRelease-1>", lambda _event: self._stop_drag())
        self.canvas.bind("<ButtonPress-2>", self._start_pan)
        self.canvas.bind("<B2-Motion>", self._pan)
        self.canvas.bind("<ButtonPress-3>", self._start_pan)
        self.canvas.bind("<B3-Motion>", self._pan)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda event: self._zoom_steps(event, 1))
        self.canvas.bind("<Button-5>", lambda event: self._zoom_steps(event, -1))
        self.canvas.bind("<Double-Button-1>", lambda _event: self.reset_view())
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda _event: self.canvas.delete("hover"))
        self.canvas.bind("<KeyPress-r>", lambda _event: self.reset_view())
        self.canvas.bind("<KeyPress-g>", lambda _event: self.toggle_cell())

    def load(self, path: Path | str) -> StructureModel:
        self.model = parse_shelx(path, bond_tolerance=self.bond_tolerance)
        self.base_model = self.model
        self.grow_steps = 0
        self._update_info()
        self.reset_view()
        return self.model

    def reload(self) -> None:
        if self.model is None:
            self.render()
            return
        source = self.model.source
        try:
            self.load(source)
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))

    def show_error(self, message: str) -> None:
        self.model = None
        self.base_model = None
        self.info_var.set(message)
        self.render()

    def _update_info(self, note: str = "") -> None:
        if self.model is None:
            return
        suffix = f"  ·  grow {self.grow_steps}" if self.grow_steps else ""
        if note:
            suffix += f"  ·  {note}"
        self.info_var.set(
            f"{self.model.source.name}  ·  {len(self.model.atoms)} atoms  ·  "
            f"{len(self.model.bonds)} bonds{suffix}"
        )

    def grow(self) -> None:
        if self.model is None:
            return
        previous_count = len(self.model.atoms)
        self.model = grow_structure(self.model, steps=self.grow_steps_per_click)
        added = len(self.model.atoms) - previous_count
        self.grow_steps += self.grow_steps_per_click
        self._update_info(f"+{added} atoms" if added else "complete")
        self.render()

    def reset_grow(self) -> None:
        """Restore the file model without changing the current camera."""
        if self.base_model is None:
            return
        self.model = self.base_model
        self.grow_steps = 0
        self._update_info("original model")
        self.render()

    def toggle_cell(self) -> None:
        self.show_cell_var.set(not bool(self.show_cell_var.get()))
        self.render()

    def reset_view(self) -> None:
        self.rotation = self._euler_rotation(-0.65, 0.48, 0.0)
        self.zoom = 1.0
        self.pan = (0.0, 0.0)
        self.render()

    def set_axis_view(self, axis: str) -> None:
        if self.model is None or axis not in {"a", "b", "c"}:
            return
        vectors = self.model.cell.vectors()
        axis_index = {"a": 0, "b": 1, "c": 2}[axis]
        view = vectors[axis_index] / np.linalg.norm(vectors[axis_index])
        up_source = vectors[1] if axis == "c" else vectors[2]
        vertical = up_source - view * float(np.dot(up_source, view))
        if np.linalg.norm(vertical) < 1e-9:
            fallback = np.array([0.0, 1.0, 0.0])
            vertical = fallback - view * float(np.dot(fallback, view))
        vertical /= np.linalg.norm(vertical)
        horizontal = np.cross(view, vertical)
        horizontal /= np.linalg.norm(horizontal)
        self.rotation = np.column_stack([horizontal, view, vertical])
        self.pan = (0.0, 0.0)
        self.render()

    def _stop_drag(self) -> None:
        self._drag = None
        self._pan_drag = None

    def _start_rotate(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        mode = "roll" if int(getattr(event, "state", 0)) & 0x0001 else "orbit"
        self._drag = (int(event.x), int(event.y), self.rotation.copy(), mode)

    def _rotate(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        start_x, start_y, rotation, mode = self._drag
        dx = float(event.x) - start_x
        dy = float(event.y) - start_y
        if mode == "roll":
            delta = self._euler_rotation(0.0, 0.0, dx * 0.012)
        else:
            delta = self._euler_rotation(dx * 0.012, dy * 0.012, 0.0)
        self.rotation = rotation @ delta
        self.render()

    def _start_pan(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        self._pan_drag = (int(event.x), int(event.y), self.pan[0], self.pan[1])

    def _pan(self, event: tk.Event) -> None:
        if self._pan_drag is None:
            return
        start_x, start_y, pan_x, pan_y = self._pan_drag
        self.pan = (pan_x + float(event.x) - start_x, pan_y + float(event.y) - start_y)
        self.render()

    def _wheel(self, event: tk.Event) -> None:
        self._zoom_steps(event, 1 if int(getattr(event, "delta", 0)) > 0 else -1)

    def _zoom_steps(self, _event: tk.Event, steps: int) -> None:
        self.zoom = float(np.clip(self.zoom * (1.12 if steps > 0 else 0.89), 0.2, 8.0))
        self.render()

    @staticmethod
    def _euler_rotation(yaw: float, pitch: float, roll: float) -> np.ndarray:
        cy, sy = np.cos(yaw), np.sin(yaw)
        cp, sp = np.cos(pitch), np.sin(pitch)
        cr, sr = np.cos(roll), np.sin(roll)
        rotate_z = np.array(((cy, -sy, 0), (sy, cy, 0), (0, 0, 1)), dtype=np.float64)
        rotate_x = np.array(((1, 0, 0), (0, cp, -sp), (0, sp, cp)), dtype=np.float64)
        rotate_y = np.array(((cr, 0, -sr), (0, 1, 0), (sr, 0, cr)), dtype=np.float64)
        return rotate_z @ rotate_x @ rotate_y

    def _rotation_matrix(self) -> np.ndarray:
        return self.rotation

    def _scene(self) -> tuple[np.ndarray, np.ndarray, tuple[tuple[int, int], ...]]:
        if self.model is None:
            return np.empty((0, 3)), np.empty((0, 3)), ()
        atoms = np.asarray([atom.cartesian for atom in self.model.atoms], dtype=np.float64)
        corners, edges = unit_cell_edges(self.model.cell)
        scene = np.vstack([atoms, corners]) if self.show_cell_var.get() else atoms
        center = (np.min(scene, axis=0) + np.max(scene, axis=0)) / 2.0
        return atoms - center, corners - center, edges

    def _project(self, points: np.ndarray, extent: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        width = max(1, int(self.canvas.winfo_width()))
        height = max(1, int(self.canvas.winfo_height()))
        rotated = points @ self._rotation_matrix()
        scale = min(width, height) * 0.40 * self.zoom / max(extent, 1e-9)
        x = width / 2.0 + self.pan[0] + rotated[:, 0] * scale
        y = height / 2.0 + self.pan[1] - rotated[:, 2] * scale
        return x, y, rotated[:, 1]

    def render(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(1, int(canvas.winfo_width()))
        height = max(1, int(canvas.winfo_height()))
        canvas.create_rectangle(0, 0, width, height, fill=VIEW_BG, outline="")
        if self.model is None:
            canvas.create_text(
                width / 2,
                height / 2,
                text="No structure loaded\nOpen a .res or .ins file in AutoSolve",
                fill=VIEW_MUTED,
                justify="center",
            )
            self._projected_atoms = []
            return

        atoms, corners, edges = self._scene()
        scene = np.vstack([atoms, corners]) if self.show_cell_var.get() else atoms
        extent = float(max(np.ptp(scene, axis=0).max() / 2.0, 1e-6))
        atom_x, atom_y, atom_depth = self._project(atoms, extent)

        if self.show_cell_var.get():
            cell_x, cell_y, _cell_depth = self._project(corners, extent)
            for first, second in edges:
                canvas.create_line(
                    cell_x[first], cell_y[first], cell_x[second], cell_y[second],
                    fill=CELL_COLOR, width=1, dash=(5, 4),
                )
            origin = 0
            for target, color, label in ((1, "#f05b61", "a"), (2, "#61d477", "b"), (4, "#579bff", "c")):
                canvas.create_line(
                    cell_x[origin], cell_y[origin], cell_x[target], cell_y[target],
                    fill=color, width=2,
                )
                canvas.create_text(cell_x[target], cell_y[target], text=label, fill=color, anchor="center")

        # Draw each half-bond using the adjoining atom colour, then paint atoms
        # from back to front for a simple but effective depth cue.
        for bond in sorted(
            self.model.bonds,
            key=lambda item: float((atom_depth[item.first] + atom_depth[item.second]) / 2.0),
        ):
            first, second = bond.first, bond.second
            middle_x = (atom_x[first] + atom_x[second]) / 2.0
            middle_y = (atom_y[first] + atom_y[second]) / 2.0
            first_color = _shade(element_color(self.model.atoms[first].element), 0.72)
            second_color = _shade(element_color(self.model.atoms[second].element), 0.72)
            canvas.create_line(atom_x[first], atom_y[first], middle_x, middle_y, fill=first_color, width=4)
            canvas.create_line(middle_x, middle_y, atom_x[second], atom_y[second], fill=second_color, width=4)

        depth_min = float(np.min(atom_depth)) if atom_depth.size else 0.0
        depth_span = max(float(np.ptp(atom_depth)), 1e-9)
        self._projected_atoms = []
        for index in np.argsort(atom_depth):
            atom = self.model.atoms[int(index)]
            depth_signal = (float(atom_depth[index]) - depth_min) / depth_span
            radius = float(np.clip(5.2 + covalent_radius(atom.element) * 3.5 + depth_signal * 1.8, 5.0, 13.0))
            x, y = float(atom_x[index]), float(atom_y[index])
            color = element_color(atom.element)
            outline = _shade(color, 0.48)
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline=outline, width=1)
            highlight = max(1.5, radius * 0.28)
            canvas.create_oval(
                x - radius * 0.45 - highlight,
                y - radius * 0.45 - highlight,
                x - radius * 0.45 + highlight,
                y - radius * 0.45 + highlight,
                fill=_shade(color, 1.35),
                outline="",
            )
            self._projected_atoms.append((x, y, radius, int(index)))

        canvas.create_text(
            12,
            height - 12,
            text="drag rotate  ·  Shift+drag roll  ·  right drag pan  ·  wheel zoom  ·  G cell",
            fill=VIEW_MUTED,
            anchor="sw",
        )
        canvas.create_text(
            width - 12,
            height - 12,
            text=f"{self.zoom:.2f}×",
            fill=VIEW_MUTED,
            anchor="se",
        )

    def _hover(self, event: tk.Event) -> None:
        self.canvas.delete("hover")
        if self.model is None:
            return
        nearest: tuple[float, int] | None = None
        for x, y, radius, index in self._projected_atoms:
            distance = float(np.hypot(float(event.x) - x, float(event.y) - y))
            if distance <= radius + 5 and (nearest is None or distance < nearest[0]):
                nearest = (distance, index)
        if nearest is None:
            return
        atom = self.model.atoms[nearest[1]]
        fx, fy, fz = atom.fractional
        text = f"{atom.label} ({atom.element})   {fx:.4f}, {fy:.4f}, {fz:.4f}"
        item = self.canvas.create_text(
            int(event.x) + 12,
            int(event.y) - 12,
            text=text,
            fill=VIEW_TEXT,
            anchor="sw",
            tags="hover",
        )
        bounds = self.canvas.bbox(item)
        if bounds:
            background = self.canvas.create_rectangle(
                bounds[0] - 5, bounds[1] - 3, bounds[2] + 5, bounds[3] + 3,
                fill="#182333", outline="#40536e", tags="hover",
            )
            self.canvas.tag_lower(background, item)
