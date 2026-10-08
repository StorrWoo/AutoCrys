from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from AutoR3D.images import FrameRecord, load_image
from AutoR3D.observations import collect_peak_pixels_with_stats


class _AngleParams:
    @staticmethod
    def angle_for_frame(frame_number: int) -> float:
        return float(frame_number) * 0.25


class ParallelObservationTests(unittest.TestCase):
    def test_parallel_pipeline_reads_each_frame_once_and_keeps_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            arrays: list[np.ndarray] = []
            frames: list[FrameRecord] = []
            for frame_number in range(1, 5):
                array = np.zeros((48, 48), dtype=np.uint16)
                if frame_number != 2:
                    array[8 + frame_number * 4, 10 + frame_number * 5] = 100 + frame_number
                path = folder / f"frame_{frame_number:04d}.tif"
                Image.fromarray(array).save(path)
                arrays.append(array)
                frames.append(FrameRecord(frame_number, str(path)))

            calls: list[str] = []

            def counted(path: str | Path) -> np.ndarray:
                calls.append(str(path))
                return load_image(path)

            with patch("AutoR3D.observations.load_image", side_effect=counted):
                sparse, stats = collect_peak_pixels_with_stats(
                    frames=frames,
                    params=_AngleParams(),
                    center_x=24.0,
                    center_y=24.0,
                    peak_threshold=30.0,
                    peak_filter_size=3,
                    center_mask_radius=0.0,
                    close_point_radius=1.0,
                    mask_edge_guard_px=0.0,
                    max_spots_per_frame=20,
                    frame_workers=4,
                    preloaded_images={1: arrays[0]},
                )

            self.assertEqual(len(calls), 3)
            self.assertEqual([item.frame_number for item in stats], [1, 2, 3, 4])
            self.assertTrue(stats[1].is_zero)
            self.assertEqual(sparse.frame.tolist(), [1, 3, 4])
            self.assertEqual(list(sparse.thresholds), [1, 3, 4])


if __name__ == "__main__":
    unittest.main()
