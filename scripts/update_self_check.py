#!/usr/bin/env python3
"""Audit and minimally adapt an existing AutoCrys deployment.

The default mode is read-only.  ``--apply`` updates an existing Conda
environment in place, refreshes the editable install, repairs executable bits,
does not download or install third-party executables, and adapts
an existing agent-mode AI config to an installed NVM OpenClaw command.

This script intentionally does not install/update WSL, create a missing Conda
environment, install XDS, start services, or print API keys.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_FILE = PROJECT_ROOT / "config" / "environment-autocrys.yml"
LOCAL_AI_CONFIG = PROJECT_ROOT / "config" / "ai_assistant.json"
SHELXL = PROJECT_ROOT / "AutoSolve" / "tools" / "shelxl"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    action: str = ""


def _run(
    argv: Iterable[str | os.PathLike[str]],
    *,
    timeout: int = 20,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _which_or_file(value: str) -> Path | None:
    candidate = Path(value).expanduser()
    if (candidate.is_absolute() or "/" in value) and candidate.is_file():
        # Preserve launcher symlinks (notably NVM's bin/openclaw).  Resolving
        # them into lib/node_modules would lose the sibling node executable
        # that must be prepended to PATH.
        return candidate.absolute()
    found = shutil.which(value)
    return Path(found).absolute() if found else None


def _dials_python() -> Path | None:
    configured = os.environ.get("DIALS_PYTHON", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    if sys.platform.startswith("linux"):
        home = Path.home()
        candidates.extend([
            home / "miniconda3" / "envs" / "dials" / "bin" / "python",
            home / "miniforge3" / "envs" / "dials" / "bin" / "python",
            home / "mambaforge" / "envs" / "dials" / "bin" / "python",
            home / "anaconda3" / "envs" / "dials" / "bin" / "python",
            Path("/mnt/c/dials/python.exe"),
            Path("/mnt/c/DIALS/python.exe"),
        ])
    found = shutil.which("dials.python") or shutil.which("dials")
    if found:
        candidates.append(Path(found).resolve())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_conda() -> Path | None:
    found = shutil.which("conda")
    if found:
        return Path(found).resolve()
    home = Path.home()
    for path in (
        home / "miniconda3" / "bin" / "conda",
        home / "miniforge3" / "bin" / "conda",
        home / "mambaforge" / "bin" / "conda",
        home / "anaconda3" / "bin" / "conda",
    ):
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    return None


def conda_environments(conda: Path | None) -> list[Path]:
    if conda is None:
        return []
    try:
        completed = _run([conda, "env", "list", "--json"])
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    return [Path(value).resolve() for value in payload.get("envs", []) if value]


def choose_autocrys_environment(envs: list[Path]) -> Path | None:
    exact = [path for path in envs if path.name.lower() == "autocrys"]
    if exact:
        return exact[0]
    active = os.environ.get("CONDA_PREFIX", "").strip()
    if active:
        active_path = Path(active).resolve()
        active_python = active_path / "bin" / "python"
        if active_python.is_file():
            # A non-standard environment name is acceptable only when it is
            # already an AutoCrys environment.  Never reinterpret base or an
            # unrelated active environment and update it by accident.
            try:
                probe = _run(
                    [active_python, "-c", "import AutoR3D; print(AutoR3D.__file__)"],
                    timeout=10,
                )
                if probe.returncode == 0:
                    return active_path
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None


def shell_candidates() -> list[Path]:
    values = [os.environ.get("SHELL", ""), shutil.which("bash") or "", shutil.which("bash2") or ""]
    result: list[Path] = []
    for value in values:
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_file() and path not in result:
            result.append(path.resolve())
    return result


def shell_profile(shell: Path | None) -> Path:
    name = shell.name.lower() if shell else ""
    if "zsh" in name:
        return Path.home() / ".zshrc"
    if "bash" in name:
        return Path.home() / ".bashrc"
    return Path.home() / ".profile"


def _version_key(path: Path) -> tuple[int, ...]:
    text = path.parents[1].name.lstrip("v") if len(path.parents) > 1 else ""
    values = tuple(int(value) for value in re.findall(r"\d+", text))
    return values or (0,)


def configured_openclaw_command() -> tuple[str, str]:
    if not LOCAL_AI_CONFIG.is_file():
        return "", ""
    try:
        data = json.loads(LOCAL_AI_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    llm = data.get("llm", {}) if isinstance(data, dict) else {}
    agent = llm.get("agent", {}) if isinstance(llm, dict) else {}
    mode = str(llm.get("mode", "auto")) if isinstance(llm, dict) else "auto"
    command = str(agent.get("command", "openclaw")) if isinstance(agent, dict) else "openclaw"
    return mode, command


def openclaw_candidates(configured: str) -> list[Path]:
    values: list[Path] = []
    if configured:
        resolved = _which_or_file(configured)
        if resolved:
            values.append(resolved)
    resolved = _which_or_file("openclaw")
    if resolved:
        values.append(resolved)
    home = Path.home()
    nvm = sorted(home.glob(".nvm/versions/node/*/bin/openclaw"), key=_version_key, reverse=True)
    values.extend(path.absolute() for path in nvm if path.is_file())
    for path in (
        home / ".local" / "bin" / "openclaw",
        home / "bin" / "openclaw",
        home / ".openclaw" / "tmp" / "agent-cli" / "openclaw",
    ):
        if path.is_file():
            values.append(path.absolute())
    result: list[Path] = []
    for path in values:
        if path not in result:
            result.append(path)
    return result


def command_environment(command: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(command.parent) + os.pathsep + env.get("PATH", "")
    return env


def working_openclaw(configured: str) -> tuple[Path | None, str, bool]:
    configured_path = _which_or_file(configured) if configured else None
    for path in openclaw_candidates(configured):
        try:
            completed = _run([path, "--version"], timeout=10, env=command_environment(path))
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            return path, _first_line(completed.stdout or completed.stderr), path == configured_path
    return None, "", False


def audit() -> tuple[list[Check], dict[str, object]]:
    checks: list[Check] = []
    context: dict[str, object] = {}

    is_linux = sys.platform.startswith("linux")
    proc_version = Path("/proc/version").read_text(encoding="utf-8", errors="replace") if Path("/proc/version").is_file() else ""
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME") or "microsoft" in proc_version.lower())
    checks.append(Check("platform", "ok" if is_linux else "fail", f"{sys.platform}; WSL={is_wsl}; arch={platform.machine()}", "Run AutoCrys inside WSL/Linux." if not is_linux else ""))

    shells = shell_candidates()
    shell = shells[0] if shells else None
    context["shell"] = shell
    if shell:
        try:
            version = _first_line((_run([shell, "--version"]).stdout))
        except (OSError, subprocess.TimeoutExpired):
            version = "version unavailable"
        alternatives = ", ".join(str(item) for item in shells)
        checks.append(Check("shell", "ok", f"selected={shell}; {version}; detected={alternatives}"))
    else:
        checks.append(Check("shell", "warn", "Neither the configured shell, bash, nor bash2 was found.", "Install Bash or run the Python updater directly."))

    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    checks.append(Check("wslg_display", "ok" if display else "warn", f"DISPLAY/WAYLAND={display or 'not set'}", "GUI needs WSLg or another X/Wayland display." if not display else ""))

    conda = find_conda()
    context["conda"] = conda
    checks.append(Check("conda", "ok" if conda else "warn", str(conda) if conda else "not found", "Keep an existing Conda installation or install Linux Miniconda." if not conda else ""))
    envs = conda_environments(conda)
    prefix = choose_autocrys_environment(envs)
    context["prefix"] = prefix
    if prefix:
        env_python = prefix / "bin" / "python"
        completed = _run([env_python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3]))); print(sys.executable)"])
        version_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        version = version_lines[0] if version_lines else "unknown"
        compatible = completed.returncode == 0 and version.startswith("3.12.")
        checks.append(Check("autocrys_env", "ok" if compatible else "warn", f"prefix={prefix}; Python={version}", "Use --apply to update this existing environment in place to the project specification." if not compatible else ""))
        imports = _run([env_python, "-c", "import numpy, PIL, scipy, matplotlib, tkinter; print('imports ok')"])
        checks.append(Check("python_dependencies", "ok" if imports.returncode == 0 else "warn", _first_line(imports.stdout or imports.stderr), "Use --apply to update dependencies in place." if imports.returncode else ""))
        package = _run([env_python, "-c", "import AutoR3D, UI, AutoXDS, AutoSolve, AutoRefine; print(AutoR3D.__file__)"])
        package_path = _first_line(package.stdout or package.stderr)
        editable = package.returncode == 0 and str(PROJECT_ROOT) in package_path
        checks.append(Check("editable_install", "ok" if editable else "warn", package_path or "not importable", "Use --apply to refresh pip install -e for this working tree." if not editable else ""))
        launchers = [prefix / "bin" / name for name in ("autocrys", "autodials", "autor3d", "autocrys-ai", "autocrys-base", "autocrys-main")]
        missing = [path.name for path in launchers if not path.is_file()]
        checks.append(Check("launchers", "ok" if not missing else "warn", "all present" if not missing else "missing: " + ", ".join(missing), "Use --apply to refresh the editable install." if missing else ""))
    else:
        checks.append(Check("autocrys_env", "warn", "No existing Conda environment named AutoCrys was found.", "Create it once from config/environment-autocrys.yml; the updater will not create or delete environments."))

    dials_python = _dials_python()
    if dials_python:
        probe = _run([dials_python, "-c",
                      "import dials, dxtbx, numpy, PIL, scipy; import cctbx; print('dials imports ok')"], timeout=60)
        versions = _run([dials_python, "-c",
                         "import importlib.metadata as m; print(m.version('dials')); print(m.version('dxtbx'))"], timeout=60)
        version = _first_line(versions.stdout) or "unknown"
        status = "ok" if probe.returncode == 0 else "warn"
        action = "" if status == "ok" else "Install/fix the DIALS runtime or set DIALS_PYTHON to a working interpreter."
        checks.append(Check("dials", status, f"{dials_python}; dials={version}; imports={'ok' if probe.returncode == 0 else 'failed'}", action))
    else:
        checks.append(Check("dials", "warn", "No DIALS Python found (set DIALS_PYTHON to enable the AutoDials tab)",
                            "Install DIALS (e.g. C:\\dials or a conda env) and point DIALS_PYTHON at its python executable."))
    if is_wsl:
        windows_dials = Path("/mnt/c/dials/python.exe")
        checks.append(Check("dials_winpath", "ok" if windows_dials.is_file() else "warn",
                            str(windows_dials) if windows_dials.is_file() else "not found",
                            "For Windows DIALS from WSL, keep C:\\dials or set DIALS_PYTHON explicitly." if not windows_dials.is_file() else ""))

    for command in ("xds", "xscale", "xdsconv", "shelxt"):
        path = shutil.which(command)
        action = "Add the existing Linux executable directory to PATH."
        checks.append(Check(command, "ok" if path else "warn", path or "not on current PATH", "" if path else action))

    shelxl_ok = SHELXL.is_file()
    executable = shelxl_ok and os.access(SHELXL, os.X_OK)
    checks.append(Check("shelxl", "ok" if executable else ("warn" if shelxl_ok else "fail"), f"{SHELXL}; executable={executable}", "Use --apply to restore the executable bit." if shelxl_ok and not executable else ""))

    mode, configured = configured_openclaw_command()
    openclaw, version, configured_works = working_openclaw(configured)
    context.update({"openclaw": openclaw, "openclaw_mode": mode, "openclaw_configured": configured, "openclaw_configured_works": configured_works})
    if openclaw:
        status = "ok" if mode != "agent" or configured_works else "warn"
        action = "Use --apply to create a PATH-safe wrapper and update the existing agent-mode config." if status == "warn" else ""
        checks.append(Check("openclaw", status, f"mode={mode or 'config missing'}; configured={configured or 'not set'}; found={openclaw}; {version}", action))
        try:
            gateway = _run([openclaw, "gateway", "status"], timeout=15, env=command_environment(openclaw))
            gateway_ok = gateway.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            gateway_ok = False
        checks.append(Check("openclaw_gateway", "ok" if gateway_ok else "warn", "reachable" if gateway_ok else "not confirmed", "Start or repair the existing gateway only if llm.mode is agent." if not gateway_ok and mode == "agent" else ""))
    else:
        checks.append(Check("openclaw", "warn" if mode == "agent" else "ok", f"mode={mode or 'config missing'}; no runnable command found", "Install/enable OpenClaw only when agent mode is required." if mode == "agent" else "Optional; no action needed for non-agent modes."))

    xds_datasets = sum(1 for _ in (PROJECT_ROOT / "Data").rglob("diff/p/XDS.INP")) if (PROJECT_ROOT / "Data").is_dir() else 0
    r3d_params = 0
    if (PROJECT_ROOT / "Data").is_dir():
        r3d_params = sum(1 for path in (PROJECT_ROOT / "Data").rglob("*.txt") if ("cred2" in path.name.lower() or "3d ed" in path.name.lower()))
    checks.append(Check("data_inventory", "ok", f"XDS-ready dataset markers={xds_datasets}; cRED2 parameter files={r3d_params}"))
    return checks, context


def ensure_local_bin_on_path(shell: Path | None) -> tuple[Path, bool]:
    profile = shell_profile(shell)
    marker = "# AutoCrys managed user-local PATH"
    existing = profile.read_text(encoding="utf-8", errors="replace") if profile.is_file() else ""
    if marker in existing:
        return profile, False
    profile.parent.mkdir(parents=True, exist_ok=True)
    with profile.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(f'{marker}\nexport PATH="$HOME/.local/bin:$PATH"\n')
    return profile, True


def write_openclaw_wrapper(openclaw: Path) -> Path:
    target = Path.home() / ".local" / "bin" / "autocrys-openclaw"
    target.parent.mkdir(parents=True, exist_ok=True)
    quoted_bin = str(openclaw.parent).replace("'", "'\"'\"'")
    quoted_cmd = str(openclaw).replace("'", "'\"'\"'")
    target.write_text(
        "#!/bin/sh\n"
        f"PATH='{quoted_bin}':$PATH\n"
        "export PATH\n"
        f"exec '{quoted_cmd}' \"$@\"\n",
        encoding="utf-8",
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def update_agent_command(wrapper: Path) -> bool:
    if not LOCAL_AI_CONFIG.is_file():
        return False
    data = json.loads(LOCAL_AI_CONFIG.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return False
    llm = data.setdefault("llm", {})
    if not isinstance(llm, dict) or str(llm.get("mode", "auto")) != "agent":
        return False
    agent = llm.setdefault("agent", {})
    if not isinstance(agent, dict):
        return False
    if str(agent.get("command", "")) == str(wrapper):
        return False
    agent["command"] = str(wrapper)
    LOCAL_AI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=LOCAL_AI_CONFIG.parent, delete=False) as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, LOCAL_AI_CONFIG)
    return True


def apply_adaptations(context: dict[str, object]) -> list[str]:
    actions: list[str] = []
    conda = context.get("conda")
    prefix = context.get("prefix")
    if isinstance(conda, Path) and isinstance(prefix, Path):
        update = _run([conda, "env", "update", "--prefix", prefix, "--file", ENVIRONMENT_FILE], timeout=1800)
        if update.returncode != 0:
            raise RuntimeError("Conda in-place update failed: " + _first_line(update.stderr or update.stdout))
        actions.append(f"Updated existing Conda environment in place: {prefix}")
        env_python = prefix / "bin" / "python"
        install = _run([env_python, "-m", "pip", "install", "-e", PROJECT_ROOT], timeout=600)
        if install.returncode != 0:
            raise RuntimeError("Editable install failed: " + _first_line(install.stderr or install.stdout))
        actions.append(f"Refreshed editable install with {env_python}")
    else:
        actions.append("Skipped Conda update: no existing AutoCrys environment was detected")

    if SHELXL.is_file() and not os.access(SHELXL, os.X_OK):
        SHELXL.chmod(SHELXL.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        actions.append(f"Restored executable bits: {SHELXL}")

    openclaw = context.get("openclaw")
    mode = str(context.get("openclaw_mode") or "")
    configured_works = bool(context.get("openclaw_configured_works"))
    need_openclaw_wrapper = isinstance(openclaw, Path) and mode == "agent" and not configured_works
    if need_openclaw_wrapper:
        shell = context.get("shell") if isinstance(context.get("shell"), Path) else None
        profile, changed = ensure_local_bin_on_path(shell)
        if changed:
            actions.append(f"Added ~/.local/bin to PATH in {profile}")

    if need_openclaw_wrapper and isinstance(openclaw, Path):
        wrapper = write_openclaw_wrapper(openclaw)
        changed_config = update_agent_command(wrapper)
        actions.append(f"Created PATH-safe OpenClaw wrapper: {wrapper}")
        if changed_config:
            actions.append("Updated llm.agent.command without reading or printing any API key")
    return actions


def print_checks(checks: list[Check]) -> None:
    for item in checks:
        print(f"[{item.status.upper():4}] {item.name}: {item.detail}")
        if item.action:
            print(f"       action: {item.action}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and minimally adapt an existing AutoCrys deployment")
    parser.add_argument("--apply", action="store_true", help="Apply safe in-place adaptations; never rebuild/delete environments or update WSL")
    parser.add_argument("--json", action="store_true", help="Emit the read-only audit as JSON; cannot be combined with --apply")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.json:
        raise SystemExit("--apply and --json cannot be combined")
    checks, context = audit()
    if args.json:
        print(json.dumps([asdict(item) for item in checks], indent=2, ensure_ascii=False))
        return 0
    print("AutoCrys existing-deployment self-check")
    print(f"Project: {PROJECT_ROOT}")
    print_checks(checks)
    if not args.apply:
        print("\nRead-only check complete. Re-run with --apply to perform the listed in-place adaptations.")
        return 0 if not any(item.status == "fail" for item in checks) else 2
    print("\nApplying in-place adaptations...")
    for action in apply_adaptations(context):
        print(f"[DONE] {action}")
    print("\nPost-update self-check")
    post_checks, _ = audit()
    print_checks(post_checks)
    return 0 if not any(item.status == "fail" for item in post_checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
