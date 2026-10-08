"""Log monitor - intercepts console output and feeds it to the rule engine."""

from __future__ import annotations

from .rules import Diagnosis, RuleEngine, build_default_engine


class LogMonitor:
    def __init__(self, engine: RuleEngine | None = None):
        self._engine = engine or build_default_engine()
        self._active: bool = True
        self._lines: list[str] = []
        self._diagnosis_history: list[Diagnosis] = []
        self._max_lines: int = 200
        self._on_diagnosis = None

    @property
    def engine(self) -> RuleEngine:
        return self._engine

    @property
    def active(self) -> bool:
        return self._active

    @active.setter
    def active(self, value: bool) -> None:
        self._active = value

    @property
    def max_lines(self) -> int:
        return self._max_lines

    @max_lines.setter
    def max_lines(self, value: int) -> None:
        self._max_lines = max(1, value)

    def set_diagnosis_callback(self, callback) -> None:
        self._on_diagnosis = callback
        self._engine.register_callback(callback)

    def feed(self, text: str, active_tab: str = "") -> list[Diagnosis]:
        if not self._active:
            return []
        lines = text.splitlines()
        self._lines.extend(lines)
        self._lines = self._lines[-self._max_lines:]
        diags = self._engine.feed_lines(lines, active_tab)
        for d in diags:
            self._diagnosis_history.append(d)
        return diags

    def feed_single(self, line: str, active_tab: str = "") -> list[Diagnosis]:
        if not self._active:
            return []
        self._lines.append(line)
        self._lines = self._lines[-self._max_lines:]
        diags = self._engine.feed_line(line, active_tab)
        self._diagnosis_history.extend(diags)
        return diags

    def recent_lines(self, n: int = 50) -> list[str]:
        return self._lines[-n:]

    def recent_diagnoses(self, n: int = 10) -> list[Diagnosis]:
        return self._diagnosis_history[-n:]

    def diagnoses_since(self, index: int) -> list[Diagnosis]:
        return self._diagnosis_history[max(0, int(index)) :]

    def clear(self) -> None:
        self._lines.clear()
        self._diagnosis_history.clear()
        self._engine.reset_cooldowns()

    @property
    def all_lines(self) -> list[str]:
        return list(self._lines)

    @property
    def diagnosis_count(self) -> int:
        return len(self._diagnosis_history)

    def reset_cooldowns(self) -> None:
        self._engine.reset_cooldowns()
