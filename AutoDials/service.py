"""AutoCrys-side dataset discovery, saved settings and checked subprocess bridge."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import uuid
from datetime import datetime
from .ui_support import create_job, native_path, default_python, validate_inputs, decode_event


def discover(root):
    root = Path(root).resolve()
    if list(root.glob("*cRED2*parameters*.txt")):
        return [root]
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in {"diff", "AutoDials", "DIALS", "AutoR3D", ".git", "__pycache__"})
        if any("cRED2" in f and "parameters" in f and f.endswith(".txt") for f in files):
            path = Path(base)
            if (path / "diff").is_dir():
                found.append(path)
                dirs[:] = []
    return sorted(found)


def load_settings(dataset):
    path = Path(dataset) / "AutoDials" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_settings(datasets, settings):
    for dataset in datasets:
        path = Path(dataset) / "AutoDials" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def missing_artifacts(run):
    """Artifacts a completed single-dataset run must still have on disk."""
    run = Path(run)
    required = [run / "work/integrated.expt", run / "work/integrated.refl",
                run / "work/manifest.json", run / "reference.json", run / "geometry_check.npz"]
    try:
        data = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return ["summary.json"]
    experiment = data.get("experiment")
    if experiment:
        required.extend(run / f"{experiment}{suffix}" for suffix in (".mtz", ".hkl", ".ins"))
    missing = [p.name for p in required if not p.is_file()]
    if any(p.is_file() and p.stat().st_size == 0 for p in required):
        missing.append("empty artifact")
    if not experiment:
        missing.append("experiment name")
    return missing


def run_info(run):
    """Stable description of one run for display, selection and reproducibility."""
    run = Path(run).resolve()
    try:
        data = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    missing = missing_artifacts(run)
    return {"run": run, "id": run.name, "directory": str(run), "status": data.get("status"),
            "experiment": data.get("experiment"), "cell": data.get("cell"),
            "space_group": data.get("space_group"), "space_group_number": data.get("space_group_number"),
            "intensity_status": data.get("intensity_status"), "dials_version": data.get("dials_version"),
            "platform": data.get("platform"), "complete": data.get("status") == "success" and not missing,
            "missing": missing}


def successful_runs(dataset, require_complete=True):
    """Runs that finished successfully and still have their full outputs.

    ``require_complete=False`` keeps the older, lighter contract (status plus
    integrated.expt) for callers that only need to locate a run directory before
    re-exporting or inspecting it.
    """
    runs = []
    for folder in ("AutoDials", "DIALS"):
        for p in (Path(dataset) / folder).glob("run_*/summary.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if data.get("status") != "success" or not (p.parent / "work/integrated.expt").exists():
                continue
            if require_complete and missing_artifacts(p.parent):
                continue
            runs.append(p.parent)
    return sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)


def run_choices(dataset):
    return [info for info in (run_info(r) for r in successful_runs(dataset)) if info]


def latest_run(dataset):
    runs = successful_runs(dataset)
    return runs[0] if runs else None


def read_result(run):
    run = Path(run)
    data = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    data["run_directory"] = str(run)
    if data.get("experiment"):
        for key, suffix in (("mtz", ".mtz"), ("hkl", ".hkl"), ("ins", ".ins")):
            data[key] = str(run / (data["experiment"] + suffix))
    return data


def execute(command, run, callback, cancelled):
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1, **flags)
    import threading
    done = threading.Event()
    def monitor():
        while not done.wait(.15):
            if cancelled.is_set():
                (run / "cancel.request").write_text("cancel", encoding="utf-8")
                return
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        with (run / "run.log").open("w", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                callback(decode_event(line) or {"kind": "log", "text": line})
        code = process.wait()
        if code:
            raise RuntimeError(f"DIALS returned {code}; see {run / 'run.log'}")
        return read_result(run) if (run / "summary.json").exists() else None
    finally:
        done.set()
        watcher.join(timeout=1)
        process.stdout.close()


def process_dataset(dataset, settings, python, callback, cancelled):
    inputs = validate_inputs(str(dataset), python, settings.get("cell", ""), settings.get("space_group", "P1"),
                             settings.get("d_min", "1.0"), settings.get("use_xds", False), settings.get("xparm", ""))
    run, command = create_job(inputs)
    callback({"kind": "run", "dataset": str(dataset), "run_directory": str(run)})
    result = execute(command, run, callback, cancelled)
    if result["status"] != "success":
        raise RuntimeError(result.get("error", "DIALS failed"))
    return result


def multi_job(root, runs, python, action, options=None):
    """Build an HCA/merge job from explicit, user-selected run directories."""
    root, python = Path(root), Path(python)
    run_dirs = [Path(r).resolve() for r in runs]
    if len(run_dirs) < 2:
        raise ValueError("Select at least two successful runs")
    for run in run_dirs:
        if not (run / "summary.json").is_file() or missing_artifacts(run):
            raise ValueError(f"Selected run is incomplete: {run}")
    folder = root / "AutoDials" / (action + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    labels = []
    for run in run_dirs:
        dataset = run.parent.parent
        labels.append(str(dataset.relative_to(root)) if dataset.is_relative_to(root) else str(dataset))
    request = dict(action=action, run_directory=native_path(folder, python),
                   inputs=[native_path(p, python) for p in run_dirs],
                   run_ids=[p.name for p in run_dirs], labels=labels,
                   **(options or {}))
    config = folder / "request.json"
    config.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [str(python), "-u", native_path(Path(__file__).with_name("worker.py"), python), native_path(config, python)]
    return folder, command
