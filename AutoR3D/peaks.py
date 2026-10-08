from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from .observations import ObservationData
from .reconstruct import VolumeData


@dataclass
class PeakCandidate:
    id: int
    q: tuple[float, float, float]
    voxel_index: tuple[int, int, int]
    intensity: float
    hit_count: float
    local_sum: float
    observation_count: int
    frame_min: int | None
    frame_max: int | None
    covariance: list[list[float]] | None
    hkl: tuple[float, float, float] | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "q": list(self.q),
            "hkl": list(self.hkl) if self.hkl is not None else None,
            "voxel_index": list(self.voxel_index),
            "intensity": self.intensity,
            "hit_count": self.hit_count,
            "local_sum": self.local_sum,
            "observation_count": self.observation_count,
            "frame_min": self.frame_min,
            "frame_max": self.frame_max,
            "covariance": self.covariance,
        }


def _weighted_covariance(q: np.ndarray, weights: np.ndarray) -> list[list[float]] | None:
    if q.shape[0] < 3 or np.sum(weights) <= 0:
        return None
    center = np.average(q, axis=0, weights=weights)
    shifted = q - center
    cov = (shifted * weights[:, None]).T @ shifted / np.sum(weights)
    return cov.astype(float).tolist()


def find_peak_candidates(
    volume: VolumeData,
    observations: ObservationData,
    percentile: float,
    max_peaks: int,
    min_hit_count: float,
    radius_voxels: float,
) -> list[PeakCandidate]:
    data = volume.i_mean.copy()
    data[volume.hit_count < min_hit_count] = 0.0
    positive = data[data > 0]
    if positive.size == 0:
        return []

    threshold = float(np.percentile(positive, percentile))
    neighborhood = ndimage.maximum_filter(data, size=3, mode="constant")
    mask = (data == neighborhood) & (data >= threshold) & (data > 0)
    coords = np.column_stack(np.nonzero(mask))
    if coords.size == 0:
        return []

    intensities = data[coords[:, 0], coords[:, 1], coords[:, 2]]
    if coords.shape[0] > max_peaks:
        keep = np.argpartition(intensities, -max_peaks)[-max_peaks:]
        coords = coords[keep]
        intensities = intensities[keep]
    order = np.argsort(intensities)[::-1]
    coords = coords[order]
    intensities = intensities[order]

    q_axes = (volume.qx, volume.qy, volume.qz)
    tree = cKDTree(observations.q.astype(np.float64)) if observations.size else None
    radius = max(radius_voxels * volume.voxel_size, volume.voxel_size)

    peaks: list[PeakCandidate] = []
    for peak_id, (idx, intensity) in enumerate(zip(coords, intensities), start=1):
        q = np.array([q_axes[0][idx[0]], q_axes[1][idx[1]], q_axes[2][idx[2]]], dtype=np.float64)
        nearby: list[int] = []
        if tree is not None:
            nearby = tree.query_ball_point(q, radius)
        if nearby:
            local_q = observations.q[nearby].astype(np.float64)
            local_i = observations.intensity[nearby].astype(np.float64)
            local_frames = observations.frame[nearby]
            cov = _weighted_covariance(local_q, np.maximum(local_i, 1.0))
            frame_min = int(np.min(local_frames))
            frame_max = int(np.max(local_frames))
            local_sum = float(np.sum(local_i))
        else:
            cov = None
            frame_min = None
            frame_max = None
            local_sum = 0.0

        peaks.append(
            PeakCandidate(
                id=peak_id,
                q=tuple(float(v) for v in q),
                voxel_index=tuple(int(v) for v in idx),
                intensity=float(intensity),
                hit_count=float(volume.hit_count[tuple(idx)]),
                local_sum=local_sum,
                observation_count=int(len(nearby)),
                frame_min=frame_min,
                frame_max=frame_max,
                covariance=cov,
            )
        )
    return peaks


def save_peak_candidates(path: str | Path, peaks: list[PeakCandidate]) -> None:
    payload = {"peaks": [peak.to_dict() for peak in peaks], "count": len(peaks)}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
