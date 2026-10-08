from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from AutoR3D.observations import ObservationData
from AutoR3D.panel import _panel_data, write_panel
from AutoR3D.peaks import PeakCandidate
from AutoR3D.reconstruct import VolumeData


class PanelTests(unittest.TestCase):
    def test_panel_uses_shader_points_with_visual_controls(self) -> None:
        peaks = [
            PeakCandidate(
                id=1,
                q=(0.1, 0.2, 0.3),
                voxel_index=(0, 0, 0),
                intensity=10.0,
                hit_count=2.0,
                local_sum=10.0,
                observation_count=2,
                frame_min=1,
                frame_max=2,
                covariance=None,
            )
        ]
        observations = ObservationData(
            frame=np.asarray([1], dtype=np.int32),
            x=np.asarray([10.0], dtype=np.float32),
            y=np.asarray([12.0], dtype=np.float32),
            q=np.asarray([[0.1, 0.2, 0.3]], dtype=np.float32),
            intensity=np.asarray([10.0], dtype=np.float32),
            background=np.asarray([0.0], dtype=np.float32),
            sigma=np.asarray([1.0], dtype=np.float32),
        )
        volume = VolumeData(
            i_sum=np.ones((1, 1, 1), dtype=np.float32),
            hit_count=np.ones((1, 1, 1), dtype=np.float32),
            i_mean=np.ones((1, 1, 1), dtype=np.float32),
            qx=np.asarray([0.1], dtype=np.float32),
            qy=np.asarray([0.2], dtype=np.float32),
            qz=np.asarray([0.3], dtype=np.float32),
            origin=(0.1, 0.2, 0.3),
            voxel_size=0.01,
            method="test",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = write_panel(Path(tmp), peaks, observations, volume)
            html = path.read_text(encoding="utf-8")

        self.assertIn("THREE.ShaderMaterial", html)
        self.assertIn("new THREE.Points", html)
        self.assertIn("Display mode", html)
        self.assertIn("Gaussian", html)
        self.assertIn("Brightness", html)
        self.assertIn("Contrast", html)
        self.assertIn("Gamma", html)
        self.assertIn('id="brightness" type="range" min="0" max="3" step="0.05" value="3"', html)
        self.assertIn('id="contrast" type="range" min="0.1" max="3" step="0.05" value="3"', html)
        self.assertIn('id="showCell"', html)
        self.assertIn('data-view="a"', html)
        self.assertIn('data-view="b"', html)
        self.assertIn('data-view="c"', html)
        self.assertIn("new THREE.LineSegments", html)
        self.assertIn("screenSpacePanning", html)
        self.assertIn('key === "g"', html)
        self.assertIn("new THREE.OrthographicCamera", html)
        self.assertNotIn("PerspectiveCamera", html)
        self.assertIn("Shift/Alt/Ctrl constrain rotation", html)
        self.assertIn('event.shiftKey ? "roll"', html)
        self.assertIn("for (const first of [-1, 0, 1])", html)
        self.assertIn('const upAxisIndex = name === "c" ? 1 : 2', html)

    def test_indexed_panel_keeps_physical_q_and_uses_reciprocal_basis(self) -> None:
        peaks = [
            PeakCandidate(
                id=1,
                q=(0.05, 0.02, 0.08),
                hkl=(1.0, 2.0, 3.0),
                voxel_index=(0, 0, 0),
                intensity=10.0,
                hit_count=2.0,
                local_sum=10.0,
                observation_count=2,
                frame_min=1,
                frame_max=2,
                covariance=None,
            )
        ]
        observations = ObservationData(
            frame=np.asarray([1], dtype=np.int32),
            x=np.asarray([10.0], dtype=np.float32),
            y=np.asarray([12.0], dtype=np.float32),
            q=np.asarray([[0.05, 0.02, 0.08]], dtype=np.float32),
            intensity=np.asarray([10.0], dtype=np.float32),
            background=np.asarray([0.0], dtype=np.float32),
            sigma=np.asarray([1.0], dtype=np.float32),
        )
        volume = VolumeData(
            i_sum=np.ones((1, 1, 1), dtype=np.float32),
            hit_count=np.ones((1, 1, 1), dtype=np.float32),
            i_mean=np.ones((1, 1, 1), dtype=np.float32),
            qx=np.asarray([0.05], dtype=np.float32),
            qy=np.asarray([0.02], dtype=np.float32),
            qz=np.asarray([0.08], dtype=np.float32),
            origin=(0.05, 0.02, 0.08),
            voxel_size=0.01,
            method="test",
        )
        reciprocal_basis = [
            [0.05867, 0.0, 0.03807],
            [0.0, 0.03453, 0.0],
            [0.0, 0.0, 0.09183],
        ]
        crystal = {
            "cell": [18.453, 28.958, 10.891, 90.0, 112.532, 90.0],
            "space_group_number": 5,
            "reciprocal_basis": reciprocal_basis,
        }

        data = _panel_data(
            peaks,
            observations,
            volume,
            max_observations=10,
            indexed_observations=np.asarray([[1.0, 2.0, 3.0]], dtype=np.float64),
            crystal=crystal,
        )
        self.assertEqual(data["space"], "q")
        self.assertTrue(data["indexed"])
        self.assertTrue(data["indexed_geometry"])
        np.testing.assert_allclose(data["observations"][0]["q"], observations.q[0])
        np.testing.assert_allclose(data["peaks"][0]["q"], peaks[0].q)

        with tempfile.TemporaryDirectory() as tmp:
            path = write_panel(
                Path(tmp),
                peaks,
                observations,
                volume,
                indexed_observations=np.asarray([[1.0, 2.0, 3.0]], dtype=np.float64),
                crystal=crystal,
            )
            html = path.read_text(encoding="utf-8")

        self.assertIn("const hklToQ", html)
        self.assertIn("cellPositions.push(...hklToQ(start), ...hklToQ(end))", html)
        self.assertIn('"indexed_geometry":true', html)
        self.assertIn('"space":"q"', html)


if __name__ == "__main__":
    unittest.main()
