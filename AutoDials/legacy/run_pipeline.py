"""Run the prepared DIALS reduction stages with logged, checked subprocesses."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--start", default="import", choices=["import", "find_spots", "index", "refine", "integrate", "export", "report"])
    parser.add_argument("--stop", default="report", choices=["import", "find_spots", "index", "refine", "integrate", "export", "report"])
    args = parser.parse_args()
    directory = args.directory.resolve()
    root = Path(sys.executable).parent
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = os.pathsep.join(str(root / p) for p in ("", "Library/bin", "Scripts", "bin")) + os.pathsep + env["PATH"]
    launcher = Path(__file__).resolve().parent.parent / "dials_compat.py"
    stages = [
        ("import", ["import.phil"], "imported.expt"),
        ("find_spots", ["imported.expt", "spots.phil"], "strong.refl"),
        ("index", ["imported.expt", "strong.refl", "index.phil"], "indexed.expt"),
        ("refine", ["indexed.expt", "indexed.refl", "detector.fix=distance", "goniometer.fix=None", "refinement.reflections.outlier.nproc=1"], "refined.expt"),
        ("integrate", ["refined.expt", "refined.refl", "integrate.phil"], "integrated.refl"),
        ("export", ["integrated.expt", "integrated.refl", "format=mtz", "mtz.hklout=integrated.mtz"], "integrated.mtz"),
        ("report", ["integrated.expt", "integrated.refl", "output.html=report.html"], "report.html"),
    ]
    names = [stage[0] for stage in stages]
    first, last = names.index(args.start), names.index(args.stop)
    if first > last:
        parser.error("--start must not follow --stop")
    selected = stages[first:last + 1]
    for _, _, artifact in selected:
        if (directory / artifact).exists():
            parser.error(f"Refusing to overwrite {artifact}; select --start or use a new output directory")
    history = []
    for name, params, artifact in selected:
        command = [sys.executable, str(launcher), name, *params]
        print(f"Running dials.{name}", flush=True)
        with (directory / f"{name}_console.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT)
        record = {"command": command, "returncode": result.returncode, "utc": datetime.now(timezone.utc).isoformat()}
        history.append(record)
        with (directory / "pipeline_history.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")
        if result.returncode or not (directory / artifact).is_file():
            raise RuntimeError(f"dials.{name} failed; see {directory / (name + '_console.log')}")
    print("Requested stages completed", flush=True)


if __name__ == "__main__":
    main()
