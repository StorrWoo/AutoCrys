from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from AutoSolve.structure_model import grow_structure, parse_shelx, parse_symmetry_operation, unit_cell_edges
from config.autocrys_common import external_target_path
from UI.ai_assistant.config import Config


class StructureModelTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "Demo" / "AutoSolve" / "1.res").is_file(), "optional real-model fixture is not distributed")
    def test_parses_demo_res_and_excludes_fourier_peaks(self):
        model = parse_shelx(ROOT / "Demo" / "AutoSolve" / "1.res")
        self.assertGreater(len(model.atoms), 20)
        self.assertGreater(len(model.bonds), 10)
        self.assertIn("Au", {atom.element for atom in model.atoms})
        self.assertFalse(any(atom.label.upper().startswith("Q") for atom in model.atoms))
        self.assertAlmostEqual(model.cell.a, 12.528)
        self.assertEqual(len(model.symmetry_operations), 2)

        grown_once = grow_structure(model)
        grown = grow_structure(model, steps=2)
        self.assertGreater(len(grown_once.atoms), len(model.atoms))
        self.assertGreater(len(grown.atoms), len(grown_once.atoms))
        self.assertGreater(len(grown.atoms), len(model.atoms))
        self.assertGreater(len(grown.bonds), len(model.bonds))

    def test_parses_shelx_symmetry_fraction_expression(self):
        operation = parse_symmetry_operation("-X, 1/2+Y, -Z")
        transformed = operation.apply((0.1, 0.2, 0.3))
        self.assertAlmostEqual(float(transformed[0]), -0.1)
        self.assertAlmostEqual(float(transformed[1]), 0.7)
        self.assertAlmostEqual(float(transformed[2]), -0.3)

    def test_cartesian_coordinates_and_radius_bonds(self):
        content = """\
TITL simple
CELL 0.71073 10 10 10 90 90 90
SFAC C O H
UNIT 1 1 1
C1 1 0.1000 0.1000 0.1000 11.0 0.04
O1 2 0.2200 0.1000 0.1000 11.0 0.04
H1 3 0.9000 0.9000 0.9000 11.0 0.04
HKLF 4
Q1 1 0.1500 0.1000 0.1000 11.0 0.05 1.0
END
"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "simple.ins"
            path.write_text(content, encoding="utf-8")
            model = parse_shelx(path)
        self.assertEqual([atom.label for atom in model.atoms], ["C1", "O1", "H1"])
        self.assertEqual(len(model.bonds), 1)
        self.assertEqual((model.bonds[0].first, model.bonds[0].second), (0, 1))
        self.assertAlmostEqual(model.bonds[0].length, 1.2)
        corners, edges = unit_cell_edges(model.cell)
        self.assertEqual(corners.shape, (8, 3))
        self.assertEqual(len(edges), 12)

    def test_rejects_non_shelx_extension(self):
        with self.assertRaisesRegex(ValueError, "res or .ins"):
            parse_shelx(ROOT / "README.md")


class StructureViewerConfigTests(unittest.TestCase):
    def test_defaults_enable_viewer_and_merge_partial_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"structure_viewer": {"enabled": False}}), encoding="utf-8")
            config = Config(path)
        self.assertFalse(config.structure_viewer_enabled)
        self.assertTrue(config.structure_viewer_show_unit_cell)
        self.assertAlmostEqual(config.structure_viewer_bond_tolerance, 0.45)
        self.assertEqual(config.structure_viewer_grow_steps, 2)

    def test_tolerance_is_safely_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                json.dumps({"structure_viewer": {"bond_tolerance_angstrom": 99}}),
                encoding="utf-8",
            )
            config = Config(path)
        self.assertEqual(config.structure_viewer_bond_tolerance, 1.5)

    def test_grow_steps_are_safely_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                json.dumps({"structure_viewer": {"grow_steps_per_click": 99}}),
                encoding="utf-8",
            )
            config = Config(path)
        self.assertEqual(config.structure_viewer_grow_steps, 4)


class ExternalApplicationPathTests(unittest.TestCase):
    def test_windows_executable_receives_a_windows_visible_wsl_path(self):
        target = ROOT / "Demo" / "AutoSolve" / "1.res"
        converted = external_target_path(Path("/mnt/c/Program Files/Olex2-1.5/olex2.exe"), target)
        self.assertNotEqual(converted, str(target.resolve()))
        self.assertIn("\\", converted)
        self.assertTrue(converted.lower().endswith("\\demo\\autosolve\\1.res"))


if __name__ == "__main__":
    unittest.main()
