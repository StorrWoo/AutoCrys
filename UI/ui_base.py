"""AutoCrys launcher that always disables the AI assistant."""

from __future__ import annotations

import argparse
import tkinter as tk

from .ui import AutoR3DUI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autocrys-base",
        description="AutoCrys desktop UI without the AI assistant",
    )
    parser.add_argument("dataset_dir", nargs="?", default=None)
    parser.add_argument("--frames", default="diff")
    args = parser.parse_args(argv)

    root = tk.Tk()
    AutoR3DUI(
        root,
        dataset_dir=args.dataset_dir,
        frames_dir=args.frames,
        ai_assistant=False,
        console_verbose=False,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
