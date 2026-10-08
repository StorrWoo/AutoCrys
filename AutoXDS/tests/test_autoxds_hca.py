from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AutoXDS" / "scripts"))

import auto_xds


class HCACommonReflectionTests(unittest.TestCase):
    @staticmethod
    def _write_xds(root: Path, name: str, intensities: list[float]) -> None:
        workdir = root / name / "diff" / "p"
        workdir.mkdir(parents=True)
        rows = [
            f"{index + 1:4d} 0 0 {value:.2f} 1.0"
            for index, value in enumerate(intensities)
        ]
        (workdir / "XDS_ASCII.HKL").write_text(
            "!FORMAT=XDS_ASCII\n" + "\n".join(rows) + "\n!END_OF_DATA\n",
            encoding="ascii",
        )

    def test_raw_cc_accepts_fewer_than_100_common_reflections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_xds(root, "small_a", [10, 20, 35])
            self._write_xds(root, "small_b", [12, 25, 39])
            rows = [{"Dataset": "small_a"}, {"Dataset": "small_b"}]

            # A legacy value of 100 must no longer reject this valid pair.
            result = auto_xds.pairwise_cc1_distances(root, rows, 100, None)

            self.assertEqual(result["pairwise"][0]["common_reflections"], 3)
            self.assertGreater(result["pairwise"][0]["cc1"], 0.99)

    def test_raw_cc_still_reports_mathematical_minimum(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_xds(root, "one_a", [10])
            self._write_xds(root, "one_b", [12])
            rows = [{"Dataset": "one_a"}, {"Dataset": "one_b"}]

            with self.assertRaisesRegex(ValueError, "requires at least 2"):
                auto_xds.pairwise_cc1_distances(root, rows, 0, None)

    def test_cli_default_has_no_fixed_minimum(self):
        args = auto_xds.build_parser().parse_args(
            ["cluster", "--dataset", "small_a", "--dataset", "small_b"]
        )
        self.assertEqual(args.min_common_reflections, 0)


class DeclaredCellStateTests(unittest.TestCase):
    def test_first_process_can_preserve_original_cell_then_clear_later(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inp = root / "sample" / "diff" / "p" / "XDS.INP"
            inp.parent.mkdir(parents=True)
            original = (
                "SPACE_GROUP_NUMBER= 19\n"
                "UNIT_CELL_CONSTANTS= 10 20 30 90 90 90\n"
            )
            inp.write_text(original, encoding="utf-8")
            parser = auto_xds.build_parser()
            args = parser.parse_args(
                [
                    "process", "--root", str(root), "--dataset", "sample",
                    "--clear-declared-cell-sg",
                    "--preserve-original-cell-sg-on-first-run",
                    "--skip-resolution", "--skip-xdsconv",
                ]
            )

            with mock.patch.object(auto_xds, "run_command", return_value=None):
                first = auto_xds.process_one(root / "sample", args, root, ROOT)
                self.assertEqual(auto_xds.parse_xds_inp_sg(inp), "19")
                self.assertIsNotNone(auto_xds.parse_xds_inp_cell(inp))
                self.assertEqual(first["Notes"], "preserved_original_cell_sg_first_run")

                auto_xds.process_one(root / "sample", args, root, ROOT)
                self.assertIsNone(auto_xds.parse_xds_inp_sg(inp))
                self.assertIsNone(auto_xds.parse_xds_inp_cell(inp))

    def test_set_then_clear_restores_unspecified_xds_state(self):
        with tempfile.TemporaryDirectory() as temp:
            inp = Path(temp) / "XDS.INP"
            inp.write_text(
                "!SPACE_GROUP_NUMBER= 0\n"
                "!UNIT_CELL_CONSTANTS= 10 20 30 90 90 90\n",
                encoding="utf-8",
            )

            auto_xds.update_xds_inp(
                inp,
                declared_cell="26.223 20.615 16.73 90 101.187 90",
                declared_sg="15",
            )
            self.assertEqual(auto_xds.parse_xds_inp_sg(inp), "15")
            self.assertIsNotNone(auto_xds.parse_xds_inp_cell(inp))

            auto_xds.update_xds_inp(inp, clear_declared_cell_sg=True)
            text = inp.read_text(encoding="utf-8")
            self.assertIn("!SPACE_GROUP_NUMBER= 15", text)
            self.assertIn("!UNIT_CELL_CONSTANTS= 26.223 20.615 16.73 90 101.187 90", text)
            self.assertIsNone(auto_xds.parse_xds_inp_sg(inp))
            self.assertIsNone(auto_xds.parse_xds_inp_cell(inp))


class UnmergedMergePipelineTests(unittest.TestCase):
    @staticmethod
    def _dataset(root: Path, name: str) -> Path:
        dataset = root / name
        workdir = dataset / "diff" / "p"
        workdir.mkdir(parents=True)
        (workdir / "XDS_ASCII.HKL").write_text(
            "!FORMAT=XDS_ASCII MERGE=FALSE FRIEDEL'S_LAW=TRUE\n"
            "1 0 0 100.0 5.0 1 1 1 0 1\n"
            "!END_OF_DATA\n",
            encoding="ascii",
        )
        return dataset

    @staticmethod
    def _rows() -> list[dict[str, str]]:
        return [
            {"Dataset": "sample_a", "Cell": "10 11 12 90 90 90", "SG": "1", "Rfactor": "8.0"},
            {"Dataset": "sample_b", "Cell": "10.1 11 12 90 90 90", "SG": "1", "Rfactor": "9.0"},
        ]

    def test_xdsconv_always_preserves_individual_shelx_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            target = auto_xds.prepare_xdsconv(
                workdir,
                ROOT,
                "combined.ahkl",
                "combined.hkl",
            )
            text = target.read_text(encoding="utf-8")
            active_merge = [line.strip() for line in text.splitlines() if line.strip().startswith("MERGE=")]
            self.assertEqual(active_merge, ["MERGE= FALSE"])

    def test_merge_preparation_uses_separate_inputs_and_merge_false(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._dataset(root, "sample_a")
            self._dataset(root, "sample_b")
            args = auto_xds.build_parser().parse_args(
                ["merge", "--root", str(root), "--dataset", "sample_a", "--dataset", "sample_b"]
            )
            with mock.patch.object(auto_xds, "load_summary_rows", return_value=(root / "summary.txt", self._rows())):
                result = auto_xds.merge_command(args)

            text = Path(result["xscale_input"]).read_text(encoding="utf-8")
            active_merge = [line.strip() for line in text.splitlines() if line.strip().startswith("MERGE=")]
            inputs = [line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("INPUT_FILE=")]
            self.assertEqual(active_merge, ["MERGE= FALSE"])
            self.assertEqual(inputs, ["sample_a.HKL", "sample_b.HKL"])
            self.assertEqual(result["observation_mode"], "unmerged")

    def test_premerged_xscale_output_is_rejected_before_xdsconv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._dataset(root, "sample_a")
            self._dataset(root, "sample_b")
            args = auto_xds.build_parser().parse_args(
                [
                    "merge", "--root", str(root), "--dataset", "sample_a", "--dataset", "sample_b",
                    "--merge-name", "combined", "--run",
                ]
            )

            calls: list[str] = []

            def fake_run(command, workdir, **_kwargs):
                calls.append(command[0])
                if command[0] == args.xscale_bin:
                    (workdir / "combined.ahkl").write_text(
                        "!FORMAT=XDS_ASCII MERGE=TRUE FRIEDEL'S_LAW=TRUE\n!END_OF_DATA\n",
                        encoding="ascii",
                    )
                return None

            with mock.patch.object(auto_xds, "load_summary_rows", return_value=(root / "summary.txt", self._rows())), mock.patch.object(
                auto_xds, "run_command", side_effect=fake_run
            ):
                result = auto_xds.merge_command(args)

            self.assertEqual(result["status"], "merge_xscale_premerged")
            self.assertEqual(calls, [args.xscale_bin])
            self.assertFalse((root / "sample_a" / "diff" / "p" / "combined.hkl").exists())


class DeclaredCellStateContinuationTests(unittest.TestCase):
    def test_clear_disables_every_duplicate_constraint(self):
        with tempfile.TemporaryDirectory() as temp:
            inp = Path(temp) / "XDS.INP"
            inp.write_text(
                "SPACE_GROUP_NUMBER= 15\n"
                "UNIT_CELL_CONSTANTS= 1 2 3 90 90 90\n"
                "SPACE_GROUP_NUMBER= 62\n"
                "UNIT_CELL_CONSTANTS= 4 5 6 90 90 90\n",
                encoding="utf-8",
            )

            auto_xds.update_xds_inp(inp, clear_declared_cell_sg=True)

            self.assertIsNone(auto_xds.parse_xds_inp_sg(inp))
            self.assertIsNone(auto_xds.parse_xds_inp_cell(inp))
            active = [
                line
                for line in inp.read_text(encoding="utf-8").splitlines()
                if line.startswith(("SPACE_GROUP_NUMBER=", "UNIT_CELL_CONSTANTS="))
            ]
            self.assertEqual(active, [])

    def test_setting_cell_removes_stale_duplicate_constraints(self):
        with tempfile.TemporaryDirectory() as temp:
            inp = Path(temp) / "XDS.INP"
            inp.write_text(
                "!SPACE_GROUP_NUMBER= 0\n"
                "!UNIT_CELL_CONSTANTS= 10 20 30 90 90 90\n"
                "SPACE_GROUP_NUMBER= 62\n"
                "UNIT_CELL_CONSTANTS= 4 5 6 90 90 90\n",
                encoding="utf-8",
            )

            auto_xds.update_xds_inp(
                inp,
                declared_cell="11 12 13 90 91 90",
                declared_sg="15",
            )

            text = inp.read_text(encoding="utf-8")
            active_cell = [line for line in text.splitlines() if line.startswith("UNIT_CELL_CONSTANTS=")]
            active_sg = [line for line in text.splitlines() if line.startswith("SPACE_GROUP_NUMBER=")]
            self.assertEqual(active_cell, ["UNIT_CELL_CONSTANTS= 11 12 13 90 91 90"])
            self.assertEqual(active_sg, ["SPACE_GROUP_NUMBER= 15"])
            self.assertEqual(auto_xds.parse_xds_inp_sg(inp), "15")

    def test_cli_exposes_explicit_clear_flag(self):
        process = auto_xds.build_parser().parse_args(
            ["process", "--dataset", "sample", "--clear-declared-cell-sg"]
        )
        set_cell = auto_xds.build_parser().parse_args(
            ["set-cell-sg", "--dataset", "sample", "--clear-declared-cell-sg"]
        )
        self.assertTrue(process.clear_declared_cell_sg)
        self.assertTrue(set_cell.clear_declared_cell_sg)

    def test_set_cell_command_can_clear_a_previous_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inp = root / "sample" / "diff" / "p" / "XDS.INP"
            inp.parent.mkdir(parents=True)
            inp.write_text(
                "!SPACE_GROUP_NUMBER= 0\n"
                "!UNIT_CELL_CONSTANTS= 10 20 30 90 90 90\n",
                encoding="utf-8",
            )
            parser = auto_xds.build_parser()
            set_args = parser.parse_args(
                [
                    "set-cell-sg",
                    "--root",
                    str(root),
                    "--dataset",
                    "sample",
                    "--declared-cell",
                    "10 20 30 90 90 90",
                    "--declared-sg",
                    "19",
                ]
            )
            auto_xds.set_cell_sg_command(set_args)
            self.assertEqual(auto_xds.parse_xds_inp_sg(inp), "19")

            clear_args = parser.parse_args(
                [
                    "set-cell-sg",
                    "--root",
                    str(root),
                    "--dataset",
                    "sample",
                    "--clear-declared-cell-sg",
                ]
            )
            auto_xds.set_cell_sg_command(clear_args)
            self.assertIsNone(auto_xds.parse_xds_inp_sg(inp))
            self.assertIsNone(auto_xds.parse_xds_inp_cell(inp))


if __name__ == "__main__":
    unittest.main()
