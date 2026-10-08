from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "AutoSolve" / "scripts" / "auto_shelxl.py"
SOURCE_RES = ROOT / "Demo" / "AutoSolve" / "1.res"
SOURCE_HKL = ROOT / "Demo" / "AutoSolve" / "1.hkl"
sys.path.insert(0, str(ROOT))

from UI.ai_assistant.artifacts import ArtifactAnalyzer


class ShelxlRefinementTests(unittest.TestCase):
    @unittest.skipUnless(SOURCE_RES.is_file() and SOURCE_HKL.is_file() and (ROOT / "AutoSolve" / "tools" / "shelxl").is_file(), "requires optional real data and user-installed SHELXL")
    def test_real_initial_model_refines_in_an_isolated_directory(self):
        source_res_before = SOURCE_RES.read_bytes()
        source_hkl_before = SOURCE_HKL.read_bytes()
        with tempfile.TemporaryDirectory(prefix="autocrys-shelxl-") as temp:
            temp_path = Path(temp)
            local_res = temp_path / "initial.res"
            local_hkl = temp_path / "initial.hkl"
            shutil.copy2(SOURCE_RES, local_res)
            shutil.copy2(SOURCE_HKL, local_hkl)
            outdir = temp_path / "shelxl_refinement" / "initial_trial"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--res", str(local_res),
                    "--hkl", str(local_hkl),
                    "--outdir", str(outdir),
                    "--cycles", "3",
                    "--peaks", "12",
                    "--threads", "2",
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "refined")
            self.assertEqual(result["quality"], "needs_model_work")
            self.assertGreater(result["r1_gt"], 0)
            self.assertIn("unit_atom_count_mismatch", result["warnings"])
            for suffix in (".ins", ".hkl", ".res", ".lst", ".cif", ".fcf"):
                self.assertTrue((outdir / f"initial{suffix}").is_file(), suffix)
            ins = (outdir / "initial.ins").read_text(encoding="utf-8")
            self.assertIn("L.S. 3", ins)
            self.assertIn("PLAN 12", ins)
            self.assertIn("ACTA", ins)
            self.assertNotIn("Q1", ins)

            report = ArtifactAnalyzer(ROOT).inspect_solve(
                local_res,
                "initial",
                {"workdir": temp_path, "basename": "initial", "res": local_res},
            )
            self.assertEqual(report.status, "shelxl_refined")
            self.assertIn("shelxl_unit_atom_mismatch", {item.code for item in report.findings})
            self.assertIn("shelxl_not_converged", {item.code for item in report.findings})

        self.assertEqual(SOURCE_RES.read_bytes(), source_res_before)
        self.assertEqual(SOURCE_HKL.read_bytes(), source_hkl_before)

    def test_dry_run_does_not_create_an_output_directory(self):
        with tempfile.TemporaryDirectory(prefix="autocrys-shelxl-dry-") as temp:
            sample = Path(temp)
            res = sample / "synthetic.res"
            hkl = sample / "synthetic.hkl"
            binary = sample / "shelxl-stub"
            res.write_text("TITL synthetic\nCELL 0.7 10 10 10 90 90 90\nLATT -1\nSFAC C\nUNIT 1\nC1 1 .1 .1 .1 11 .04\nHKLF 4\nEND\n")
            hkl.write_text("0 0 0 0 0\n")
            binary.write_text("#!/bin/sh\nexit 99\n")
            binary.chmod(0o755)
            outdir = sample / "will-not-exist"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--res", str(res),
                    "--hkl", str(hkl),
                    "--shelxl-bin", str(binary),
                    "--outdir", str(outdir),
                    "--dry-run",
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            self.assertEqual(json.loads(completed.stdout)["status"], "dry_run")
            self.assertFalse(outdir.exists())
