"""AI controls hosted in the main AutoCrys Assistant workspace."""

from __future__ import annotations

import queue
import threading
import re
import json
from datetime import datetime
import tkinter as tk
from tkinter import ttk

from .artifacts import AnalysisReport, ArtifactAnalyzer
from .config import Config
from .context import AIContext
from .knowledge import KnowledgeBase
from .llm import LLMBackend, NullBackend, OllamaBackend, create_llm
from .monitor import LogMonitor
from .responses import (
    compact_suggestion,
    grounded_user_message,
    limit_text,
    offline_response,
    suggestion_evidence,
)
from .rules import Diagnosis
from .skills import SkillAction, SkillMapper


PANEL_BG = "#0e1622"
PANEL_ALT = "#1b2739"
PANEL_TEXT = "#d4dce8"
PANEL_MUTED = "#8092ab"
PANEL_ACCENT = "#5aa8ff"
PANEL_WARN = "#f4c95d"
PANEL_ERROR = "#ff6b6b"
REPLY_TARGET_CHARS = 300
DEFAULT_REPLY_CHARS = 1200
DETAIL_REPLY_CHARS = 4000
DETAIL_REQUEST_PATTERN = re.compile(
    r"(详细|展开(?:说明|讲讲)?|完整(?:说明|分析)?|深入(?:说明|分析)?|逐步|一步一步|"
    r"explain\s+in\s+detail|detailed|step[- ]by[- ]step)",
    re.IGNORECASE,
)
CONTEXT_REQUEST_PATTERN = re.compile(
    r"<autocrys_context>\s*(\{.*?\})\s*</autocrys_context>",
    re.DOTALL | re.IGNORECASE,
)
TASK_RESULT_PATTERN = re.compile(
    r"<autocrys_result>\s*(\{.*?\})\s*</autocrys_result>",
    re.DOTALL | re.IGNORECASE,
)


def response_char_limit(user_text: str) -> int:
    return DETAIL_REPLY_CHARS if DETAIL_REQUEST_PATTERN.search(user_text or "") else DEFAULT_REPLY_CHARS


def suggestion_char_limit(configured_target: int) -> int:
    """Treat the configured suggestion length as a target, not a hard 300-char cutoff."""
    return max(DEFAULT_REPLY_CHARS, int(configured_target or REPLY_TARGET_CHARS))


class AIAssistantPanel(ttk.Frame):
    """AI controller plus a compact input area for the Assistant workspace."""

    def __init__(
        self,
        parent: tk.Widget,
        context: AIContext,
        monitor: LogMonitor,
        knowledge: KnowledgeBase,
        skill_mapper: SkillMapper,
        config: Config | None = None,
        run_command_callback=None,
        execute_fix_callback=None,
        analyzer: ArtifactAnalyzer | None = None,
        context_provider=None,
        message_widget: tk.Text | None = None,
        mode_callback=None,
        task_result_callback=None,
        font_family: str = "SimSun",
        mono_family: str | None = None,
        verbose_console: bool = False,
    ) -> None:
        super().__init__(parent, style="AIInput.TFrame", padding=(0, 6, 0, 0))
        self.context = context
        self._monitor = monitor
        self._knowledge = knowledge
        self._skills = skill_mapper
        self._config = config or Config()
        self._run_command_cb = run_command_callback
        self._execute_fix_cb = execute_fix_callback
        self._analyzer = analyzer
        self._context_provider = context_provider
        self._msg_text = message_widget
        self._mode_callback = mode_callback
        self._task_result_callback = task_result_callback
        self._font = (font_family, 13)
        self._font_bold = (font_family, 13, "bold")
        self._font_small = (font_family, 12)
        self._font_mono = (mono_family or font_family, 12)
        self._verbose_console = bool(verbose_console)
        self._llm: LLMBackend = NullBackend()
        self._pending_skill: tuple[SkillAction, dict] | None = None
        self._pending_command: str | None = None
        self._pending_argv: list[str] | None = None
        self._analysis_generation = 0
        self._suggestion_generation = 0
        self._analysis_cache: dict[tuple, AnalysisReport | None] = {}
        self._analysis_cache_lock = threading.Lock()
        self._action_catalog = self._skills.action_catalog()
        self._system_prompt = self._knowledge.build_system_prompt(modules=[])
        self._agent_bridge_prompt = self._knowledge.build_agent_bridge_prompt()
        self._async_queue: queue.Queue = queue.Queue()
        self._after_ids: set[str] = set()
        self._destroyed = False
        self._query_in_flight = False

        self.bind("<Destroy>", self._on_destroy, add="+")
        self._setup_styles()
        self._setup_console_tags()
        self._build_ui()
        self._connect_monitor()
        self._reload_llm()
        self._schedule_async_drain()
        self._add_message("system", f"AI assistant enabled ({self.backend_label}).")

    @property
    def backend_label(self) -> str:
        if isinstance(self._llm, OllamaBackend):
            model = self._llm.model_name if self._llm.is_available() else "unavailable"
            return f"Ollama · {model}"
        if isinstance(self._llm, NullBackend):
            return "offline rules"
        provider = getattr(self._llm, "provider_name", "cloud")
        model = getattr(self._llm, "model_name", "")
        return f"{provider} · {model}".strip(" ·")

    def _schedule(self, delay_ms: int, callback) -> str | None:
        if self._destroyed:
            return None
        after_id: str | None = None

        def run() -> None:
            if after_id is not None:
                self._after_ids.discard(after_id)
            if not self._destroyed:
                callback()

        after_id = self.after(delay_ms, run)
        self._after_ids.add(after_id)
        return after_id

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self or self._destroyed:
            return
        self._destroyed = True
        for after_id in tuple(self._after_ids):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()

    def _setup_styles(self) -> None:
        style = ttk.Style(self)
        style.configure("AIPanel.TFrame", background=PANEL_BG)
        style.configure("AIInput.TFrame", background=PANEL_BG)
        style.configure("AIContext.TFrame", background=PANEL_ALT)
        style.configure("AI.TLabel", background=PANEL_BG, foreground=PANEL_TEXT, font=self._font)
        style.configure("AIMuted.TLabel", background=PANEL_BG, foreground=PANEL_MUTED, font=self._font_small)
        style.configure("AISmall.TButton", font=self._font_small, padding=(7, 4))
        style.configure("AIAction.TButton", background=PANEL_ACCENT, font=self._font_small, padding=(9, 4))

    def _setup_console_tags(self) -> None:
        if self._msg_text is None:
            return
        self._msg_text.tag_configure("timestamp", foreground=PANEL_MUTED, font=self._font_small)
        self._msg_text.tag_configure("user", foreground="#83c2ff", font=self._font_bold)
        self._msg_text.tag_configure("assistant", foreground=PANEL_TEXT, font=self._font)
        self._msg_text.tag_configure("system", foreground=PANEL_MUTED, font=self._font_small)
        self._msg_text.tag_configure("diag_error", foreground=PANEL_ERROR, font=self._font_bold)
        self._msg_text.tag_configure("diag_warn", foreground=PANEL_WARN, font=self._font_bold)
        self._msg_text.tag_configure("diag_info", foreground=PANEL_ACCENT, font=self._font)
        self._msg_text.tag_configure("skill_cmd", foreground="#83c2ff", font=self._font_mono)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=0)
        self.rowconfigure(2, weight=0)

        self._input_entry = tk.Entry(
            self,
            bg=PANEL_ALT,
            fg=PANEL_TEXT,
            insertbackground=PANEL_TEXT,
            relief="flat",
            font=self._font,
        )
        self._input_entry.grid(row=0, column=0, sticky="ew", ipady=5)
        self._input_entry.bind("<Return>", self._on_send)
        self._input_entry.bind("<KP_Enter>", self._on_send)

        actions = ttk.Frame(self, style="AIInput.TFrame")
        actions.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        actions.columnconfigure(0, weight=1)
        self._mode_label = ttk.Label(
            actions, text="AI: detecting…", style="AIMuted.TLabel", width=12, anchor="w"
        )
        self._mode_label.grid(row=0, column=0, sticky="w")
        self._suggest_button = ttk.Button(
            actions, text="AI建议", style="AISmall.TButton", command=self._request_suggestion
        )
        self._suggest_button.grid(row=0, column=1, sticky="e", padx=(6, 4))
        self._send_button = ttk.Button(actions, text="Send", style="AIAction.TButton", command=self._on_send)
        self._send_button.grid(row=0, column=2, sticky="e")

        self._skill_frame = ttk.Frame(self, style="AIContext.TFrame", padding=(7, 6))
        self._skill_frame.columnconfigure(0, weight=1)
        self._skill_label = ttk.Label(self._skill_frame, text="", style="AIMuted.TLabel", wraplength=250)
        self._skill_label.grid(row=0, column=0, sticky="w")
        self._skill_execute_btn = ttk.Button(
            self._skill_frame, text="Execute", style="AIAction.TButton", command=self._execute_skill
        )
        self._skill_cancel_btn = ttk.Button(
            self._skill_frame, text="Cancel", style="AISmall.TButton", command=self._cancel_skill
        )

    def _connect_monitor(self) -> None:
        self._monitor.set_diagnosis_callback(
            lambda diag: self._schedule(0, lambda: self.context.add_diagnosis(diag))
        )
        self.context.on(
            "diagnosis",
            lambda diagnosis=None, **_: self._schedule(
                0, lambda: self._handle_diagnosis(diagnosis) if diagnosis else None
            ),
        )
        self.context.on("dataset_selected", lambda **_: self._schedule(250, self._refresh_analysis))
        self.context.on(
            "operation_finished",
            lambda success=True, **_: self._schedule(
                350, lambda: self._refresh_analysis(request=success)
            ),
        )

    def _schedule_async_drain(self) -> None:
        while True:
            try:
                kind, payload = self._async_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "analysis":
                    report, generation = payload
                    self._apply_analysis(report, generation)
                elif kind == "analysis_error":
                    self._add_message("diag_warn", f"Artifact analysis failed: {payload}")
                elif kind == "llm":
                    self._set_query_busy(False)
                    if isinstance(payload, tuple) and len(payload) == 3:
                        response, action_context, max_chars = payload
                    elif isinstance(payload, tuple) and len(payload) == 2:
                        response, action_context = payload
                        max_chars = DEFAULT_REPLY_CHARS
                    else:
                        response, action_context, max_chars = payload, {}, DEFAULT_REPLY_CHARS
                    self._apply_llm_response(
                        str(response), dict(action_context or {}), int(max_chars)
                    )
                elif kind == "llm_error":
                    self._set_query_busy(False)
                    self._add_message("diag_warn", f"AI 回复失败：{payload}")
                elif kind == "suggestion":
                    report, response, generation = payload
                    self._apply_suggestion(report, response, generation)
                elif kind == "command":
                    self._show_command_result(*payload)
            except Exception as exc:
                self._add_message("diag_warn", f"AI 消息处理失败：{exc}")
        if not self._destroyed and self.winfo_exists():
            self._schedule(100, self._schedule_async_drain)

    def _collect_analysis_inputs(self) -> tuple[dict, object, object, str, dict]:
        hints = dict(self.context.action_context)
        if self._context_provider:
            hints.update(self._context_provider() or {})
        self.context.update_action_context(hints)
        dataset_path = hints.get("dataset_path") or self.context.dataset_path
        dataset_name = hints.get("dataset_name") or self.context.dataset_name
        return hints, dataset_path, dataset_name, self.context.active_tab, dict(self.context.summary_rows)

    def _collect_suggestion_inputs(self) -> tuple[tuple[dict, object, object, str, dict], dict | None, dict]:
        screen = dict(self.context.action_context)
        if self._context_provider:
            screen.update(self._context_provider() or {})
        self.context.update_action_context(screen)
        operation = self.context.last_operation
        if operation:
            hints = dict(operation.get("action_context") or {})
            hints.update({key: value for key, value in screen.items() if key not in hints})
            dataset_path = operation.get("dataset_path") or hints.get("dataset_path")
            dataset_name = operation.get("dataset") or hints.get("dataset_name")
            active_tab = str(operation.get("tab") or self.context.active_tab)
        else:
            hints = screen
            dataset_path = hints.get("dataset_path") or self.context.dataset_path
            dataset_name = hints.get("dataset_name") or self.context.dataset_name
            active_tab = self.context.active_tab
        analysis_inputs = (
            hints,
            dataset_path,
            dataset_name,
            active_tab,
            dict(self.context.summary_rows),
        )
        return analysis_inputs, operation, screen

    def _analyze_inputs(self, inputs: tuple[dict, object, object, str, dict]) -> AnalysisReport | None:
        if self._analyzer is None:
            return None
        hints, dataset_path, dataset_name, active_tab, summary_rows = inputs
        relevant_hints = {
            key: hints.get(key)
            for key in (
                "dataset_path", "dataset_name", "root", "xds_root", "output",
                "workdir", "basename", "hkl", "res",
            )
            if hints.get(key) not in (None, "")
        }
        cache_key = (
            str(dataset_path or ""),
            str(dataset_name or ""),
            str(active_tab or ""),
            tuple(sorted((key, repr(value)) for key, value in relevant_hints.items())),
            tuple(sorted(str(key) for key in summary_rows)),
        )
        with self._analysis_cache_lock:
            if cache_key in self._analysis_cache:
                return self._analysis_cache[cache_key]
            report = self._analyzer.analyze(
                dataset_path,
                active_tab=active_tab,
                dataset_name=dataset_name,
                summary_rows=summary_rows,
                hints=hints,
            )
            self._analysis_cache[cache_key] = report
            return report

    def _refresh_analysis(self, request: bool = True) -> None:
        with self._analysis_cache_lock:
            self._analysis_cache.clear()
        if request:
            self._request_analysis()

    def _request_analysis(self) -> None:
        if self._analyzer is None:
            return
        self._analysis_generation += 1
        generation = self._analysis_generation
        try:
            inputs = self._collect_analysis_inputs()
        except Exception as exc:
            self._add_message("diag_warn", f"Unable to collect UI context: {exc}")
            return

        def analyze() -> None:
            try:
                self._async_queue.put(("analysis", (self._analyze_inputs(inputs), generation)))
            except Exception as exc:
                self._async_queue.put(("analysis_error", str(exc)))

        threading.Thread(target=analyze, daemon=True).start()

    def _apply_analysis(self, report: AnalysisReport | None, generation: int) -> None:
        if generation == self._analysis_generation:
            self.context.update_artifact_report(report)

    def _request_suggestion(self) -> None:
        self._suggestion_generation += 1
        generation = self._suggestion_generation
        self._suggest_button.configure(state="disabled")
        self._add_message("system", "正在检查上一个操作、日志、界面状态和结果文件…")
        try:
            inputs, operation, screen = self._collect_suggestion_inputs()
        except Exception as exc:
            self._suggest_button.configure(state="normal")
            self._add_message("diag_warn", f"Unable to collect UI context: {exc}")
            return

        def suggest() -> None:
            try:
                report = self._analyze_inputs(inputs)
                max_chars = suggestion_char_limit(self._config.max_suggestion_chars)
                diagnosis_start = int(
                    dict((operation or {}).get("action_context") or {}).get("_diagnosis_start", 0)
                )
                diagnoses = (
                    self._monitor.diagnoses_since(diagnosis_start)
                    if operation
                    else self._monitor.recent_diagnoses(20)
                )
                kwargs = {
                    "operation": operation,
                    "hca_result": self.context.hca_result,
                    "hca_cutoff": self.context.hca_cutoff,
                    "screen": screen,
                    "diagnoses": diagnoses,
                }
                if isinstance(self._llm, NullBackend) or not self._llm.is_available():
                    response = compact_suggestion(report, max_chars, **kwargs)
                else:
                    evidence = suggestion_evidence(
                        report,
                        recent_logs=self._monitor.recent_lines(250),
                        **kwargs,
                    )
                    system = (
                        "你是AutoCrys晶体学操作审查助手。核心任务是检查用户上一个实际完成的操作，"
                        "而不是机械复述当前XDS数据。证据可能来自操作返回值、完整操作日志、规则诊断、"
                        "当前界面状态和产物文件。先识别操作类型（XDS处理、HCA、合并、AutoR3D、"
                        "SHELXT、SHELXL或其他），只依据证据分析，不得编造。HCA必须讨论所用方法、"
                        "距离/CC/共同反射、cutoff与分组；未选择cutoff时不得直接建议合并全部数据。"
                        "本程序中distance<=cutoff才合并，因此减小cutoff更严格、增大cutoff更宽松，"
                        "不得把这个方向说反。"
                        "必须使用简体中文。通常将用户可见回答控制在约300字；若关键信息写不完整可适当超出，"
                        "但必须完整收句，不得为了字数在句中截断。"
                    )
                    response = self._llm.chat(system, evidence)
                    if not response.strip() or (response.startswith("[") and "error" in response.lower()):
                        response = compact_suggestion(report, max_chars, **kwargs)
                    response = limit_text(response, max_chars)
                self._async_queue.put(("suggestion", (report, response, generation)))
            except Exception as exc:
                fallback = compact_suggestion(
                    self.context.artifact_report,
                    suggestion_char_limit(self._config.max_suggestion_chars),
                    operation=self.context.last_operation,
                    hca_result=self.context.hca_result,
                    hca_cutoff=self.context.hca_cutoff,
                    screen=self.context.action_context,
                    diagnoses=self._monitor.recent_diagnoses(20),
                )
                self._async_queue.put(
                    ("suggestion", (self.context.artifact_report, fallback, generation))
                )
                self._async_queue.put(("analysis_error", str(exc)))

        threading.Thread(target=suggest, daemon=True).start()

    def _apply_suggestion(
        self, report: AnalysisReport | None, response: str, generation: int
    ) -> None:
        self._suggest_button.configure(state="normal")
        if generation != self._suggestion_generation:
            return
        self.context.update_artifact_report(report)
        self._add_message(
            "assistant",
            "[AI 建议]\n" + limit_text(
                response, suggestion_char_limit(self._config.max_suggestion_chars)
            ),
        )

    def _reload_llm(self) -> None:
        self._llm = create_llm(self._config._path)
        label = f"AI: {self._llm.mode_name}"
        self._mode_label.configure(text=label)
        if self._mode_callback:
            self._mode_callback(label)

    def _on_send(self, event=None) -> None:
        if self._query_in_flight:
            return
        text = self._input_entry.get().strip()
        if not text:
            return
        self._input_entry.delete(0, "end")
        self._add_message("user", text)
        self._process_message(text)

    def _process_message(self, text: str) -> None:
        action_context = dict(self.context.action_context)
        if self._context_provider:
            try:
                action_context.update(self._context_provider() or {})
            except Exception:
                pass
        action_context.setdefault("dataset_name", self.context.dataset_name)
        self.context.update_action_context(action_context)
        if not isinstance(self._llm, NullBackend):
            self._add_message("system", "正在回复…")
            self._run_llm_query(text, action_context)
            return
        matches = self._skills.match(text, action_context)
        if matches and matches[0][1]:
            skill, params = matches[0]
            if int(self._skills._match_score(skill, params) * 100) >= 30:
                missing = skill.missing_params(params)
                if missing:
                    self._add_message("diag_warn", "暂不执行：缺少或无效参数：" + ", ".join(missing))
                    return
                self._show_skill_confirmation(skill, params)
                return
        if isinstance(self._llm, NullBackend):
            self._fallback_response(text)
        else:
            self._run_llm_query(text)

    def _show_skill_confirmation(self, skill: SkillAction, params: dict) -> None:
        self._pending_skill = (skill, params)
        self._pending_command = skill.build_command(**params)
        self._pending_argv = skill.build_argv(**params)
        datasets = params.get("datasets") or []
        confirmation = skill.description
        if datasets:
            confirmation += f"\nDatasets ({len(datasets)}): {', '.join(map(str, datasets))}"
        self._skill_label.configure(text=confirmation)
        self._skill_execute_btn.grid(row=0, column=1, padx=(8, 0))
        self._skill_cancel_btn.grid(row=0, column=2, padx=(4, 0))
        self._skill_frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._add_message("system", f"Matched skill: {skill.description}")
        if datasets:
            self._add_message("system", f"Datasets ({len(datasets)}): {', '.join(map(str, datasets))}")
        if self._verbose_console:
            self._add_message("skill_cmd", self._pending_command)

    def _execute_skill(self) -> None:
        if not self._pending_command:
            return
        cmd = self._pending_command
        argv = list(self._pending_argv or [])
        skill = self._pending_skill[0] if self._pending_skill else None
        if self._verbose_console:
            self._add_message("system", f"Executing: {cmd}")
        else:
            self._add_message("system", "Executing selected skill…")
        self._clear_pending_skill()
        if self._run_command_cb:
            self._run_command_cb(argv, skill)
        else:
            self._run_in_thread(cmd)

    def _clear_pending_skill(self) -> None:
        self._pending_skill = None
        self._pending_command = None
        self._pending_argv = None
        self._skill_frame.grid_remove()
        self._skill_execute_btn.grid_remove()
        self._skill_cancel_btn.grid_remove()

    def _cancel_skill(self) -> None:
        self._clear_pending_skill()
        self._add_message("system", "Command cancelled.")

    def _run_in_thread(self, cmd: str) -> None:
        def run() -> None:
            self._async_queue.put(("command", self._skills.run_command(cmd)))

        threading.Thread(target=run, daemon=True).start()

    def _show_command_result(self, rc: int, stdout: str, stderr: str) -> None:
        if rc == 0:
            self._add_message("system", "Command completed successfully.")
            if stdout:
                self._add_message("assistant", stdout.strip()[-1000:])
        else:
            self._add_message("diag_error", f"Command failed (exit code {rc})")
            self._add_message("diag_error", (stderr or stdout).strip()[:800])

    def _fallback_response(self, text: str) -> None:
        self._add_message(
            "assistant",
            limit_text(
                offline_response(text, self._knowledge, self.context.artifact_report, self._llm),
                response_char_limit(text),
            ),
        )

    def _apply_llm_response(
        self, response: str, action_context: dict, max_chars: int = DEFAULT_REPLY_CHARS
    ) -> None:
        response, task_result, result_error = self._extract_task_result(response)
        if getattr(self._llm, "manages_own_skills", False) is True:
            visible, skill, params, error = response.strip(), None, {}, None
        else:
            visible, skill, params, error = self._skills.parse_model_response(
                response, action_context
            )
        if visible:
            self._add_message("assistant", limit_text(visible, max_chars))
        if result_error:
            self._add_message("diag_warn", result_error)
        elif task_result is not None and self._task_result_callback:
            self._task_result_callback(task_result)
        if error:
            self._add_message("diag_warn", error)
            return
        if skill is None:
            return
        missing = skill.missing_params(params)
        if missing:
            self._add_message(
                "diag_warn",
                "AI selected a skill but required parameters are missing: " + ", ".join(missing),
            )
            return
        self._show_skill_confirmation(skill, params)

    @staticmethod
    def _extract_task_result(response: str) -> tuple[str, dict | None, str | None]:
        matches = list(TASK_RESULT_PATTERN.finditer(response or ""))
        if not matches:
            return str(response or ""), None, None
        match = matches[-1]
        visible = (response[: match.start()] + response[match.end() :]).strip()
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            return visible, None, f"Agent completion JSON is invalid: {exc.msg}"
        if not isinstance(payload, dict):
            return visible, None, "Agent completion must be a JSON object."
        module = str(payload.get("module") or "").strip()
        if module not in {"AutoXDS", "AutoSolve", "AutoR3D"}:
            return visible, None, f"Agent completion has an unknown module: {module or '(empty)'}"
        if not isinstance(payload.get("success"), bool):
            return visible, None, "Agent completion success must be true or false."
        if not str(payload.get("summary") or payload.get("output") or "").strip():
            return visible, None, "Agent completion is missing summary/output."
        payload["module"] = module
        return visible, payload, None

    def _run_llm_query(self, text: str, action_context: dict | None = None) -> None:
        self._set_query_busy(True)
        action_context = dict(action_context or {})
        max_chars = response_char_limit(text)
        agent_managed_skills = getattr(self._llm, "manages_own_skills", False) is True
        system_prompt = (
            self._agent_bridge_prompt
            if agent_managed_skills
            else self._system_prompt
        )

        def query() -> None:
            try:
                context_text = self.context.build_prompt_context(action_context)
                full_msg = grounded_user_message(context_text, text)
                response = self._llm.chat(system_prompt, full_msg)
                if not str(response or "").strip():
                    raise RuntimeError("模型返回了空内容")
                requested = (
                    []
                    if agent_managed_skills
                    else self._requested_context_modules(str(response))
                )
                if requested:
                    if not hasattr(self, "_knowledge") or not hasattr(self, "_action_catalog"):
                        raise RuntimeError("知识模块加载器不可用")
                    expanded_prompt = self._knowledge.build_system_prompt(
                        self._action_catalog,
                        modules=requested,
                    )
                    response = self._llm.chat(expanded_prompt, full_msg)
                    if not str(response or "").strip():
                        raise RuntimeError("加载知识后模型返回了空内容")
                    if self._requested_context_modules(str(response)):
                        raise RuntimeError("模型重复请求知识模块")
                self._async_queue.put(("llm", (response, action_context, max_chars)))
            except Exception as exc:
                self._async_queue.put(("llm_error", str(exc)))

        threading.Thread(target=query, daemon=True).start()

    def _set_query_busy(self, busy: bool) -> None:
        self._query_in_flight = bool(busy)
        state = "disabled" if busy else "normal"
        if hasattr(self, "_input_entry"):
            self._input_entry.configure(state=state)
        if hasattr(self, "_send_button"):
            self._send_button.configure(state=state)
        if not busy and hasattr(self, "_input_entry"):
            self._input_entry.focus_set()

    def _requested_context_modules(self, response: str) -> list[str]:
        """Validate the model's one-shot request for cached SKILL documentation."""
        matches = list(CONTEXT_REQUEST_PATTERN.finditer(response or ""))
        if not matches:
            return []
        try:
            payload = json.loads(matches[-1].group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"知识模块请求格式错误：{exc.msg}") from exc
        requested = payload.get("modules", []) if isinstance(payload, dict) else []
        if not isinstance(requested, list):
            raise RuntimeError("知识模块请求必须使用 modules 列表")
        allowed = set(self._knowledge.modules()) if hasattr(self, "_knowledge") else set()
        modules: list[str] = []
        for value in requested:
            name = str(value).strip()
            if name not in allowed:
                raise RuntimeError(f"模型请求了未知知识模块：{name or '(empty)'}")
            if name not in modules:
                modules.append(name)
        if not modules:
            raise RuntimeError("模型请求的知识模块为空")
        return modules

    def _handle_diagnosis(self, diag: Diagnosis) -> None:
        if diag is None:
            return
        body = f"{diag.severity_icon} {diag.title}: {diag.detail}"
        if diag.suggestion:
            body += f"\n建议：{diag.suggestion}"
        self._add_message(diag.severity_tag, body)

    def _add_message(self, tag: str, text: str) -> None:
        if self._msg_text is None:
            return
        prefix = {"user": "[You] ", "assistant": "[AI] ", "system": "[AI] "}.get(tag, "")
        self._append_msg(f"{datetime.now().strftime('%H:%M:%S')}  ", "timestamp")
        self._append_msg(prefix + str(text).rstrip() + "\n", tag)
        self._msg_text.see("end")

    def _append_msg(self, text: str, tag: str) -> None:
        if self._msg_text is None:
            return
        state = str(self._msg_text.cget("state"))
        if state == "disabled":
            self._msg_text.configure(state="normal")
        self._msg_text.insert("end", text, tag)
        if state == "disabled":
            self._msg_text.configure(state="disabled")

    def add_chat_message(self, message: str, role: str = "user") -> None:
        self._add_message(role, message)

    def add_diagnosis(self, diag: Diagnosis) -> None:
        self._handle_diagnosis(diag)

    def disable(self) -> None:
        self._monitor.active = False

    def enable(self) -> None:
        self._monitor.active = True

    def clear(self) -> None:
        if self._msg_text is not None:
            self._msg_text.delete("1.0", "end")
        self._monitor.clear()
        self.context.diagnosis_count = 0
