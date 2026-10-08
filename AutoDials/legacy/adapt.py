"""One-command cRED2 conversion and DIALS reduction; invoke with DIALS Python."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--xparm", type=Path)
    parser.add_argument("--d-min", type=float, default=1.0)
    parser.add_argument("--overload", type=int, default=130000)
    parser.add_argument("--axis", type=float, nargs=3)
    parser.add_argument("--centre", type=float, nargs=2)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = Path(sys.executable).parent
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = os.pathsep.join(str(root / p) for p in ("", "Library/bin", "Scripts", "bin")) + os.pathsep + env["PATH"]
    scripts = Path(__file__).resolve().parent
    module = scripts.parent
    command = [sys.executable, str(module / "prepare_cred2.py"), str(args.dataset.resolve()),
               str(args.output.resolve()), "--d-min", str(args.d_min), "--overload", str(args.overload)]
    if args.xparm:
        command += ["--xparm", str(args.xparm.resolve())]
    for name in ("axis", "centre"):
        values = getattr(args, name)
        if values:
            command += ["--" + name, *map(str, values)]
    subprocess.run(command, env=env, check=True)
    if not args.prepare_only:
        subprocess.run([sys.executable, str(scripts / "run_pipeline.py"), str(args.output.resolve())], env=env, check=True)
        subprocess.run([sys.executable, str(scripts / "validate_result.py"), str(args.output.resolve())], env=env, check=True)


if __name__ == "__main__":
    main()
