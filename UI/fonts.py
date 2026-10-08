"""Font discovery and WSL CJK font registration for the Tk interface."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tkinter as tk
from tkinter import font as tkfont


_CJK_UI_FAMILIES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "DengXian",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "SimHei",
    "SimSun",
    "NSimSun",
    "Song Ti",
    # Classic X11 Tk exposes its CJK fallback fonts under these aliases even
    # when it does not list the original Fontconfig family name.
    "FangSong Ti",
    "Gothic",
)
_LATIN_MONO_FAMILIES = (
    "Consolas",
    "Cascadia Mono",
    "DejaVu Sans Mono",
    "Liberation Mono",
    "Courier New",
)
_WSL_WINDOWS_FONTS = (
    "msyh.ttc",
    "msyhbd.ttc",
    "msyhl.ttc",
    "Deng.ttf",
    "Dengb.ttf",
    "Dengl.ttf",
    "simhei.ttf",
    "simsun.ttc",
    "consola.ttf",
    "consolab.ttf",
    "consolai.ttf",
    "consolaz.ttf",
)


def _fontconfig_has_cjk() -> bool:
    """Return whether Fontconfig knows at least one font with Chinese glyphs."""
    if shutil.which("fc-list") is None:
        return False
    try:
        result = subprocess.run(
            ["fc-list", ":lang=zh", "family"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool((result.stdout or "").strip())


def ensure_cjk_fonts() -> bool:
    """Make Windows CJK fonts visible to WSL Fontconfig.

    WSLg does not automatically expose fonts from ``C:\\Windows\\Fonts`` to
    Linux Tk applications.  We create lightweight symlinks in the user's font
    directory and refresh only that directory.  Existing Linux CJK fonts are
    always preferred and no system files are modified.
    """
    if os.name != "posix":
        return _fontconfig_has_cjk()

    windows_fonts = Path("/mnt/c/Windows/Fonts")
    if not windows_fonts.is_dir() or shutil.which("fc-cache") is None:
        return False

    target_dir = Path.home() / ".local" / "share" / "fonts" / "autocrys"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    installed = False
    changed = False
    for filename in _WSL_WINDOWS_FONTS:
        source = windows_fonts / filename
        if not source.is_file():
            continue
        target = target_dir / filename
        installed = True
        if target.exists() or target.is_symlink():
            continue
        try:
            target.symlink_to(source)
        except OSError:
            try:
                shutil.copy2(source, target)
            except OSError:
                continue
        changed = True

    if not installed:
        return False
    if changed or not _fontconfig_has_cjk():
        try:
            subprocess.run(
                ["fc-cache", "-f", str(target_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return _fontconfig_has_cjk()


def _fontconfig_font_file(family: str) -> Path | None:
    """Resolve a family name to a font file through Fontconfig."""
    if shutil.which("fc-match") is None:
        return None
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", family],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def _x11_font_path() -> list[str]:
    """Return the font path currently registered with the X server."""
    try:
        result = subprocess.run(
            ["xset", "q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = (result.stdout or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "Font Path:":
            entries: list[str] = []
            for following in lines[index + 1:]:
                if not following.startswith((" ", "\t")):
                    break
                entries.extend(part for part in following.strip().split(",") if part)
            return entries
    return []


def _keep_unicode_charset_entries(directory: Path) -> bool:
    """Strip legacy 8-bit charset entries from ``fonts.dir``/``fonts.scale``.

    ``mkfontscale`` indexes the Windows fonts with legacy charsets such as
    ``gb2312.1980-0`` and ``cns11643-1``.  The X server rejects many of those
    entries for TrueType collections, and Tk then renders Chinese characters
    as literal ``\\uXXXX`` escapes.  Keeping only Unicode (``iso10646-1``)
    entries forces Tk onto the working Unicode faces.
    """
    rewritten = False
    for filename in ("fonts.dir", "fonts.scale"):
        index = directory / filename
        if not index.is_file():
            continue
        try:
            lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        entries = [line for line in lines[1:] if line.strip()] if lines else []
        if not entries or all("iso10646-1" in line for line in entries):
            continue
        unicode_entries = [line for line in entries if "iso10646-1" in line]
        content = "\n".join([str(len(unicode_entries)), *unicode_entries]) + "\n"
        try:
            index.write_text(content, encoding="utf-8")
        except OSError:
            continue
        rewritten = True
    return rewritten


def register_x11_core_fonts() -> bool:
    """Index CJK and monospace fonts for Tk builds without Xft support.

    Conda's Tk on WSL links only against libX11, so Fontconfig families are
    invisible to ``tkfont.families()`` and every requested family silently
    falls back to ``fixed``.  The font files are symlinked into a directory
    with an X11 ``fonts.dir`` index and appended to the X server font path,
    which makes the real families resolvable again.  Legacy charset entries
    are stripped from the index so Tk keeps using the Unicode faces.
    """
    if os.name != "posix" or not os.environ.get("DISPLAY"):
        return False
    if any(shutil.which(tool) is None for tool in ("xset", "mkfontscale", "mkfontdir")):
        return False

    sources: list[Path] = []
    seen: set[Path] = set()
    windows_fonts = Path("/mnt/c/Windows/Fonts")
    for filename in _WSL_WINDOWS_FONTS:
        candidate = windows_fonts / filename
        if candidate.is_file() and candidate not in seen:
            sources.append(candidate)
            seen.add(candidate)
    for family in _CJK_UI_FAMILIES + _LATIN_MONO_FAMILIES:
        candidate = _fontconfig_font_file(family)
        if candidate is not None and candidate not in seen:
            sources.append(candidate)
            seen.add(candidate)
    if not sources:
        return False

    target_dir = Path.home() / ".local" / "share" / "fonts" / "x11core"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    changed = False
    for source in sources:
        target = target_dir / source.name
        if target.exists() or target.is_symlink():
            continue
        try:
            target.symlink_to(source)
        except OSError:
            try:
                shutil.copy2(source, target)
            except OSError:
                continue
        changed = True

    if changed or not (target_dir / "fonts.dir").is_file():
        for command in (["mkfontscale", str(target_dir)], ["mkfontdir", str(target_dir)]):
            try:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
    _keep_unicode_charset_entries(target_dir)

    existing = set(_x11_font_path())
    candidates = (str(target_dir), f"/mnt/wslg/distro{target_dir}")
    registered = any(candidate in existing for candidate in candidates)
    if not registered:
        for candidate in candidates:
            try:
                result = subprocess.run(
                    ["xset", "+fp", candidate],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                registered = True
                break
    if not registered:
        return False
    try:
        subprocess.run(
            ["xset", "fp", "rehash"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def select_ui_fonts(root: tk.Misc) -> tuple[str, str]:
    """Choose a readable CJK UI face and a distinct Latin monospace face.

    SimSun's Chinese glyphs are usable, but its enlarged Latin glyphs look
    pixelated and make characters such as ``0/O`` and ``1/l/I`` difficult to
    distinguish.  General labels therefore prefer Microsoft YaHei UI, while
    logs, commands, paths, and numeric summaries prefer Consolas.
    """
    ensure_cjk_fonts()
    available = {name.casefold(): name for name in tkfont.families(root)}

    def choose(preferences: tuple[str, ...], fallback: str) -> str:
        for family in preferences:
            match = available.get(family.casefold())
            if match:
                return match
        return fallback

    if not any(family.casefold() in available for family in _LATIN_MONO_FAMILIES):
        if register_x11_core_fonts():
            available = {name.casefold(): name for name in tkfont.families(root)}

    ui_family = choose(_CJK_UI_FAMILIES, "DengXian")
    mono_family = choose(_LATIN_MONO_FAMILIES, "DejaVu Sans Mono")
    return ui_family, mono_family
