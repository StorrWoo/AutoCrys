from __future__ import annotations

import unittest

import numpy as np

from AutoR3D.axis import (
    AxisScore,
    _best_projection_score,
    _renormalize_scores,
    points_to_unit_vectors,
    spherical_angles,
    spherical_lattice_order_index,
    volume_projection_points,
)
from AutoR3D.reconstruct import VolumeData


class AxisScoringTests(unittest.TestCase):
    def test_spherical_projection_uses_unit_vectors(self) -> None:
        points = np.array(
            [
                [2.0, 0.0, 0.0],
                [0.0, 3.0, 0.0],
                [0.0, 0.0, -4.0],
            ],
            dtype=np.float64,
        )
        dirs = points_to_unit_vectors(points)
        theta, phi = spherical_angles(points)

        self.assertTrue(np.allclose(np.linalg.norm(dirs, axis=1), 1.0))
        self.assertAlmostEqual(theta[0], 0.0)
        self.assertAlmostEqual(theta[1], np.pi / 2.0)
        self.assertAlmostEqual(phi[2], -np.pi / 2.0)

    def test_direction_score_prefers_repeated_directions(self) -> None:
        rng = np.random.default_rng(17)
        theta = np.linspace(-np.pi, np.pi, 36, endpoint=False)
        equator = np.column_stack([np.cos(theta), np.sin(theta), np.zeros_like(theta)])
        radii = np.linspace(0.3, 1.0, 4)
        ordered = np.vstack([equator * radius for radius in radii])

        random_dirs = rng.normal(size=ordered.shape)
        random_dirs /= np.linalg.norm(random_dirs, axis=1)[:, None]
        random_points = random_dirs * rng.uniform(0.3, 1.0, size=(ordered.shape[0], 1))

        ordered_result = spherical_lattice_order_index(
            ordered,
            duplicate_deg=1.2,
        )
        random_result = spherical_lattice_order_index(
            random_points,
            duplicate_deg=1.2,
        )

        self.assertGreater(ordered_result["direction_concentration_score"], random_result["direction_concentration_score"])
        self.assertEqual(ordered_result["great_circle_score"], 0.0)

    def test_volume_projection_points_drop_weakest_ten_percent(self) -> None:
        i_mean = np.arange(1, 11, dtype=np.float32).reshape(10, 1, 1)
        volume = VolumeData(
            i_sum=i_mean.copy(),
            hit_count=np.ones_like(i_mean),
            i_mean=i_mean,
            qx=np.arange(10, dtype=np.float32),
            qy=np.asarray([0.0], dtype=np.float32),
            qz=np.asarray([0.0], dtype=np.float32),
            origin=(0.0, 0.0, 0.0),
            voxel_size=1.0,
            method="test",
        )

        q, weights = volume_projection_points(volume)

        self.assertEqual(weights.size, 9)
        self.assertNotIn(1.0, weights.tolist())
        self.assertEqual(q.shape[0], weights.size)

    def test_axis_selection_prefers_higher_direction_concentration(self) -> None:
        scores = _renormalize_scores(
            [
                AxisScore(38.0, 0.8, 0.0, 0.8, 0.0, 10, 10, "test"),
                AxisScore(42.0, 0.2, 0.0, 0.2, 0.0, 10, 10, "test"),
            ]
        )

        best = _best_projection_score(scores, base_axis_deg=40.0)

        self.assertEqual(best.rotation_axis_deg, 38.0)
        self.assertGreater(scores[0].combined_score, scores[1].combined_score)


if __name__ == "__main__":
    unittest.main()
