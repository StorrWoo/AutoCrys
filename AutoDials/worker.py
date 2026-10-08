"""Native DIALS dispatcher for the AutoCrys AutoDials tab."""
import json
from pathlib import Path
import sys
import traceback
try:
    from .ui_worker import configure_runtime, process_job, event, Cancelled, runtime_metadata
except ImportError:
    from ui_worker import configure_runtime, process_job, event, Cancelled, runtime_metadata


def main():
    configure_runtime()
    from dials_compat import apply
    apply()
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root = Path(config["run_directory"])
    report = {}
    try:
        action = config.get("action", "process")
        if action in ("process", "reference"):
            if action == "process":
                report = process_job(config)
            else:
                from ui_worker import summarize
                request = json.loads((root / "request.json").read_text(encoding="utf-8"))
                name = request["name"]
                report = summarize(root / "work", root / f"{name}.mtz", root / f"{name}.hkl", root / f"{name}.ins")
                report.update(status="success", experiment=name, requested_cell=request.get("unit_cell"),
                              requested_space_group=request.get("space_group"), xds_geometry=bool(request.get("xparm")),
                              **runtime_metadata())
            from bridge import export_reference
            report["reference"] = export_reference(root)
            report["source"] = "AutoDials"
            from dxtbx.model.experiment_list import ExperimentListFactory
            e = ExperimentListFactory.from_json_file(str(root / "work/integrated.expt"), check_format=False)[0]
            report["space_group_number"] = e.crystal.get_space_group().type().number()
            (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            event("result", **report)
        else:
            from multi import run_multi
            run_multi(config)
        return 0
    except Exception as error:
        report.update(status="cancelled" if isinstance(error, Cancelled) else "failed", error=str(error))
        (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        event("error", **report)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
