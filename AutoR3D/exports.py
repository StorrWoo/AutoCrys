from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .observations import ObservationData
from .peaks import PeakCandidate
from .reconstruct import VolumeData


def write_json(path: str | Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _intensity_to_rgb(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return np.zeros((0, 3), dtype=np.uint8)
    lo = float(np.percentile(values, 5.0))
    hi = float(np.percentile(values, 99.0))
    scaled = np.clip((values - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    r = (255 * scaled).astype(np.uint8)
    g = (64 + 191 * np.sqrt(scaled)).astype(np.uint8)
    b = (255 * (1.0 - scaled)).astype(np.uint8)
    return np.stack([r, g, b], axis=1)


def write_points_ply(
    path: str | Path,
    points: np.ndarray,
    intensity: np.ndarray,
    extra: dict[str, np.ndarray] | None = None,
) -> None:
    path = Path(path)
    points = np.asarray(points, dtype=np.float32)
    intensity = np.asarray(intensity, dtype=np.float32)
    colors = _intensity_to_rgb(intensity)
    extra = extra or {}
    scalar_names = list(extra.keys())

    lines = [
        "ply",
        "format ascii 1.0",
        f"element vertex {points.shape[0]}",
        "property float x",
        "property float y",
        "property float z",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        "property float intensity",
    ]
    for name in scalar_names:
        lines.append(f"property float {name}")
    lines.append("end_header")

    with path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
        for i in range(points.shape[0]):
            values = [
                f"{points[i, 0]:.7g}",
                f"{points[i, 1]:.7g}",
                f"{points[i, 2]:.7g}",
                str(int(colors[i, 0])),
                str(int(colors[i, 1])),
                str(int(colors[i, 2])),
                f"{float(intensity[i]):.7g}",
            ]
            for name in scalar_names:
                values.append(f"{float(extra[name][i]):.7g}")
            handle.write(" ".join(values))
            handle.write("\n")


def write_peaks_ply(path: str | Path, peaks: list[PeakCandidate]) -> None:
    if not peaks:
        write_points_ply(path, np.zeros((0, 3), dtype=np.float32), np.zeros(0, dtype=np.float32))
        return
    points = np.asarray([peak.q for peak in peaks], dtype=np.float32)
    intensity = np.asarray([peak.intensity for peak in peaks], dtype=np.float32)
    hit_count = np.asarray([peak.hit_count for peak in peaks], dtype=np.float32)
    write_points_ply(path, points, intensity, {"hit_count": hit_count})


def sample_observations(observations: ObservationData, max_points: int) -> np.ndarray:
    n = observations.size
    if n <= max_points:
        return np.arange(n)
    # Mix bright points with deterministic spread across the acquisition.
    bright_n = max_points // 2
    spread_n = max_points - bright_n
    bright = np.argpartition(observations.intensity, -bright_n)[-bright_n:]
    spread = np.linspace(0, n - 1, spread_n, dtype=np.int64)
    return np.unique(np.concatenate([bright, spread]))[:max_points]


def write_observations_sample_ply(path: str | Path, observations: ObservationData, max_points: int) -> np.ndarray:
    keep = sample_observations(observations, max_points)
    write_points_ply(
        path,
        observations.q[keep],
        observations.intensity[keep],
        {"frame": observations.frame[keep].astype(np.float32)},
    )
    return keep


def _downsample_volume(volume: np.ndarray, max_dim: int) -> tuple[np.ndarray, tuple[int, int, int]]:
    stride = max(1, int(math.ceil(max(volume.shape) / max_dim)))
    return volume[::stride, ::stride, ::stride], (stride, stride, stride)


def write_vti(path: str | Path, volume: VolumeData, max_dim: int = 128) -> dict:
    data, stride = _downsample_volume(volume.i_mean.astype(np.float32), max_dim=max_dim)
    spacing = tuple(volume.voxel_size * s for s in stride)
    origin = volume.origin
    nx, ny, nz = data.shape
    flat = data.ravel(order="F")
    path = Path(path)
    with path.open("w", encoding="utf-8") as handle:
        handle.write('<?xml version="1.0"?>\n')
        handle.write('<VTKFile type="ImageData" version="0.1" byte_order="LittleEndian">\n')
        handle.write(
            f'  <ImageData WholeExtent="0 {nx - 1} 0 {ny - 1} 0 {nz - 1}" '
            f'Origin="{origin[0]:.7g} {origin[1]:.7g} {origin[2]:.7g}" '
            f'Spacing="{spacing[0]:.7g} {spacing[1]:.7g} {spacing[2]:.7g}">\n'
        )
        handle.write(f'    <Piece Extent="0 {nx - 1} 0 {ny - 1} 0 {nz - 1}">\n')
        handle.write('      <PointData Scalars="I_mean">\n')
        handle.write('        <DataArray type="Float32" Name="I_mean" format="ascii">\n')
        for start in range(0, flat.size, 8):
            handle.write("          " + " ".join(f"{float(v):.7g}" for v in flat[start : start + 8]) + "\n")
        handle.write("        </DataArray>\n")
        handle.write("      </PointData>\n")
        handle.write("      <CellData/>\n")
        handle.write("    </Piece>\n")
        handle.write("  </ImageData>\n")
        handle.write("</VTKFile>\n")
    return {
        "path": str(path),
        "original_shape": list(volume.shape),
        "export_shape": [nx, ny, nz],
        "stride": list(stride),
        "spacing": list(spacing),
    }
