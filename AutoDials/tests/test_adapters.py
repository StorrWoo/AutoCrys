import argparse
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np

from AutoDials import service
from AutoDials.results import reflection_metadata
from AutoR3D.cred2 import find_cred2_file, parse_cred2_parameters
from AutoR3D.geometry import build_geometry, pixel_to_q_sample
from AutoR3D.reference import load_reference
from AutoSolve.scripts.auto_shelxt import cell_from_sources, sg_from_sources, prepare

ROOT = Path(__file__).resolve().parents[2]


class AdapterTests(unittest.TestCase):
    def test_all_dials_code_lives_in_top_level_module(self):
        for name in ("mini_ui.py", "ui_support.py", "ui_worker.py", "worker.py", "multi.py",
                     "prepare_cred2.py", "dials_compat.py", "start_ui.bat", "start_ui.sh"):
            with self.subTest(file=name):
                self.assertTrue((ROOT / "AutoDials" / name).is_file())
        self.assertFalse((ROOT / "Data/DIALS").exists())

    def test_native_geometry_matches_dials_centroids(self):
        dataset = ROOT / "Data/sample3_1"
        runs = [r for r in service.successful_runs(dataset) if (r / "geometry_check.npz").exists()]
        if not runs:
            self.skipTest("Run exercise_pipeline.py first")
        ref = load_reference(dataset, "dials", runs[0])
        params = parse_cred2_parameters(find_cred2_file(dataset))
        geometry = build_geometry(params, (516, 516), ref.rotation_axis_deg, *ref.beam_center_px,
                                  detector_distance_mm=ref.detector_distance_mm, wavelength_angstrom=ref.wavelength_angstrom)
        geometry = ref.apply_geometry(geometry)
        data = np.load(runs[0] / "geometry_check.npz")
        q = pixel_to_q_sample(data["x"], data["y"], data["angle_deg"], geometry)
        error = float(np.max(np.abs(q - data["q"])))
        print(f"Native DIALS reciprocal mapping max error: {error:.3g} A^-1 ({len(q)} centroids)")
        np.testing.assert_allclose(q, data["q"], atol=2e-12, rtol=1e-10)
        np.testing.assert_allclose(np.asarray(ref.cell_axes) @ np.asarray(ref.reciprocal_basis), np.eye(3), atol=1e-12)
        self.assertEqual(ref.source, "DIALS")
        self.assertEqual(ref.model["acquired_frames"], 421)
        self.assertEqual(len(ref.model["zero_frames"]), 42)
        # The wrong dataset cannot borrow this crystal's orientation.
        with self.assertRaisesRegex(ValueError, "belong"):
            load_reference(ROOT / "Data/sample2_1", "dials", runs[0])
        self.assertEqual(type(load_reference(dataset, "auto")).__name__, "XdsReference")

    def test_autosolve_uses_matching_result_before_stale_xds(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "specimen.hkl").write_text("   1   0   0   100.0    10.0\n   0   0   0     0.0     0.0\n")
            cell = [10., 11., 12., 90., 100., 90.]
            (work / "summary.json").write_text(json.dumps(dict(status="success", experiment="specimen", cell=cell, space_group_number=14)))
            (work / "summary.txt").write_text("Dataset\tCell\tSG\nspecimen\t1 2 3 90 90 90\t1\n")
            # Distinct SYMM validates retention of the actual exported setting.
            (work / "specimen.ins").write_text("TITL 14\nCELL 0.0251 10 11 12 90 100 90\nLATT 1\nSYMM -X,Y+1/2,-Z+1/2\n")
            args = argparse.Namespace(cell=None, sg=None, no_force_sg=False, hkl=str(work / "specimen.hkl"))
            self.assertEqual(cell_from_sources(args, ROOT, work, "specimen", "specimen"), (cell, "DIALS summary.json"))
            sg, source = sg_from_sources(args, ROOT, work, "specimen", "specimen")
            self.assertEqual(source, "DIALS summary.json")
            args.cell = "20 21 22 90 90 90"
            self.assertEqual(cell_from_sources(args, ROOT, work, "specimen", "specimen")[1], "user")
            self.assertIsNone(reflection_metadata(work / "unrelated.hkl"))
            args = argparse.Namespace(root=str(ROOT), hkl=str(work / "specimen.hkl"), workdir=str(work), dataset=None,
                basename=None, cell=None, sg=None, no_force_sg=False, composition="C2 H4", formula=None, z=None, dry_run=False)
            result = prepare(args)
            text = (work / "specimen.ins").read_text()
            self.assertIn("CELL 0.0251", text)
            self.assertIn("SYMM -X,Y+1/2,-Z+1/2", text)
            cell_line = next(line.split() for line in text.splitlines() if line.startswith("CELL "))
            self.assertEqual(list(map(float, cell_line[2:])), cell)
            self.assertEqual(result["cell_source"], "DIALS summary.json")


if __name__ == "__main__":
    unittest.main()
