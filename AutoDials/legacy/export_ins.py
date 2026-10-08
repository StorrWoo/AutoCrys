"""Add a named INS to an existing standalone-UI result without rerunning integration."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ui_worker import configure_runtime, validate_ins


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    configure_runtime()
    from dials_compat import apply
    apply()
    from dxtbx.model.experiment_list import ExperimentListFactory
    from dials.util.export_shelx import _write_ins
    run = args.run_directory.resolve()
    request = json.loads((run / "request.json").read_text(encoding="utf-8"))
    name = request["name"]
    if Path(name).name != name or name in ("", ".", ".."):
        parser.error("Invalid experiment name")
    target = run / f"{name}.ins"
    if target.exists():
        parser.error(f"Refusing to overwrite {target}")
    work = run / "work"
    experiments = ExperimentListFactory.from_json_file(str(work / "integrated.expt"), check_format=False)
    _write_ins(experiments, best_unit_cell=None, composition="CH", ins_file=str(target))
    validation = validate_ins(work, target)
    summary_path = run / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary.update(ins=str(target), **validation)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(ins=str(target), **validation), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
