"""AI Assistant configuration loader."""

from __future__ import annotations

from pathlib import Path
import json
import os


class Config:
    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[2] / "config" / "ai_assistant.json"
        self._path = config_path
        self._data = self._load()

    def _load(self) -> dict:
        defaults = self._defaults()
        if not self._path.exists():
            return defaults
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, IOError):
            return defaults
        return self._merge(defaults, loaded)

    @classmethod
    def _merge(cls, defaults: dict, loaded: dict) -> dict:
        """Merge older config files with new defaults without losing choices."""
        result = dict(defaults)
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = cls._merge(result[key], value)
            else:
                result[key] = value
        return result

    def _defaults(self) -> dict:
        return {
            "assistant": {
                "enabled": False,
                "max_suggestion_chars": 300,
            },
            "structure_viewer": {
                "enabled": True,
                "show_unit_cell": True,
                "bond_tolerance_angstrom": 0.45,
                "grow_steps_per_click": 2,
            },
            "llm": {
                "mode": "auto",
                "local": {
                    "provider": "ollama",
                    "host": "http://localhost:11434",
                    "model": "",
                    "model_pattern": "(?i)(instruct|qwen|llama|gemma|mistral)",
                },
                "cloud": {
                    "provider": "deepseek",
                    "api_key": "",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-flash",
                },
                "agent": {
                    "command": "openclaw",
                    "agent_id": "main",
                    "session_key": "autocrys",
                    "thinking": "",
                    "timeout_seconds": 600,
                },
            },
            "auto_diagnose": True,
            "auto_context": True,
            "max_console_lines": 200,
            "language": "zh",
        }

    def save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    @property
    def llm_mode(self) -> str:
        return self._data.get("llm", {}).get("mode", "auto")

    @llm_mode.setter
    def llm_mode(self, value: str) -> None:
        self._data.setdefault("llm", {})["mode"] = value
        self.save()

    @property
    def llm_config(self) -> dict:
        return self._data.get("llm", {})

    @property
    def assistant_enabled(self) -> bool:
        return bool(self._data.get("assistant", {}).get("enabled", False))

    @assistant_enabled.setter
    def assistant_enabled(self, value: bool) -> None:
        self._data.setdefault("assistant", {})["enabled"] = bool(value)
        self.save()

    @property
    def structure_viewer(self) -> dict:
        value = self._data.get("structure_viewer", {})
        return value if isinstance(value, dict) else {}

    @property
    def structure_viewer_enabled(self) -> bool:
        return bool(self.structure_viewer.get("enabled", True))

    @property
    def structure_viewer_show_unit_cell(self) -> bool:
        return bool(self.structure_viewer.get("show_unit_cell", True))

    @property
    def structure_viewer_bond_tolerance(self) -> float:
        try:
            value = float(self.structure_viewer.get("bond_tolerance_angstrom", 0.45))
        except (TypeError, ValueError):
            return 0.45
        return max(0.0, min(value, 1.5))

    @property
    def structure_viewer_grow_steps(self) -> int:
        try:
            value = int(self.structure_viewer.get("grow_steps_per_click", 2))
        except (TypeError, ValueError):
            return 2
        return max(1, min(value, 4))

    @property
    def max_suggestion_chars(self) -> int:
        value = self._data.get("assistant", {}).get("max_suggestion_chars", 300)
        try:
            parsed = int(value)
            return 300 if parsed <= 0 else min(parsed, 12000)
        except (TypeError, ValueError):
            return 300

    @property
    def auto_diagnose(self) -> bool:
        return self._data.get("auto_diagnose", True)

    @auto_diagnose.setter
    def auto_diagnose(self, value: bool) -> None:
        self._data["auto_diagnose"] = value
        self.save()

    @property
    def auto_context(self) -> bool:
        return self._data.get("auto_context", True)

    @property
    def max_console_lines(self) -> int:
        return self._data.get("max_console_lines", 200)

    @property
    def language(self) -> str:
        return self._data.get("language", "zh")

    @property
    def api_key(self) -> str:
        cloud = self._data.get("llm", {}).get("cloud", {})
        profiles = cloud.get("profiles", {})
        if isinstance(profiles, dict) and profiles:
            active = str(
                os.environ.get("AUTOCRYS_LLM_PROFILE")
                or cloud.get("active_profile")
                or next(iter(profiles))
            ).strip()
            profile = profiles.get(active, {})
            return profile.get("api_key", "") if isinstance(profile, dict) else ""
        return cloud.get("api_key", "")
