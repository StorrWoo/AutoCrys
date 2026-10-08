"""AutoCrys AI-powered desktop UI entry point.

Provides: autocrys-ai command (AI controls in the main Assistant workspace).
"""

from __future__ import annotations

import argparse
import tkinter as tk

from .ui import AutoR3DUI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autocrys-ai", description="AutoCrys desktop UI with AI assistant")
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
