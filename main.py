#!/usr/bin/env python3
"""Top-level AutoCrys entrypoint.

Usage examples:
    python main.py
    python main.py ui
    python main.py xds process --all
    python main.py solve solve --dataset experiment_003 --composition "Au4 C20 H16 N2"
    python main.py r3d ui
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent


def _prepend_path(path: Path) -> None:
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def run_ui(argv):
    _prepend_path(ROOT)
    from UI.ui import AutoR3DUI
    import tkinter as tk

    parser = argparse.ArgumentParser(prog="main.py ui")
    parser.add_argument("dataset_dir", nargs="?", default=None)
    parser.add_argument("--frames", default="diff")
    args = parser.parse_args(argv)

    root = tk.Tk()
    # None means the ordinary project launcher follows assistant.enabled in
    # config/ai_assistant.json. Installed variants remain explicit overrides.
    AutoR3DUI(
        root,
        dataset_dir=args.dataset_dir,
        frames_dir=args.frames,
        ai_assistant=None,
        console_verbose=False,
    )
    root.mainloop()
    return 0


def run_ai(argv):
    _prepend_path(ROOT)
    from UI.ui import AutoR3DUI
    import tkinter as tk
    import argparse

    parser = argparse.ArgumentParser(prog="main.py ai")
    parser.add_argument("dataset_dir", nargs="?", default=None)
    parser.add_argument("--frames", default="diff")
    args = parser.parse_args(argv)

    root = tk.Tk()
    AutoR3DUI(
        root,
        dataset_dir=args.dataset_dir,
        frames_dir=args.frames,
        ai_assistant=True,
        console_verbose=False,
    )
    root.mainloop()
    return 0


def run_xds(argv):
    _prepend_path(ROOT / "AutoXDS" / "scripts")
    import auto_xds

    return int(auto_xds.main(argv))


def run_solve(argv):
    _prepend_path(ROOT / "AutoSolve" / "scripts")
    import auto_shelxt

    return int(auto_shelxt.main(argv))


def run_r3d(argv):
    _prepend_path(ROOT)
    from AutoR3D.cli import main as autor3d_main

    return int(autor3d_main(argv))


COMMANDS = {
    "ui": ("Launch integrated AutoCrys desktop UI", run_ui),
    "ai": ("Launch AutoCrys desktop UI with AI assistant", run_ai),
    "xds": ("Run AutoXDS CLI", run_xds),
    "solve": ("Run AutoSolve CLI", run_solve),
    "r3d": ("Run AutoR3D CLI", run_r3d),
}


def build_parser():
    parser = argparse.ArgumentParser(description="AutoCrys unified entrypoint")
    parser.add_argument(
        "command",
        nargs="?",
        choices=sorted(COMMANDS.keys()),
        default="ui",
        help="Sub-command to run (default: ui)",
    )
    parser.add_argument(
        "command_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to the selected sub-command",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    _, handler = COMMANDS[args.command]

    # argparse keeps a leading '--' in REMAINDER; strip it for cleaner passthrough.
    forwarded = args.command_args[1:] if args.command_args[:1] == ["--"] else args.command_args
    return handler(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
