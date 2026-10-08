"""AutoCrys launcher with the complete, unfiltered Console output."""

from __future__ import annotations

import argparse
import tkinter as tk

from .ui import AutoR3DUI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autocrys-main",
        description="AutoCrys desktop UI with full Console output",
    )
    parser.add_argument("dataset_dir", nargs="?", default=None)
    parser.add_argument("--frames", default="diff")
    args = parser.parse_args(argv)

    root = tk.Tk()
    AutoR3DUI(
        root,
        dataset_dir=args.dataset_dir,
        frames_dir=args.frames,
        ai_assistant=None,
        console_verbose=True,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
