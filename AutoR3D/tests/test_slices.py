from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from AutoR3D.slices import (
    MAX_SLICE_BINS_PER_AXIS,
    SLICE_HEADER_HEIGHT,
    SliceSpec,
    build_slice,
    render_slice_image,
    save_slice_npz,
    slice_title,
    write_canonical_slices,
    write_slice,
)


class SliceTests(unittest.TestCase):
    def test_hk0_slice_keeps_only_the_layer(self) -> None:
        hkl = np.array(
            [
                [1.0, 2.0, 0.0],     # inside l=0
                [2.0, 1.0, 0.03],    # inside +-0.04
                [3.0, 3.0, 0.4],     # far outside
                [-1.0, -2.0, -0.02],  # inside
            ]
        )
        intensity = np.array([10.0, 20.0, 30.0, 40.0])
        spec = SliceSpec(family="hk0", layer=0, thickness_rlu=0.08, step_rlu=0.01)
        data = build_slice(hkl, intensity, spec)

        self.assertEqual(data.point_count, 3)
        self.assertAlmostEqual(data.total_intensity, 70.0)
        self.assertEqual(data.image.shape, data.counts.shape)
        self.assertGreaterEqual(data.axis_u.size, 5)
        self.assertIn("hk0 slice", slice_title(data))

    def test_slice_bins_sum_intensity_at_the_same_cell(self) -> None:
        hkl = np.array([[0.0, 0.0, 0.0], [0.005, 0.004, 0.0]])
        intensity = np.array([1.5, 2.5])
        spec = SliceSpec(family="hk0", step_rlu=0.01, thickness_rlu=0.08)
        data = build_slice(hkl, intensity, spec)
        self.assertEqual(data.point_count, 2)
        self.assertAlmostEqual(float(data.image.sum()), 4.0)

    def test_family_plane_axes(self) -> None:
        self.assertEqual(SliceSpec(family="hk0").plane_axes(), (0, 1, 2))
        self.assertEqual(SliceSpec(family="h0l").plane_axes(), (0, 2, 1))
        self.assertEqual(SliceSpec(family="0kl").plane_axes(), (1, 2, 0))
        with self.assertRaises(ValueError):
            SliceSpec(family="hhl")

    def test_zero_and_negative_layers_are_valid(self) -> None:
        hkl = np.array([[1.0, 2.0, 0.0], [1.0, 2.0, -1.0], [1.0, 2.0, 1.0]])
        intensity = np.array([10.0, 20.0, 30.0])

        zero = build_slice(hkl, intensity, SliceSpec(family="hk0", layer=0, thickness_rlu=0.1))
        negative = build_slice(hkl, intensity, SliceSpec(family="hk0", layer=-1, thickness_rlu=0.1))

        self.assertEqual(zero.point_count, 1)
        self.assertAlmostEqual(zero.total_intensity, 10.0)
        self.assertEqual(negative.point_count, 1)
        self.assertAlmostEqual(negative.total_intensity, 20.0)

    def test_requested_step_is_coarsened_to_bound_grid_size(self) -> None:
        hkl = np.array([[-1000.0, 0.0, 0.0], [1000.0, 0.0, 0.0]])
        intensity = np.array([1.0, 1.0])
        data = build_slice(
            hkl,
            intensity,
            SliceSpec(family="hk0", layer=0, thickness_rlu=0.1, step_rlu=0.001),
        )

        self.assertLessEqual(data.shape[1], MAX_SLICE_BINS_PER_AXIS + 2)
        self.assertGreater(data.effective_step_rlu, data.spec.step_rlu)

    def test_rendered_slice_plot_area_is_square(self) -> None:
        hkl = np.array([[-8.0, -1.0, 0.0], [8.0, 1.0, 0.0]])
        intensity = np.array([1.0, 2.0])
        data = build_slice(
            hkl,
            intensity,
            SliceSpec(family="hk0", layer=0, thickness_rlu=0.1, step_rlu=0.1),
        )

        self.assertNotEqual(data.shape[0], data.shape[1])
        rendered = render_slice_image(data)
        self.assertEqual(rendered.width, rendered.height - SLICE_HEADER_HEIGHT)
        without_cell = render_slice_image(data, show_cell=False)
        self.assertEqual(rendered.size, without_cell.size)
        self.assertFalse(np.array_equal(np.asarray(rendered), np.asarray(without_cell)))
        thresholded = render_slice_image(data, threshold_percent=75.0)
        self.assertEqual(rendered.size, thresholded.size)
        self.assertFalse(np.array_equal(np.asarray(rendered), np.asarray(thresholded)))

    def test_write_canonical_slices(self) -> None:
        rng = np.random.default_rng(3)
        hkl = rng.normal(size=(400, 3)) * 4.0
        intensity = rng.uniform(1.0, 100.0, size=400)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            info = write_canonical_slices(out, hkl, intensity, thickness_rlu=0.1, step_rlu=0.05)
            self.assertEqual(set(info), {"hk0", "h0l", "0kl"})
            for family, entry in info.items():
                png = Path(str(entry["png"]))
                npz = Path(str(entry["npz"]))
                self.assertTrue(png.is_file(), family)
                self.assertTrue(npz.is_file(), family)
                with np.load(npz) as data:
                    self.assertIn("image", data.files)
                    self.assertEqual(str(data["family"][0]), family)
                    self.assertAlmostEqual(float(data["thickness_rlu"][0]), 0.1, places=6)

    def test_empty_slice_is_written(self) -> None:
        hkl = np.array([[0.0, 0.0, 5.0]])
        intensity = np.array([1.0])
        with tempfile.TemporaryDirectory() as temp:
            info = write_slice(Path(temp), hkl, intensity, SliceSpec(family="hk0"))
            self.assertEqual(info["point_count"], 0)
            self.assertTrue(Path(str(info["png"])).is_file())


if __name__ == "__main__":
    unittest.main()
