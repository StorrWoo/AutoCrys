from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "AutoRefine" / "scripts" / "autorefine.py"
SPEC = importlib.util.spec_from_file_location("autorefine_skill", SCRIPT)
assert SPEC and SPEC.loader
AUTOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUTOR
SPEC.loader.exec_module(AUTOR)


class AutoRefineSkillTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "Data" / "sample3_1").is_dir(), "requires optional private model and user-installed runtime")
    def test_spd3_1_a_alias_and_real_model_geometry(self):
        located = AUTOR.locate_target("sample3_1_a", ROOT / "Data")
        source_res = Path(located["res"])
        source_hkl = source_res.with_suffix(".hkl")
        self.assertEqual(source_res.name, "sample3_1_a.res")
        self.assertTrue(source_res.is_relative_to(ROOT / "Data"))
        result = AUTOR.inspect_files(
            source_res,
            source_hkl,
            located,
            AUTOR.PROFILE_SIO2,
        )
        self.assertEqual(result["status"], "inspected")
        self.assertEqual(result["atom_counts"], {"O": 25, "Si": 12})
        self.assertEqual(result["model_unit_cell_counts"], {"O": 184, "Si": 96})
        self.assertEqual(result["classification"], "continue_cycles")
        geometry = result["zeolite_sio2_analysis"]
        self.assertEqual(geometry["si_o_distance_summary"]["count"], 44)
        si_sites = [site for site in geometry["sites"] if site["assigned"] == "Si"]
        self.assertTrue(all(site["opposite_type_coordination"] in {3, 4} for site in si_sites))
        self.assertGreaterEqual(sum(site["opposite_type_coordination"] == 4 for site in si_sites), 8)

    @unittest.skipUnless((ROOT / "Data" / "sample3_1").is_dir() and (ROOT / "AutoSolve" / "tools" / "shelxl").is_file(), "requires optional private model and user-installed runtime")
    def test_real_shelxl_stage_is_isolated_and_metrics_are_parsed(self):
        located = AUTOR.locate_target("sample3_1_a", ROOT / "Data")
        source_res = Path(located["res"])
        source_hkl = source_res.with_suffix(".hkl")
        before = (AUTOR.sha256(source_res), AUTOR.sha256(source_hkl))
        with tempfile.TemporaryDirectory(prefix="autorefine-real-") as temp:
            outdir = Path(temp) / "stage"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "refine",
                    "--res", str(source_res),
                    "--hkl", str(source_hkl),
                    "--profile", "zeolite-sio2",
                    "--cycles", "2",
                    "--peaks", "10",
                    "--threads", "2",
                    "--outdir", str(outdir),
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            result = json.loads(completed.stdout)
            self.assertTrue(result["source_unchanged"])
            self.assertEqual(result["status"], "refined")
            metrics = result["inspection"]["metrics"]
            self.assertGreater(metrics["r1_gt"], 0)
            self.assertGreater(metrics["r1_all"], metrics["r1_gt"])
            self.assertNotEqual(metrics["r1_all"], metrics["gof"])
            self.assertEqual(metrics["npd_atoms"], 0)
            self.assertEqual(len(metrics["highest_peak_fractional"]), 3)
            self.assertEqual(len(metrics["deepest_hole_fractional"]), 3)
            self.assertTrue((outdir / "sample3_1_a.lst").is_file())
        self.assertEqual(before, (AUTOR.sha256(source_res), AUTOR.sha256(source_hkl)))

    def test_negative_adp_halts(self):
        with tempfile.TemporaryDirectory(prefix="autorefine-negative-") as temp:
            root = Path(temp)
            res = root / "bad.res"
            hkl = root / "bad.hkl"
            res.write_text(
                "\n".join(
                    [
                        "TITL negative U test in P1",
                        "CELL 0.71073 10 10 10 90 90 90",
                        "LATT -1",
                        "SFAC Si O",
                        "UNIT 1 2",
                        "FVAR 1",
                        "SI1 1 0.5 0.5 0.5 11.0 -0.01",
                        "O1 2 0.66 0.5 0.5 11.0 0.02",
                        "O2 2 0.34 0.5 0.5 11.0 0.02",
                        "HKLF 4",
                        "END",
                    ]
                ),
                encoding="utf-8",
            )
            hkl.write_text("0 0 0 0 0\n", encoding="utf-8")
            result = AUTOR.inspect_files(res, hkl, {"query": str(res)}, "generic")
            self.assertEqual(result["classification"], "halt_invalid")
            self.assertIn("negative_adp", {item["code"] for item in result["adp_analysis"]["warnings"]})

    def test_missing_hkl_is_a_clean_input_failure(self):
        with tempfile.TemporaryDirectory(prefix="autorefine-missing-") as temp:
            res = Path(temp) / "only.res"
            res.write_text("TITL missing hkl\nCELL 0.7 10 10 10 90 90 90\n", encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect", "--res", str(res), "--json"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(failed.returncode, 2)
            payload = json.loads(failed.stdout)
            self.assertEqual(payload["status"], "input_or_analysis_failed")
            self.assertIn("missing_hkl", payload["reason"])


if __name__ == "__main__":
    unittest.main()
