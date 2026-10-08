"""Isolated tests for the AutoCrys-side AutoDials service layer.

These do not need a DIALS installation, numpy or real data.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest

from AutoDials import service
from AutoDials import ui_support


def make_dataset(root, name="dataset_1"):
    dataset = Path(root) / name
    (dataset / "diff").mkdir(parents=True)
    (dataset / "dataset_1cRED2parameters.txt").write_text("Number of frames: 2\n", encoding="utf-8")
    (dataset / "diff" / "frame_0001.tif").write_bytes(b"x")
    return dataset


def make_run(dataset, name="run_20260101_000000_aaaaaa", *, status="success", complete=True, experiment="dataset_1"):
    run = Path(dataset) / "AutoDials" / name
    (run / "work").mkdir(parents=True)
    for artifact in ("integrated.expt", "integrated.refl", "manifest.json"):
        (run / "work" / artifact).write_text("{}", encoding="utf-8")
    (run / "reference.json").write_text("{}", encoding="utf-8")
    (run / "geometry_check.npz").write_bytes(b"npz")
    summary = {"status": status, "experiment": experiment, "cell": [10, 11, 12, 90, 90, 90],
               "space_group": "P1", "space_group_number": 1, "intensity_status": "unscaled"}
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if complete and status == "success":
        for suffix in (".mtz", ".hkl", ".ins"):
            (run / f"{experiment}{suffix}").write_text("data", encoding="utf-8")
    return run


class ServiceTests(unittest.TestCase):
    def test_discover_skips_output_and_finds_nested(self):
        with tempfile.TemporaryDirectory() as folder:
            top = make_dataset(folder, "top")
            nested = Path(folder) / "batch" / "inner"
            nested.mkdir(parents=True)
            make_dataset(str(nested), "inner")
            (Path(folder) / "batch" / "AutoDials").mkdir()
            found = [p.name for p in service.discover(folder)]
            self.assertIn("top", found)
            self.assertIn("inner", found)
            self.assertEqual(found.count("inner"), 1)

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset = make_dataset(folder)
            service.save_settings([dataset], {"cell": "1 2 3 90 90 90", "space_group": "P1"})
            self.assertEqual(service.load_settings(dataset)["space_group"], "P1")

    def test_completeness_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset = make_dataset(folder)
            good = make_run(dataset, "run_good_1")
            bad = make_run(dataset, "run_bad_1", complete=False)
            self.assertEqual(service.missing_artifacts(good), [])
            self.assertTrue(service.missing_artifacts(bad))
            self.assertEqual([r.name for r in service.successful_runs(dataset)], ["run_good_1"])
            self.assertEqual({r.name for r in service.successful_runs(dataset, require_complete=False)},
                             {"run_good_1", "run_bad_1"})
            info = service.run_choices(dataset)
            self.assertEqual(len(info), 1)
            self.assertTrue(info[0]["complete"])

    def test_failed_run_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset = make_dataset(folder)
            make_run(dataset, "run_failed_1", status="failed", complete=False)
            self.assertEqual(service.successful_runs(dataset), [])

    def test_multi_job_uses_explicit_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = make_dataset(folder, "a"), make_dataset(folder, "b")
            run_a = make_run(first, experiment="a")
            run_b = make_run(second, experiment="b")
            # An older, different run must not be silently chosen.
            make_run(first, "run_older_1", experiment="a")
            out, command = service.multi_job(folder, [run_a, run_b], "/usr/bin/python3", "hca",
                                             {"method": "unit-cell"})
            request = json.loads((out / "request.json").read_text(encoding="utf-8"))
            self.assertEqual(request["run_ids"], [run_a.name, run_b.name])
            self.assertNotIn("run_older_1", " ".join(request["inputs"]))
            self.assertEqual(request["action"], "hca")
            self.assertIn("worker.py", " ".join(command))

    def test_multi_job_rejects_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = make_dataset(folder, "a"), make_dataset(folder, "b")
            run_a = make_run(first, experiment="a")
            run_b = make_run(second, experiment="b", complete=False)
            with self.assertRaises(ValueError):
                service.multi_job(folder, [run_a, run_b], "/usr/bin/python3", "merge")
            with self.assertRaises(ValueError):
                service.multi_job(folder, [run_a], "/usr/bin/python3", "merge")


class SupportTests(unittest.TestCase):
    def test_decode_event(self):
        self.assertIsNone(ui_support.decode_event("plain log line\n"))
        payload = ui_support.decode_event(ui_support.EVENT_PREFIX + '{"kind": "result", "x": 1}')
        self.assertEqual(payload["x"], 1)

    def test_parse_cell_validation(self):
        self.assertEqual(ui_support.parse_cell("10 11 12 90 90 90"), [10, 11, 12, 90, 90, 90])
        self.assertIsNone(ui_support.parse_cell("  "))
        for bad in ("1 2 3", "a b c d e f", "10 11 12 0 90 90", "10 11 12 90 90 180"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ui_support.parse_cell(bad)

    def test_probe_rejects_missing_python(self):
        with self.assertRaises(ValueError):
            ui_support.probe_dials("/nonexistent/python")

    def test_default_python_honours_env(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "python"
            fake.write_text("", encoding="utf-8")
            old = os.environ.get("DIALS_PYTHON")
            os.environ["DIALS_PYTHON"] = str(fake)
            try:
                self.assertEqual(ui_support.default_python(), str(fake))
            finally:
                if old is None:
                    os.environ.pop("DIALS_PYTHON", None)
                else:
                    os.environ["DIALS_PYTHON"] = old

    def test_create_job_request_is_actionable(self):
        with tempfile.TemporaryDirectory() as folder:
            dataset = make_dataset(folder)
            inputs = ui_support.validate_inputs(str(dataset), "/usr/bin/python3", "10 11 12 90 90 90", "P1", "1.0", False, "")
            run, command = ui_support.create_job(inputs)
            request = json.loads((run / "request.json").read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "process")
            self.assertEqual(request["name"], "dataset_1")
            self.assertTrue(request["dataset"].endswith("dataset_1"))
            self.assertEqual(len(command), 4)


if __name__ == "__main__":
    unittest.main()
