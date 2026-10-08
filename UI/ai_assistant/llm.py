"""LLM abstraction layer - supports offline, local (Ollama), and cloud (OpenAI/Anthropic) modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error


def _text_value(value: object) -> str:
    """Extract text from common Chat Completions and Responses content shapes."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "value", "content"):
            text = _text_value(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        return "".join(_text_value(item) for item in value).strip()
    return ""


def _uses_max_completion_tokens(model: str) -> bool:
    """GPT-5-family models reject max_tokens and any temperature but the default."""
    return "gpt-5" in model.strip().lower()


def _openai_response_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices", [])
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        if isinstance(message, dict):
            text = _text_value(message.get("content"))
            if text:
                return text
            text = _text_value(message.get("reasoning_content"))
            if text:
                return text
        text = _text_value(choice.get("text")) if isinstance(choice, dict) else ""
        if text:
            return text
    text = _text_value(data.get("output_text"))
    if text:
        return text
    return _text_value(data.get("output"))


class LLMBackend(ABC):
    @abstractmethod
    def chat(self, system_prompt: str, user_message: str) -> str:
        ...

    def is_available(self) -> bool:
        return True

    @property
    def manages_own_skills(self) -> bool:
        """Whether this backend owns skill discovery and tool execution."""
        return False

    @property
    @abstractmethod
    def mode_name(self) -> str:
        ...


class NullBackend(LLMBackend):
    def chat(self, system_prompt: str, user_message: str) -> str:
        return (
            "当前为离线模式。我可以使用内置知识库回答基础问题；"
            "如需更深入分析，请在设置中启用本地模型或云端模型。"
        )

    @property
    def mode_name(self) -> str:
        return "offline"


class OllamaBackend(LLMBackend):
    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "",
        model_pattern: str = r"(?i)(instruct|qwen|llama|gemma|mistral)",
    ):
        self._host = host.rstrip("/")
        self._model = model
        self._model_pattern = model_pattern
        self._resolved_model: str | None = None

    def available_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self._host}/api/tags", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [str(item.get("name")) for item in data.get("models", []) if item.get("name")]
        except Exception:
            return []

    def resolve_model(self) -> str:
        if self._resolved_model:
            return self._resolved_model
        models = self.available_models()
        if self._model and self._model in models:
            self._resolved_model = self._model
            return self._model
        if self._model:
            base = self._model.split(":", 1)[0]
            matches = [name for name in models if name == base or name.startswith(base + ":")]
            if matches:
                self._resolved_model = matches[0]
                return self._resolved_model
        try:
            pattern_matches = [name for name in models if re.search(self._model_pattern, name)]
        except re.error:
            pattern_matches = []
        # Prefer the configured regular-expression match, then an instruction
        # model, then the first model reported by Ollama.
        instruct = [name for name in models if "instruct" in name.lower()]
        self._resolved_model = (pattern_matches or instruct or models or [self._model or "llama3.2"])[0]
        return self._resolved_model

    def chat(self, system_prompt: str, user_message: str) -> str:
        url = f"{self._host}/api/chat"
        payload = json.dumps({
            "model": self.resolve_model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.2, "num_predict": 2048},
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                message = data.get("message", {})
                content = message.get("content", "")
                if content:
                    return content
                return "[Ollama returned an empty response; try the request again or select another instruct model.]"
        except urllib.error.URLError as e:
            return f"[Ollama error: {e}]"
        except Exception as e:
            return f"[Ollama error: {e}]"

    def is_available(self) -> bool:
        return bool(self.available_models())

    @property
    def model_name(self) -> str:
        return self.resolve_model()

    @property
    def mode_name(self) -> str:
        return "local"


class OpenAIBackend(LLMBackend):
    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        provider: str = "openai",
        request_model: str = "",
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._provider = provider
        self._request_model = request_model or model

    def chat(self, system_prompt: str, user_message: str) -> str:
        if not self._api_key:
            return "[Error: OpenAI API key not configured]"
        url = f"{self._base_url}/chat/completions"
        payload_data = {
            "model": self._request_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if _uses_max_completion_tokens(self._request_model):
            payload_data["max_completion_tokens"] = 4096
        else:
            payload_data["temperature"] = 0.3
            payload_data["max_tokens"] = 2048
        payload = json.dumps(payload_data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("success") is False:
                    message = str(data.get("message") or f"code={data.get('code')}")
                    return f"[API error: {message[:200]}]"
                text = _openai_response_text(data)
                if text:
                    return text
                choices = data.get("choices", []) if isinstance(data, dict) else []
                finish_reason = ""
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    finish_reason = str(choices[0].get("finish_reason") or "")
                detail = f", finish_reason={finish_reason}" if finish_reason else ""
                return f"[API error: model returned an empty response{detail}]"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return f"[API error {e.code}: {body[:200]}]"
        except Exception as e:
            return f"[API error: {e}]"

    def is_available(self) -> bool:
        return bool(self._api_key)

    @property
    def mode_name(self) -> str:
        return "cloud"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def request_model_name(self) -> str:
        return self._request_model


class AnthropicBackend(LLMBackend):
    def __init__(self, api_key: str = "", model: str = "claude-sonnet-4-20250514"):
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.anthropic.com/v1"

    def chat(self, system_prompt: str, user_message: str) -> str:
        if not self._api_key:
            return "[Error: Anthropic API key not configured]"
        url = f"{self._base_url}/messages"
        payload = json.dumps({
            "model": self._model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
            "max_tokens": 2048,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("content", [])
                if content:
                    return content[0].get("text", "")
                return ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return f"[API error {e.code}: {body[:200]}]"
        except Exception as e:
            return f"[API error: {e}]"

    def is_available(self) -> bool:
        return bool(self._api_key)

    @property
    def mode_name(self) -> str:
        return "cloud"


class OpenClawAgentBackend(LLMBackend):
    """Delegate chats to a local OpenClaw gateway agent turn.

    The OpenClaw agent picks its own skills and tools, so a reply can take much
    longer than a single chat completion and needs a generous timeout.
    """

    def __init__(
        self,
        command: str = "openclaw",
        agent_id: str = "main",
        session_key: str = "autocrys",
        thinking: str = "",
        timeout_seconds: int = 600,
    ):
        self._command = command.strip() or "openclaw"
        self._agent_id = agent_id.strip()
        self._session_key = session_key.strip()
        self._thinking = thinking.strip()
        try:
            self._timeout = max(30, int(timeout_seconds))
        except (TypeError, ValueError):
            self._timeout = 600

    def chat(self, system_prompt: str, user_message: str) -> str:
        message = str(user_message or "").strip()
        if str(system_prompt or "").strip():
            message = f"{system_prompt.strip()}\n\n{message}"
        if not message:
            return "[OpenClaw error: empty message]"
        path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".md", prefix="autocrys-agent-", delete=False
            ) as handle:
                handle.write(message)
                path = handle.name
            argv = [self._command, "agent", "--json", "--message-file", path]
            if self._agent_id:
                argv += ["--agent", self._agent_id]
            if self._session_key:
                argv += ["--session-key", self._session_key]
            if self._thinking:
                argv += ["--thinking", self._thinking]
            argv += ["--timeout", str(self._timeout)]
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self._timeout + 30,
                )
            except FileNotFoundError:
                return f"[OpenClaw error: command not found: {self._command}]"
            except subprocess.TimeoutExpired:
                return f"[OpenClaw error: agent turn exceeded {self._timeout}s]"
            return self._reply_text(proc)
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    @staticmethod
    def _reply_text(proc: object) -> str:
        stdout = str(getattr(proc, "stdout", "") or "").strip()
        stderr = str(getattr(proc, "stderr", "") or "").strip()
        data: object = {}
        if stdout:
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = {}
        if isinstance(data, dict):
            result = data.get("result", {})
            if isinstance(result, dict):
                text = str(result.get("finalAssistantVisibleText") or "").strip()
                if text:
                    return text
                payloads = result.get("payloads", [])
                if isinstance(payloads, list):
                    text = "\n".join(
                        str(item.get("text")).strip()
                        for item in payloads
                        if isinstance(item, dict) and str(item.get("text") or "").strip()
                    )
                    if text:
                        return text
            status = str(data.get("status") or "")
            if status and status != "ok":
                summary = str(data.get("summary") or "")
                return f"[OpenClaw error: {status}{': ' + summary if summary else ''}]"
        if not stdout and not stderr and getattr(proc, "returncode", 0) == 0:
            return "[OpenClaw error: agent returned an empty response]"
        detail = stderr or stdout or f"exit code {getattr(proc, 'returncode', '?')}"
        return f"[OpenClaw error: {detail[:200]}]"

    def is_available(self) -> bool:
        if "/" in self._command or os.path.sep in self._command:
            return os.path.exists(self._command)
        return shutil.which(self._command) is not None

    @property
    def manages_own_skills(self) -> bool:
        return True

    @property
    def mode_name(self) -> str:
        return "agent"

    @property
    def model_name(self) -> str:
        return self._session_key or "openclaw"

    @property
    def provider_name(self) -> str:
        return "openclaw"


def _active_cloud_profile(cloud: dict) -> dict:
    """Resolve a named cloud profile while preserving the legacy flat format."""
    profiles = cloud.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return cloud

    requested = str(
        os.environ.get("AUTOCRYS_LLM_PROFILE")
        or cloud.get("active_profile")
        or next(iter(profiles))
    ).strip()
    selected = profiles.get(requested)
    if isinstance(selected, dict):
        return selected
    # A misspelled profile must not silently consume a different provider's key.
    return {
        "provider": "invalid-profile",
        "api_key": "",
        "api_key_env": "__AUTOCRYS_INVALID_PROFILE__",
        "model": requested or "invalid-profile",
    }


def _cloud_backend(cloud: dict) -> LLMBackend:
    cloud = _active_cloud_profile(cloud)
    provider = str(cloud.get("provider", "deepseek")).lower()
    default_env = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(provider, f"{provider.upper()}_API_KEY")
    env_name = str(cloud.get("api_key_env") or default_env)
    api_key = str(cloud.get("api_key") or os.environ.get(env_name, ""))
    if provider == "anthropic":
        return AnthropicBackend(
            api_key=api_key,
            model=str(cloud.get("model") or "claude-sonnet-4-20250514"),
        )
    if provider == "deepseek":
        model = str(cloud.get("model") or "deepseek-flash")
        return OpenAIBackend(
            api_key=api_key,
            model=model,
            base_url=str(cloud.get("base_url") or "https://api.deepseek.com"),
            provider="deepseek",
            request_model=str(cloud.get("request_model") or model),
        )
    model = str(cloud.get("model") or "gpt-4o")
    return OpenAIBackend(
        api_key=api_key,
        model=model,
        base_url=str(cloud.get("base_url") or "https://api.openai.com/v1"),
        provider=provider,
        request_model=str(cloud.get("request_model") or model),
    )


def create_llm(config_path: Path | None = None) -> LLMBackend:
    if config_path is None:
        config_path = Path(__file__).resolve().parents[2] / "config" / "ai_assistant.json"

    try:
        if not config_path.exists():
            return NullBackend()
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, IOError):
        return NullBackend()

    llm_cfg = cfg.get("llm", {})
    mode = str(llm_cfg.get("mode", "auto")).lower()
    local = llm_cfg.get("local", {})
    ollama = OllamaBackend(
        host=local.get("host", "http://localhost:11434"),
        model=local.get("model", ""),
        model_pattern=local.get("model_pattern", r"(?i)(instruct|qwen|llama|gemma|mistral)"),
    )

    if mode == "local":
        return ollama
    elif mode == "cloud":
        return _cloud_backend(llm_cfg.get("cloud", {}))
    elif mode == "agent":
        agent = llm_cfg.get("agent", {})
        if not isinstance(agent, dict):
            agent = {}
        return OpenClawAgentBackend(
            command=str(agent.get("command") or "openclaw"),
            agent_id=str(agent.get("agent_id") or "main"),
            session_key=str(agent.get("session_key") or "autocrys"),
            thinking=str(agent.get("thinking") or ""),
            timeout_seconds=agent.get("timeout_seconds") or 600,
        )
    elif mode == "auto":
        if ollama.is_available():
            return ollama
        cloud = _cloud_backend(llm_cfg.get("cloud", {}))
        if cloud.is_available():
            return cloud
    return NullBackend()
