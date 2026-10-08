"""Focused checks for scientific inputs, file naming, cancellation and the UI bridge."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from AutoDials import ui_support as support
from AutoDials import ui_worker


class InputsTest(unittest.TestCase):
    def test_cell_accepts_separators_and_rejects_nonphysical_geometry(self):
        self.assertEqual(support.parse_cell("10, 20，30;90 90 90"), [10, 20, 30, 90, 90, 90])
        self.assertIsNone(support.parse_cell(" "))
        for text in ("1 2 3", "nan 2 3 90 90 90", "-1 2 3 90 90 90", "1 2 3 10 10 170"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                support.parse_cell(text)

    def test_optional_xds_and_named_unique_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "实验 folder"
            (dataset / "diff").mkdir(parents=True)
            (dataset / "Continuous 3D ED (cRED2) parameters.txt").write_text("test")
            (dataset / "diff" / "frame_0001.tif").touch()
            inputs = support.validate_inputs(str(dataset), sys.executable)
            self.assertIsNone(inputs["xparm"])
            with self.assertRaises(ValueError):
                support.validate_inputs(str(dataset), sys.executable, use_xds=True)
            (dataset / "diff" / "p").mkdir()
            xparm = dataset / "diff" / "p" / "GXPARM.XDS"
            xparm.touch()
            inputs = support.validate_inputs(str(dataset), sys.executable, use_xds=True)
            self.assertEqual(inputs["xparm"], xparm)
            run1, command = support.create_job(inputs)
            run2, _ = support.create_job(inputs)
            self.assertNotEqual(run1, run2)
            config = json.loads((run1 / "request.json").read_text(encoding="utf-8"))
            self.assertEqual(config["name"], "实验 folder")
            self.assertEqual(len(command), 4)  # list arguments preserve spaces
            with self.assertRaises(ValueError):
                support.validate_inputs(str(dataset), sys.executable, space_group="231")

    def test_merge_discovers_complete_runs_and_records_exact_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for dataset_name, run_name in (("sample_a", "run_a"), ("sample_b", "run_b")):
                run = root / dataset_name / "DIALS" / run_name
                (run / "work").mkdir(parents=True)
                (run / "work" / "integrated.expt").write_text("{}")
                (run / "work" / "integrated.refl").write_text("data")
                (run / "summary.json").write_text(json.dumps({"status": "success", "cell": [10, 11, 12, 90, 90, 90], "space_group": "P1"}))
                runs.append(run)
            incomplete = root / "sample_c" / "DIALS" / "run_c"
            incomplete.mkdir(parents=True)
            (incomplete / "summary.json").write_text(json.dumps({"status": "success"}))
            found = support.discover_merge_runs(root)
            self.assertEqual({item["run"] for item in found}, set(runs))
            output, command = support.create_merge_job(root, runs, sys.executable, "merged_test")
            request = json.loads((output / "request.json").read_text())
            self.assertEqual(request["action"], "merge")
            self.assertEqual(request["run_ids"], ["run_a", "run_b"])
            self.assertEqual(request["labels"], ["sample_a", "sample_b"])
            self.assertIn("AutoDials", command[2])
            with self.assertRaises(ValueError):
                support.create_merge_job(root, [runs[0]], sys.executable)

    def test_wsl_converts_each_path_for_windows_python(self):
        if os.name == "nt":
            self.skipTest("WSL bridge test")
        with patch("AutoDials.ui_support.subprocess.run") as call:
            call.return_value.stdout = "C:\\some folder\\file.py\n"
            converted = support.native_path(Path("/mnt/c/some folder/file.py"), Path("/mnt/c/dials/python.exe"))
            self.assertEqual(converted, "C:\\some folder\\file.py")
            self.assertEqual(call.call_args.args[0][0:2], ["wslpath", "-w"])

    def test_cancellation_terminates_active_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "sleep.py"
            script.write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n")
            cancel = root / "cancel.request"
            timer = threading.Timer(0.5, lambda: cancel.write_text("stop"))
            timer.start()
            try:
                with self.assertRaises(ui_worker.Cancelled):
                    ui_worker.run_child([sys.executable, str(script)], root, root / "log.txt", cancel)
            finally:
                timer.join()

    @unittest.skipUnless(os.name == "nt", "Run with Windows DIALS Python")
    def test_symmetry_uses_number_and_rejects_incompatible_cell(self):
        ui_worker.configure_runtime()
        cell, sg = ui_worker.canonical_symmetry([19.7, 19.88, 13.17, 90, 90, 90], "62")
        self.assertEqual(sg.replace(" ", ""), "Pnma")
        with self.assertRaises(Exception):
            ui_worker.canonical_symmetry(cell, "NotASpaceGroup")
        with self.assertRaises(ValueError):
            ui_worker.canonical_symmetry([10, 20, 30, 90, 90, 90], "195")


if __name__ == "__main__":
    unittest.main()
