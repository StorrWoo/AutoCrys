"""Context tracker for AI Assistant - monitors the active state of the AutoCrys UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .artifacts import AnalysisReport


@dataclass
class AIContext:
    active_tab: str = ""
    dataset_path: Path | None = None
    dataset_name: str | None = None
    operation_state: str = "idle"
    operation_name: str = ""
    current_command: str | None = None
    status_text: str = "Ready"
    processing_history: list[dict] = field(default_factory=list)
    summary_rows: dict[str, dict[str, str]] = field(default_factory=dict)
    hca_result: dict | None = None
    hca_cutoff: float | None = None
    console_buffer: list[str] = field(default_factory=list)
    diagnosis_count: int = 0
    monitor_enabled: bool = True
    artifact_report: AnalysisReport | None = None
    action_context: dict = field(default_factory=dict)
    current_operation_result: object | None = None
    _operation_log_start: int = 0
    _operation_snapshot: dict = field(default_factory=dict)
    _current_operation_logs: list[str] = field(default_factory=list)
    _listeners: dict[str, list[Callable]] = field(default_factory=dict)

    def on(self, event: str, callback: Callable) -> None:
        if event not in self._listeners:
            self._listeners[event] = []
        self._listeners[event].append(callback)

    def emit(self, event: str, **data) -> None:
        for callback in self._listeners.get(event, []):
            try:
                callback(**data)
            except Exception:
                pass

    def update_tab(self, tab_name: str) -> None:
        self.active_tab = tab_name
        self.emit("tab_changed", tab=tab_name)

    def update_dataset(self, path: Path | None, name: str | None = None) -> None:
        self.dataset_path = path
        self.dataset_name = name or (path.name if path else None)
        self.emit("dataset_selected", path=path, name=self.dataset_name)

    def start_operation(self, name: str, command: str | None = None) -> None:
        self.operation_state = "processing"
        self.operation_name = name
        self.current_command = command
        self.current_operation_result = None
        self._operation_log_start = len(self.console_buffer)
        self._current_operation_logs = []
        self._operation_snapshot = {
            "name": name,
            "command": command,
            "tab": self.active_tab,
            "dataset": self.dataset_name,
            "dataset_path": str(self.dataset_path) if self.dataset_path else None,
            "action_context": dict(self.action_context),
            "status_before": self.status_text,
        }
        self.emit("operation_started", op=name, cmd=command)

    def record_operation_result(self, name: str, result: object) -> None:
        if not self._operation_snapshot:
            self._operation_snapshot = {
                "name": name,
                "tab": self.active_tab,
                "dataset": self.dataset_name,
                "dataset_path": str(self.dataset_path) if self.dataset_path else None,
                "action_context": dict(self.action_context),
            }
            self._operation_log_start = max(0, len(self.console_buffer) - 200)
            self._current_operation_logs = self.console_buffer[self._operation_log_start :]
        self.current_operation_result = result

    def finish_operation(self, name: str, success: bool = True, error: str | None = None) -> None:
        self.operation_state = "error" if not success else "done"
        self.operation_name = ""
        self.current_command = None
        snapshot = dict(self._operation_snapshot)
        entry = {
            **snapshot,
            "name": name or snapshot.get("name", ""),
            "success": success,
            "error": error,
            "result": self.current_operation_result,
            "logs": self._current_operation_logs[-500:],
            "status_after": self.status_text,
        }
        self.processing_history.append(entry)
        if len(self.processing_history) > 50:
            self.processing_history = self.processing_history[-50:]
        self.emit("operation_finished", op=name, success=success, error=error)
        self._operation_snapshot = {}
        self.current_operation_result = None
        self._current_operation_logs = []

    def set_idle(self) -> None:
        self.operation_state = "idle"
        self.operation_name = ""

    def update_status(self, text: str) -> None:
        self.status_text = text
        self.emit("status_changed", text=text)

    def append_console(self, line: str) -> None:
        self.console_buffer.append(line)
        if len(self.console_buffer) > 500:
            self.console_buffer = self.console_buffer[-500:]
        if self._operation_snapshot:
            self._current_operation_logs.append(line)
            if len(self._current_operation_logs) > 500:
                self._current_operation_logs = self._current_operation_logs[-500:]
        self.emit("console_output", line=line)

    def update_summary_rows(self, rows: dict[str, dict[str, str]]) -> None:
        self.summary_rows = rows

    def update_hca_result(self, result: dict | None, cutoff: float | None = None) -> None:
        self.hca_result = result
        self.hca_cutoff = cutoff
        self.emit("hca_updated", result=result, cutoff=cutoff)

    def update_artifact_report(self, report: AnalysisReport | None) -> None:
        self.artifact_report = report
        self.emit("artifact_analyzed", report=report)

    def update_action_context(self, values: dict | None) -> None:
        self.action_context = dict(values or {})

    def add_diagnosis(self, diagnosis) -> None:
        self.diagnosis_count += 1
        self.emit("diagnosis", diagnosis=diagnosis)

    def get_recent_history(self, n: int = 5) -> list[dict]:
        return self.processing_history[-n:]

    @property
    def last_operation(self) -> dict | None:
        return self.processing_history[-1] if self.processing_history else None

    def build_prompt_context(self, action_context: dict | None = None) -> str:
        """Build a compact in-memory snapshot; never read project files here."""
        screen = dict(self.action_context)
        screen.update(action_context or {})
        allowed_keys = (
            "active_tab", "dataset", "dataset_name", "dataset_path", "datasets",
            "root", "xds_root", "output", "workdir", "hkl", "res", "cell", "sg",
            "composition", "axis", "method", "hca_method", "hca_cutoff", "status_text",
        )
        compact_screen = {
            key: screen[key]
            for key in allowed_keys
            if key in screen and screen[key] not in (None, "", [], {})
        }

        parts = ["[Current UI snapshot]"]
        parts.append(f"Page: {self.active_tab or compact_screen.get('active_tab') or 'N/A'}")
        parts.append(f"Dataset: {self.dataset_name or compact_screen.get('dataset_name') or 'N/A'}")
        parts.append(f"Dataset path: {self.dataset_path or compact_screen.get('dataset_path') or 'N/A'}")
        parts.append(f"Operation: {self.operation_state} {self.operation_name}".strip())
        parts.append(f"Status: {self.status_text}")
        if compact_screen:
            rendered = ", ".join(f"{key}={value}" for key, value in compact_screen.items())
            parts.append(f"Current parameters: {rendered}")
        if self.hca_result:
            parts.append(f"HCA clustering available (cutoff={self.hca_cutoff})")
        if self.summary_rows:
            keys = list(self.summary_rows.keys())[:10]
            parts.append(f"Known datasets ({len(self.summary_rows)}): {', '.join(keys)}")
        if self.processing_history:
            last = self.processing_history[-1]
            parts.append(
                "Last operation: "
                f"name={last.get('name', 'N/A')}, success={last.get('success')}, "
                f"tab={last.get('tab', 'N/A')}, dataset={last.get('dataset', 'N/A')}"
            )
        if self.artifact_report:
            parts.append(self.artifact_report.to_prompt())
        recent_logs = [str(line)[-300:] for line in self.console_buffer[-12:]]
        if recent_logs:
            parts.append(f"Recent log output:\n  " + "\n  ".join(recent_logs))
        return "\n".join(parts)
