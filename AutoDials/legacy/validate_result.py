"""Summarize real DIALS outputs and check electron/scan invariants."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dials_compat import apply
apply()
import numpy as np
from dxtbx.model.experiment_list import ExperimentListFactory
from dials.array_family import flex
from iotbx import mtz


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    report = {"geometry_source": manifest["geometry_source"], "frames": manifest["frames"],
              "zero_frame_count": len(manifest["zero_frames"]),
              "all_frames_lossless_verified": manifest["all_frames_lossless_verified"]}
    for stage in ("imported", "indexed", "refined", "integrated"):
        path = directory / f"{stage}.expt"
        if not path.exists():
            continue
        exp = ExperimentListFactory.from_json_file(str(path), check_format=False)[0]
        assert str(exp.beam.get_probe()).lower().endswith("electron"), exp.beam.get_probe()
        scan = exp.scan
        record = {"probe": str(exp.beam.get_probe()), "image_range": scan.get_image_range(),
                  "oscillation": scan.get_oscillation(), "distance_mm": exp.detector[0].get_distance()}
        assert abs(scan.get_oscillation()[1] - manifest["step_deg"]) < 1e-8
        assert abs(scan.get_oscillation()[0] - manifest["start_deg"]) < 1e-8
        if exp.crystal:
            record["cell"] = exp.crystal.get_unit_cell().parameters()
            record["space_group"] = str(exp.crystal.get_space_group().info())
        report[stage] = record
    strong = flex.reflection_table.from_file(str(directory / "strong.refl"))
    report["strong_spots"] = len(strong)
    z = np.asarray(strong["xyzobs.px.value"].parts()[2])
    assert not set(np.floor(z).astype(int) + 1).intersection(manifest["zero_frames"])
    report["no_strong_centroids_on_zero_frames"] = True
    indexed = flex.reflection_table.from_file(str(directory / "indexed.refl"))
    report["indexed_spots"] = int(indexed.get_flags(indexed.flags.indexed).count(True))
    report["indexed_fraction"] = report["indexed_spots"] / len(strong)
    if (directory / "integrated.refl").exists():
        refl = flex.reflection_table.from_file(str(directory / "integrated.refl"))
        report["saved_integrated_reflections"] = len(refl)
        for flag in ("integrated_sum", "integrated_prf"):
            report[flag] = int(refl.get_flags(getattr(refl.flags, flag)).count(True))
    if (directory / "integrated.mtz").exists():
        data = mtz.object(str(directory / "integrated.mtz"))
        report["mtz_reflections"] = data.n_reflections()
        report["mtz_space_group"] = str(data.space_group_info())
    (directory / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
