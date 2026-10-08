from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from AutoR3D.xds import load_xds_reference, reciprocal_basis_from_axes


ROOT = Path(__file__).resolve().parents[2]
SPD2_1 = ROOT / "Data" / "sample2_1"
LEGACY_DEMO = ROOT / "Demo" / "AutoR3D_Demo_single_legacy"


class XdsReferenceTests(unittest.TestCase):
    def test_spd2_1_reference_matches_xds(self) -> None:
        if not (SPD2_1 / "diff" / "p" / "CORRECT.LP").is_file():
            self.skipTest("sample2_1 XDS output is not available")
        reference = load_xds_reference(SPD2_1)
        self.assertIsNotNone(reference)
        assert reference is not None

        # refined axis 0.789158 -0.614179 0.003788 -> atan2 = -37.90 deg
        self.assertAlmostEqual(reference.axis_angle_deg, -37.90, delta=0.6)
        self.assertAlmostEqual(reference.axis_vector[0], 0.789, delta=0.006)
        self.assertAlmostEqual(reference.axis_vector[1], -0.614, delta=0.006)
        self.assertAlmostEqual(reference.rotation_axis_deg, reference.axis_angle_deg, delta=1e-9)
        self.assertEqual(reference.rotation_sign, -1.0)
        self.assertTrue(reference.angle_mid_frame)
        self.assertTrue(reference.has_orientation)

        cell = reference.cell_constants
        self.assertIsNotNone(cell)
        assert cell is not None
        self.assertAlmostEqual(cell[0], 19.81, delta=0.1)
        self.assertAlmostEqual(cell[1], 19.98, delta=0.1)
        self.assertAlmostEqual(cell[2], 13.22, delta=0.1)

        basis = np.asarray(reference.reciprocal_basis, dtype=np.float64)
        magnitudes = [float(np.linalg.norm(basis[:, index])) for index in range(3)]
        self.assertAlmostEqual(magnitudes[0], 0.050484, delta=3e-4)
        self.assertAlmostEqual(magnitudes[1], 0.050043, delta=3e-4)
        self.assertAlmostEqual(magnitudes[2], 0.075627, delta=3e-4)

        beam = reference.beam_center_px
        self.assertIsNotNone(beam)
        assert beam is not None
        self.assertAlmostEqual(beam[0], 259.16, delta=0.3)
        self.assertAlmostEqual(beam[1], 258.79, delta=0.3)
        self.assertAlmostEqual(reference.detector_distance_mm or 0.0, 443.0, delta=0.01)
        self.assertAlmostEqual(reference.wavelength_angstrom or 0.0, 0.0251, delta=1e-4)
        self.assertEqual(reference.space_group_number, 62)

    def test_index_q_round_trip(self) -> None:
        # triclinic cell so a transposed inverse would be caught
        axes = ((10.0, 0.0, 0.0), (1.2, 12.0, 0.0), (0.5, 0.7, 9.0))
        basis = np.asarray(reciprocal_basis_from_axes(axes), dtype=np.float64)
        hkl = np.array([[3.0, -2.0, 5.0], [0.0, 0.0, 0.0], [-7.0, 1.0, -3.0]])
        q = (basis @ hkl.T).T

        reference = _reference_with_basis(basis)
        recovered = reference.index_q(q)
        self.assertTrue(np.allclose(recovered, hkl, atol=1e-9))

        # and the direct definition hkl = M^T q with M = [a b c]
        matrix = np.asarray(axes, dtype=np.float64).T
        self.assertTrue(np.allclose((matrix.T @ q.T).T, hkl, atol=1e-9))

    def test_missing_xds_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(load_xds_reference(Path(temp)))

    def test_axis_only_reference_from_demo(self) -> None:
        if not (LEGACY_DEMO / "diff" / "p" / "XDS.INP").is_file():
            self.skipTest("legacy AutoR3D demo is not available")
        reference = load_xds_reference(LEGACY_DEMO)
        self.assertIsNotNone(reference)
        assert reference is not None
        # XDS.INP input axis 0.7845 -0.6201 0.0041 -> atan2 = -38.33 deg
        self.assertAlmostEqual(reference.axis_angle_deg, -38.33, delta=0.6)
        self.assertFalse(reference.has_orientation)
        self.assertIsNotNone(reference.cell_constants)
        self.assertEqual(reference.rotation_sign, -1.0)


def _reference_with_basis(basis: np.ndarray):
    from AutoR3D.xds import XdsReference

    return XdsReference(
        dataset="synthetic",
        files={},
        axis_vector=(1.0, 0.0, 0.0),
        axis_angle_deg=0.0,
        rotation_axis_deg=0.0,
        rotation_sign=-1.0,
        angle_mid_frame=True,
        cell_constants=None,
        cell_axes=None,
        reciprocal_basis=tuple(tuple(float(value) for value in row) for row in basis),
        beam_center_px=None,
        detector_distance_mm=None,
        wavelength_angstrom=None,
        space_group_number=None,
        starting_angle_deg=None,
        oscillation_angle_deg=None,
        first_frame=None,
        last_frame=None,
    )


if __name__ == "__main__":
    unittest.main()
