"""Native DIALS reindexing/scaling plus HCA for independent datasets."""
import json
from pathlib import Path
import sys


def run_multi(config):
    from ui_worker import run_child, event, Cancelled
    import numpy as np
    from dxtbx.model.experiment_list import ExperimentListFactory
    from dials.array_family import flex
    from cctbx import crystal, miller, sgtbx
    from scipy.cluster.hierarchy import linkage
    from scipy.spatial.distance import pdist, squareform
    root = Path(config["run_directory"])
    inputs = [Path(p) for p in config["inputs"]]
    labels = config["labels"]
    cancel = root / "cancel.request"
    source_expts = [ExperimentListFactory.from_json_file(str(p / "work/integrated.expt"), check_format=False) for p in inputs]
    if any(len(e) != 1 for e in source_expts):
        raise ValueError("HCA/Merge expects individual single-crystal results")
    cells = [list(e[0].crystal.get_unit_cell().parameters()) for e in source_expts]
    sgs = [e[0].crystal.get_space_group() for e in source_expts]
    methods = config.get("method", "both")
    action = config["action"]
    report = {"status": "success", "action": action, "labels": labels, "cells": cells, "methods": {},
              "inputs": list(map(str, inputs)), "run_ids": config.get("run_ids", [p.name for p in inputs]),
              "dials_version": None}
    try:
        from ui_worker import runtime_metadata
        report.update(runtime_metadata())
    except Exception:
        pass
    def cmd(label, module, args):
        if cancel.exists():
            raise Cancelled("Cancelled")
        event("stage", stage=label, current=0, total=1)
        run_child([sys.executable, "-u", str(Path(__file__).with_name("dials_compat.py")), module, *args],
                  root, root / f"{module}.log", cancel)
    if action == "hca" and methods in ("unit-cell", "both"):
        reduced = [e[0].crystal.get_unit_cell().niggli_cell() for e in source_expts]
        vectors = np.array([c.metrical_matrix() for c in reduced])
        scale = np.median(np.linalg.norm(vectors, axis=1))
        tree = linkage(pdist(vectors / scale), method="average")
        report["methods"]["unit-cell"] = {"linkage": tree.tolist(), "metric": "Euclidean Niggli G6 / median G6 norm (average linkage)"}
    needs_alignment = action == "merge" or methods in ("cc1", "both")
    if needs_alignment:
        if len({sg.type().number() for sg in sgs}) != 1:
            raise ValueError("Intensity clustering/merging requires the same space group. Reprocess the selected datasets with consistent symmetry first.")
        paths = []
        for p in inputs:
            paths.extend([str(p / "work/integrated.expt"), str(p / "work/integrated.refl")])
        cmd("Combine experiments", "combine_experiments", [*paths, "output.experiments=combined.expt", "output.reflections=combined.refl"])
        cmd("Resolve indexing ambiguity", "cosym", ["combined.expt", "combined.refl", f"space_group={str(sgs[0].info())}",
                "nproc=1", "exclude_inconsistent_unit_cells=False", "output.experiments=aligned.expt", "output.reflections=aligned.refl"])
        experiments = ExperimentListFactory.from_json_file(str(root / "aligned.expt"), check_format=False)
        reflections = flex.reflection_table.from_file(str(root / "aligned.refl"))
        if len(experiments) != len(inputs):
            raise ValueError("DIALS rejected one or more inputs during reindexing; refusing a silent partial merge")
        # Preserve dataset identity after any DIALS reordering.
        original_ids = [e[0].identifier for e in source_expts]
        current_ids = [e.identifier for e in experiments]
        if set(original_ids) != set(current_ids):
            raise ValueError("Reindexed experiment identifiers no longer match the selected datasets")
        if action == "hca":
            from dials.util.filter_reflections import filter_reflection_table
            arrays = []
            for identifier in original_ids:
                i = current_ids.index(identifier)
                exp = experiments[i]
                table = reflections.select(reflections["id"] == i)
                table = filter_reflection_table(table, intensity_choice=["profile"], partiality_threshold=0.4, combine_partials=True)
                ms = miller.set(crystal.symmetry(unit_cell=exp.crystal.get_unit_cell(), space_group=exp.crystal.get_space_group()),
                                table["miller_index"], anomalous_flag=False)
                arr = miller.array(ms, table["intensity.prf.value"], flex.sqrt(table["intensity.prf.variance"]))
                arrays.append(arr.map_to_asu().merge_equivalents().array())
            n = len(arrays)
            cc = np.eye(n)
            common = np.zeros((n, n), dtype=int)
            for i in range(n):
                for j in range(i):
                    left, right = arrays[i].common_sets(arrays[j], assert_is_similar_symmetry=False)
                    count = left.size()
                    common[i, j] = common[j, i] = count
                    if count < int(config.get("min_common", 20)):
                        raise ValueError(f"Too few common reflections for CC: {labels[i]} / {labels[j]} ({count})")
                    corr = np.corrcoef(np.array(left.data()), np.array(right.data()))[0, 1]
                    if not np.isfinite(corr):
                        raise ValueError("Undefined correlation (constant reflection intensities)")
                    cc[i, j] = cc[j, i] = np.clip(corr, -1, 1)
            distances = np.sqrt(np.maximum(0, 1 - cc))
            np.fill_diagonal(distances, 0)
            tree = linkage(squareform(distances, checks=False), method="average")
            report["methods"]["cc1"] = {"linkage": tree.tolist(), "cc": cc.tolist(), "common_reflections": common.tolist(),
                                           "metric": "sqrt(1 - Pearson CC), common ASU intensities after dials.cosym; average linkage"}
        else:
            name = config.get("name", "merged").strip()
            if Path(name).name != name or not name or any(c in name for c in '\\/:*?"<>|\n\r'):
                raise ValueError("Invalid merge name")
            quoted = lambda value: json.dumps(str(value), ensure_ascii=False)
            cmd("Scale datasets", "scale", ["aligned.expt", "aligned.refl", "nproc=1", "output.json=scaling.json"])
            scaled = ExperimentListFactory.from_json_file(str(root / "scaled.expt"), check_format=False)
            if len(scaled) != len(inputs):
                raise ValueError("Scaling excluded a dataset; inspect the scaling report before merging")
            cmd("Merge MTZ", "merge", ["scaled.expt", "scaled.refl", f"output.mtz={quoted(name + '.mtz')}", "output.json=merging.json"])
            cmd("Export HKL / INS", "export", ["scaled.expt", "scaled.refl", "format=shelx", "intensity=scale",
                        f"shelx.hklout={quoted(name + '.hkl')}", f"shelx.ins={quoted(name + '.ins')}"])
            from iotbx import mtz, builders
            from iotbx.shelx import command_stream, crystal_symmetry_parser, hklf
            builder = builders.crystal_symmetry_builder()
            crystal_symmetry_parser(command_stream(filename=str(root / f"{name}.ins")), builder).parse()
            data = mtz.object(str(root / f"{name}.mtz"))
            with (root / f"{name}.hkl").open() as f:
                n_hkl = hklf.reader(file_object=f).indices().size()
            if data.n_reflections() == 0 or n_hkl == 0:
                raise ValueError("Empty exported reflections")
            report.update(experiment=name, cell=builder.crystal_symmetry.unit_cell().parameters(),
                          space_group=str(builder.crystal_symmetry.space_group_info()),
                          space_group_number=builder.crystal_symmetry.space_group().type().number(),
                          mtz=str(root / f"{name}.mtz"), hkl=str(root / f"{name}.hkl"), ins=str(root / f"{name}.ins"),
                          mtz_reflections=data.n_reflections(), hkl_reflections=n_hkl, intensity_status="scaled",
                          mtz_type="merged", hkl_type="scaled unmerged HKLF4", merging_report=str(root / "dials.merge.html"))
    (root / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    event("result", **report)
    return report
