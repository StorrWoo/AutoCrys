from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .observations import ObservationData


@dataclass
class VolumeData:
    i_sum: np.ndarray
    hit_count: np.ndarray
    i_mean: np.ndarray
    qx: np.ndarray
    qy: np.ndarray
    qz: np.ndarray
    origin: tuple[float, float, float]
    voxel_size: float
    method: str

    @property
    def shape(self) -> tuple[int, int, int]:
        return tuple(int(v) for v in self.i_sum.shape)

    def save_npz(self, path: str | Path) -> None:
        np.savez_compressed(
            path,
            I_sum=self.i_sum.astype(np.float32),
            hit_count=self.hit_count.astype(np.float32),
            I_mean=self.i_mean.astype(np.float32),
            qx=self.qx.astype(np.float32),
            qy=self.qy.astype(np.float32),
            qz=self.qz.astype(np.float32),
            origin=np.asarray(self.origin, dtype=np.float32),
            voxel_size=np.asarray([self.voxel_size], dtype=np.float32),
            method=np.asarray([self.method]),
        )


def _volume_axes(q: np.ndarray, voxel_size: float, margin_voxels: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if q.size == 0:
        raise ValueError("Cannot build a volume with no q observations.")
    q_min = np.min(q, axis=0) - margin_voxels * voxel_size
    q_max = np.max(q, axis=0) + margin_voxels * voxel_size
    axes = []
    for lo, hi in zip(q_min, q_max):
        n = int(np.ceil((hi - lo) / voxel_size)) + 1
        axes.append((lo + np.arange(n, dtype=np.float32) * voxel_size).astype(np.float32))
    return axes[0], axes[1], axes[2]


def _flat_index(ix: np.ndarray, iy: np.ndarray, iz: np.ndarray, shape: tuple[int, int, int]) -> np.ndarray:
    return np.ravel_multi_index((ix, iy, iz), shape)


def _accumulate_flat(
    i_sum_flat: np.ndarray,
    hit_flat: np.ndarray,
    flat: np.ndarray,
    values: np.ndarray,
    hits: np.ndarray,
) -> None:
    if flat.size == 0:
        return
    order = np.argsort(flat)
    flat_sorted = flat[order]
    values_sorted = values[order]
    hits_sorted = hits[order]
    unique_flat, start = np.unique(flat_sorted, return_index=True)
    i_sum_flat[unique_flat] += np.add.reduceat(values_sorted, start).astype(np.float32, copy=False)
    hit_flat[unique_flat] += np.add.reduceat(hits_sorted, start).astype(np.float32, copy=False)


def grid_observations(
    observations: ObservationData,
    voxel_size: float,
    method: str = "trilinear",
    margin_voxels: int = 2,
) -> VolumeData:
    if observations.size == 0:
        raise ValueError("No observations available for reconstruction.")
    q = observations.q.astype(np.float64, copy=False)
    qx, qy, qz = _volume_axes(q, voxel_size=voxel_size, margin_voxels=margin_voxels)
    origin = (float(qx[0]), float(qy[0]), float(qz[0]))
    shape = (len(qx), len(qy), len(qz))
    total_voxels = int(np.prod(shape))
    if total_voxels > 120_000_000:
        raise ValueError(
            f"Requested volume shape {shape} has {total_voxels:,} voxels. "
            "Increase --voxel-size or lower the observation threshold."
        )

    i_sum_flat = np.zeros(total_voxels, dtype=np.float32)
    hit_flat = np.zeros(total_voxels, dtype=np.float32)
    frac = (q - np.asarray(origin)) / voxel_size

    if method == "nearest":
        idx = np.rint(frac).astype(np.int64)
        valid = np.all((idx >= 0) & (idx < np.asarray(shape)), axis=1)
        flat = _flat_index(idx[valid, 0], idx[valid, 1], idx[valid, 2], shape)
        values = observations.intensity[valid].astype(np.float32, copy=False)
        hits = np.ones(values.size, dtype=np.float32)
        _accumulate_flat(i_sum_flat, hit_flat, flat, values, hits)
    elif method == "trilinear":
        base = np.floor(frac).astype(np.int64)
        delta = frac - base
        intensity = observations.intensity.astype(np.float32, copy=False)
        for ox in (0, 1):
            wx = (1.0 - delta[:, 0]) if ox == 0 else delta[:, 0]
            ix = base[:, 0] + ox
            for oy in (0, 1):
                wy = (1.0 - delta[:, 1]) if oy == 0 else delta[:, 1]
                iy = base[:, 1] + oy
                for oz in (0, 1):
                    wz = (1.0 - delta[:, 2]) if oz == 0 else delta[:, 2]
                    iz = base[:, 2] + oz
                    weight = (wx * wy * wz).astype(np.float32)
                    valid = (
                        (ix >= 0)
                        & (ix < shape[0])
                        & (iy >= 0)
                        & (iy < shape[1])
                        & (iz >= 0)
                        & (iz < shape[2])
                        & (weight > 0)
                    )
                    if not np.any(valid):
                        continue
                    flat = _flat_index(ix[valid], iy[valid], iz[valid], shape)
                    hit_values = weight[valid].astype(np.float32, copy=False)
                    intensity_values = intensity[valid] * hit_values
                    _accumulate_flat(i_sum_flat, hit_flat, flat, intensity_values, hit_values)
    else:
        raise ValueError(f"Unknown gridding method: {method}")

    i_sum = i_sum_flat.reshape(shape)
    hit_count = hit_flat.reshape(shape)
    i_mean = np.zeros_like(i_sum, dtype=np.float32)
    np.divide(i_sum, hit_count, out=i_mean, where=hit_count > 0)
    return VolumeData(
        i_sum=i_sum,
        hit_count=hit_count,
        i_mean=i_mean,
        qx=qx,
        qy=qy,
        qz=qz,
        origin=origin,
        voxel_size=float(voxel_size),
        method=method,
    )
