from __future__ import annotations

import unittest

import numpy as np
from scipy.ndimage import maximum_filter

from AutoR3D.images import circular_footprint, circular_local_maxima, extract_diffraction_points


class PeakMaskEdgeTests(unittest.TestCase):
    def test_fast_circular_maxima_match_full_footprint_filter(self) -> None:
        rng = np.random.default_rng(17)
        image = rng.integers(0, 100, size=(71, 67)).astype(np.float32)
        image[:2, :3] = 150.0
        image[-1, -1] = 175.0

        for radius in (1, 2, 5, 12):
            filtered = maximum_filter(
                image,
                footprint=circular_footprint(radius),
                mode="reflect",
            )
            expected = np.column_stack(np.nonzero((image == filtered) & (image > 0)))
            actual = circular_local_maxima(image, radius)
            np.testing.assert_array_equal(actual, expected)

    def test_center_mask_edge_guard_rejects_edge_artifact(self) -> None:
        image = np.zeros((64, 64), dtype=np.float32)
        image[32, 44] = 100.0
        image[32, 56] = 120.0

        guarded, _, _ = extract_diffraction_points(
            image,
            threshold=30.0,
            filter_size=10,
            center_x=32.0,
            center_y=32.0,
            center_mask_radius=10.0,
            min_distance_px=1.0,
            max_spots=100,
        )
        unguarded, _, _ = extract_diffraction_points(
            image,
            threshold=30.0,
            filter_size=10,
            center_x=32.0,
            center_y=32.0,
            center_mask_radius=10.0,
            min_distance_px=1.0,
            max_spots=100,
            mask_edge_guard_px=0.0,
        )

        self.assertEqual([(round(x), round(y)) for (x, y), _ in guarded], [(56, 32)])
        self.assertEqual(
            [(round(x), round(y)) for (x, y), _ in unguarded],
            [(44, 32), (56, 32)],
        )


if __name__ == "__main__":
    unittest.main()
