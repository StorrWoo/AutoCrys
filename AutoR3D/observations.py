from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path

import numpy as np

from .cred2 import CRED2Parameters
from .geometry import GeometryConfig, pixel_to_q_sample
from .images import (
    FrameRecord,
    FrameStats,
    extract_diffraction_points,
    inspect_image,
    load_image,
    significant_pixel_mask,
)


DEFAULT_FRAME_WORKERS = min(8, max(1, os.cpu_count() or 1))


@dataclass
class SparsePixels:
    frame: np.ndarray
    x: np.ndarray
    y: np.ndarray
    angle_deg: np.ndarray
    intensity: np.ndarray
    background: np.ndarray
    sigma: np.ndarray
    thresholds: dict[int, float]
    mode: str = "pixels"

    @property
    def size(self) -> int:
        return int(self.intensity.size)


@dataclass
class ObservationData:
    frame: np.ndarray
    x: np.ndarray
    y: np.ndarray
    q: np.ndarray
    intensity: np.ndarray
    background: np.ndarray
    sigma: np.ndarray

    @property
    def size(self) -> int:
        return int(self.intensity.size)

    def save_npz(self, path: str | Path) -> None:
        np.savez_compressed(
            path,
            frame=self.frame.astype(np.int32),
            x=self.x.astype(np.float32),
            y=self.y.astype(np.float32),
            q=self.q.astype(np.float32),
            intensity=self.intensity.astype(np.float32),
            background=self.background.astype(np.float32),
            sigma=self.sigma.astype(np.float32),
        )


@dataclass
class _PeakFrameResult:
    record: FrameRecord
    stats: FrameStats
    points: list[tuple[tuple[float, float], float]]
    background: float
    sigma: float


@dataclass
class _SparseFrameResult:
    record: FrameRecord
    stats: FrameStats
    x: np.ndarray
    y: np.ndarray
    intensity: np.ndarray
    background: float
    sigma: float
    threshold: float


def _ordered_parallel_map(frames, worker, frame_workers: int):
    workers = max(1, int(frame_workers))
    if workers == 1:
        return [worker(record) for record in frames]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="autor3d-frame") as executor:
        return list(executor.map(worker, frames))


def collect_sparse_pixels(
    frames: list[FrameRecord],
    frame_stats: dict[int, FrameStats],
    params: CRED2Parameters,
    center_x: float,
    center_y: float,
    threshold_sigma: float,
    min_intensity: float,
    center_mask_radius: float,
    max_pixels_per_frame: int,
    max_frames: int | None = None,
    exclusion_mask: np.ndarray | None = None,
) -> SparsePixels:
    frame_values: list[np.ndarray] = []
    x_values: list[np.ndarray] = []
    y_values: list[np.ndarray] = []
    angle_values: list[np.ndarray] = []
    intensity_values: list[np.ndarray] = []
    background_values: list[np.ndarray] = []
    sigma_values: list[np.ndarray] = []
    thresholds: dict[int, float] = {}

    used = 0
    for record in frames:
        if max_frames is not None and used >= max_frames:
            break
        stats = frame_stats[record.frame_number]
        if stats.is_zero:
            continue

        array = load_image(record.path)
        mask, background, sigma, threshold = significant_pixel_mask(
            array,
            threshold_sigma=threshold_sigma,
            min_intensity=min_intensity,
            center_x=center_x,
            center_y=center_y,
            center_mask_radius=center_mask_radius,
            exclusion_mask=exclusion_mask,
        )
        yy, xx = np.nonzero(mask)
        if xx.size == 0:
            thresholds[record.frame_number] = float(threshold)
            used += 1
            continue

        intensities = array[yy, xx].astype(np.float32)
        if max_pixels_per_frame > 0 and intensities.size > max_pixels_per_frame:
            keep = np.argpartition(intensities, -max_pixels_per_frame)[-max_pixels_per_frame:]
            xx = xx[keep]
            yy = yy[keep]
            intensities = intensities[keep]

        n = intensities.size
        angle = params.angle_for_frame(record.frame_number)
        frame_values.append(np.full(n, record.frame_number, dtype=np.int32))
        x_values.append(xx.astype(np.float32))
        y_values.append(yy.astype(np.float32))
        angle_values.append(np.full(n, angle, dtype=np.float32))
        intensity_values.append(intensities)
        background_values.append(np.full(n, background, dtype=np.float32))
        sigma_values.append(np.full(n, sigma, dtype=np.float32))
        thresholds[record.frame_number] = float(threshold)
        used += 1

    if not intensity_values:
        empty_i = np.array([], dtype=np.int32)
        empty_f = np.array([], dtype=np.float32)
        return SparsePixels(empty_i, empty_f, empty_f, empty_f, empty_f, empty_f, empty_f, thresholds)

    return SparsePixels(
        frame=np.concatenate(frame_values),
        x=np.concatenate(x_values),
        y=np.concatenate(y_values),
        angle_deg=np.concatenate(angle_values),
        intensity=np.concatenate(intensity_values),
        background=np.concatenate(background_values),
        sigma=np.concatenate(sigma_values),
        thresholds=thresholds,
        mode="pixels",
    )


def collect_peak_pixels(
    frames: list[FrameRecord],
    frame_stats: dict[int, FrameStats],
    params: CRED2Parameters,
    center_x: float,
    center_y: float,
    peak_threshold: float,
    peak_filter_size: int,
    center_mask_radius: float,
    close_point_radius: float,
    mask_edge_guard_px: float | None,
    max_spots_per_frame: int,
    max_frames: int | None = None,
    output_file: str | Path | None = None,
    exclusion_mask: np.ndarray | None = None,
    peak_footprint_shape: str = "circle",
) -> SparsePixels:
    frame_values: list[np.ndarray] = []
    x_values: list[np.ndarray] = []
    y_values: list[np.ndarray] = []
    angle_values: list[np.ndarray] = []
    intensity_values: list[np.ndarray] = []
    background_values: list[np.ndarray] = []
    sigma_values: list[np.ndarray] = []
    thresholds: dict[int, float] = {}
    output_handle = None
    if output_file is not None:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_handle = output_path.open("w", encoding="utf-8")

    try:
        used = 0
        for record in frames:
            if max_frames is not None and used >= max_frames:
                break
            stats = frame_stats[record.frame_number]
            if stats.is_zero:
                continue

            array = load_image(record.path)
            points, background, sigma = extract_diffraction_points(
                array,
                threshold=peak_threshold,
                filter_size=peak_filter_size,
                center_x=center_x,
                center_y=center_y,
                center_mask_radius=center_mask_radius,
                min_distance_px=close_point_radius,
                max_spots=max_spots_per_frame,
                exclusion_mask=exclusion_mask,
                mask_edge_guard_px=mask_edge_guard_px,
                footprint_shape=peak_footprint_shape,
            )
            thresholds[record.frame_number] = float(peak_threshold)
            if output_handle is not None:
                output_handle.write(f"{Path(record.path).name}\n")
                for (x, y), intensity in points:
                    output_handle.write(f"x = {x:.3f}, y = {y:.3f}, intensity = {intensity:.6g}\n")
            if not points:
                used += 1
                continue

            coords = np.asarray([point for point, _ in points], dtype=np.float32)
            intensities = np.asarray([intensity for _, intensity in points], dtype=np.float32)
            n = intensities.size
            angle = params.angle_for_frame(record.frame_number)
            frame_values.append(np.full(n, record.frame_number, dtype=np.int32))
            x_values.append(coords[:, 0])
            y_values.append(coords[:, 1])
            angle_values.append(np.full(n, angle, dtype=np.float32))
            intensity_values.append(intensities)
            background_values.append(np.full(n, background, dtype=np.float32))
            sigma_values.append(np.full(n, sigma, dtype=np.float32))
            used += 1
    finally:
        if output_handle is not None:
            output_handle.close()

    if not intensity_values:
        empty_i = np.array([], dtype=np.int32)
        empty_f = np.array([], dtype=np.float32)
        return SparsePixels(empty_i, empty_f, empty_f, empty_f, empty_f, empty_f, empty_f, thresholds, mode="peaks")

    return SparsePixels(
        frame=np.concatenate(frame_values),
        x=np.concatenate(x_values),
        y=np.concatenate(y_values),
        angle_deg=np.concatenate(angle_values),
        intensity=np.concatenate(intensity_values),
        background=np.concatenate(background_values),
        sigma=np.concatenate(sigma_values),
        thresholds=thresholds,
        mode="peaks",
    )


def collect_peak_pixels_with_stats(
    frames: list[FrameRecord],
    params: CRED2Parameters,
    center_x: float,
    center_y: float,
    peak_threshold: float,
    peak_filter_size: int,
    center_mask_radius: float,
    close_point_radius: float,
    mask_edge_guard_px: float | None,
    max_spots_per_frame: int,
    max_frames: int | None = None,
    output_file: str | Path | None = None,
    exclusion_mask: np.ndarray | None = None,
    peak_footprint_shape: str = "circle",
    frame_workers: int = DEFAULT_FRAME_WORKERS,
    preloaded_images: dict[int, np.ndarray] | None = None,
) -> tuple[SparsePixels, list[FrameStats]]:
    """Inspect and peak-pick every TIFF in one ordered, parallel read pass."""

    def process(record: FrameRecord) -> _PeakFrameResult:
        array = (
            preloaded_images[record.frame_number]
            if preloaded_images is not None and record.frame_number in preloaded_images
            else load_image(record.path)
        )
        stats = inspect_image(record, array)
        if stats.is_zero:
            return _PeakFrameResult(record, stats, [], 0.0, 1.0)
        points, background, sigma = extract_diffraction_points(
            array,
            threshold=peak_threshold,
            filter_size=peak_filter_size,
            center_x=center_x,
            center_y=center_y,
            center_mask_radius=center_mask_radius,
            min_distance_px=close_point_radius,
            max_spots=max_spots_per_frame,
            exclusion_mask=exclusion_mask,
            mask_edge_guard_px=mask_edge_guard_px,
            footprint_shape=peak_footprint_shape,
        )
        return _PeakFrameResult(record, stats, points, background, sigma)

    results = _ordered_parallel_map(frames, process, frame_workers)
    stats = [result.stats for result in results]
    frame_values: list[np.ndarray] = []
    x_values: list[np.ndarray] = []
    y_values: list[np.ndarray] = []
    angle_values: list[np.ndarray] = []
    intensity_values: list[np.ndarray] = []
    background_values: list[np.ndarray] = []
    sigma_values: list[np.ndarray] = []
    thresholds: dict[int, float] = {}

    output_handle = None
    if output_file is not None:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_handle = output_path.open("w", encoding="utf-8")
    try:
        used = 0
        for result in results:
            if result.stats.is_zero:
                continue
            if max_frames is not None and used >= max_frames:
                continue
            record = result.record
            thresholds[record.frame_number] = float(peak_threshold)
            if output_handle is not None:
                output_handle.write(f"{Path(record.path).name}\n")
                for (x, y), intensity in result.points:
                    output_handle.write(f"x = {x:.3f}, y = {y:.3f}, intensity = {intensity:.6g}\n")
            if result.points:
                coords = np.asarray([point for point, _ in result.points], dtype=np.float32)
                intensities = np.asarray([intensity for _, intensity in result.points], dtype=np.float32)
                n = intensities.size
                angle = params.angle_for_frame(record.frame_number)
                frame_values.append(np.full(n, record.frame_number, dtype=np.int32))
                x_values.append(coords[:, 0])
                y_values.append(coords[:, 1])
                angle_values.append(np.full(n, angle, dtype=np.float32))
                intensity_values.append(intensities)
                background_values.append(np.full(n, result.background, dtype=np.float32))
                sigma_values.append(np.full(n, result.sigma, dtype=np.float32))
            used += 1
    finally:
        if output_handle is not None:
            output_handle.close()

    if not intensity_values:
        empty_i = np.array([], dtype=np.int32)
        empty_f = np.array([], dtype=np.float32)
        sparse = SparsePixels(
            empty_i,
            empty_f,
            empty_f,
            empty_f,
            empty_f,
            empty_f,
            empty_f,
            thresholds,
            mode="peaks",
        )
    else:
        sparse = SparsePixels(
            frame=np.concatenate(frame_values),
            x=np.concatenate(x_values),
            y=np.concatenate(y_values),
            angle_deg=np.concatenate(angle_values),
            intensity=np.concatenate(intensity_values),
            background=np.concatenate(background_values),
            sigma=np.concatenate(sigma_values),
            thresholds=thresholds,
            mode="peaks",
        )
    return sparse, stats


def collect_sparse_pixels_with_stats(
    frames: list[FrameRecord],
    params: CRED2Parameters,
    center_x: float,
    center_y: float,
    threshold_sigma: float,
    min_intensity: float,
    center_mask_radius: float,
    max_pixels_per_frame: int,
    max_frames: int | None = None,
    exclusion_mask: np.ndarray | None = None,
    frame_workers: int = DEFAULT_FRAME_WORKERS,
    preloaded_images: dict[int, np.ndarray] | None = None,
) -> tuple[SparsePixels, list[FrameStats]]:
    """Inspect and threshold every TIFF in one ordered, parallel read pass."""

    def process(record: FrameRecord) -> _SparseFrameResult:
        array = (
            preloaded_images[record.frame_number]
            if preloaded_images is not None and record.frame_number in preloaded_images
            else load_image(record.path)
        )
        stats = inspect_image(record, array)
        empty = np.array([], dtype=np.float32)
        if stats.is_zero:
            return _SparseFrameResult(record, stats, empty, empty, empty, 0.0, 1.0, 0.0)
        mask, background, sigma, threshold = significant_pixel_mask(
            array,
            threshold_sigma=threshold_sigma,
            min_intensity=min_intensity,
            center_x=center_x,
            center_y=center_y,
            center_mask_radius=center_mask_radius,
            exclusion_mask=exclusion_mask,
        )
        yy, xx = np.nonzero(mask)
        intensities = array[yy, xx].astype(np.float32)
        if max_pixels_per_frame > 0 and intensities.size > max_pixels_per_frame:
            keep = np.argpartition(intensities, -max_pixels_per_frame)[-max_pixels_per_frame:]
            xx = xx[keep]
            yy = yy[keep]
            intensities = intensities[keep]
        return _SparseFrameResult(
            record,
            stats,
            xx.astype(np.float32),
            yy.astype(np.float32),
            intensities,
            background,
            sigma,
            threshold,
        )

    results = _ordered_parallel_map(frames, process, frame_workers)
    stats = [result.stats for result in results]
    frame_values: list[np.ndarray] = []
    x_values: list[np.ndarray] = []
    y_values: list[np.ndarray] = []
    angle_values: list[np.ndarray] = []
    intensity_values: list[np.ndarray] = []
    background_values: list[np.ndarray] = []
    sigma_values: list[np.ndarray] = []
    thresholds: dict[int, float] = {}

    used = 0
    for result in results:
        if result.stats.is_zero:
            continue
        if max_frames is not None and used >= max_frames:
            continue
        record = result.record
        thresholds[record.frame_number] = float(result.threshold)
        if result.intensity.size:
            n = result.intensity.size
            angle = params.angle_for_frame(record.frame_number)
            frame_values.append(np.full(n, record.frame_number, dtype=np.int32))
            x_values.append(result.x)
            y_values.append(result.y)
            angle_values.append(np.full(n, angle, dtype=np.float32))
            intensity_values.append(result.intensity)
            background_values.append(np.full(n, result.background, dtype=np.float32))
            sigma_values.append(np.full(n, result.sigma, dtype=np.float32))
        used += 1

    if not intensity_values:
        empty_i = np.array([], dtype=np.int32)
        empty_f = np.array([], dtype=np.float32)
        sparse = SparsePixels(empty_i, empty_f, empty_f, empty_f, empty_f, empty_f, empty_f, thresholds)
    else:
        sparse = SparsePixels(
            frame=np.concatenate(frame_values),
            x=np.concatenate(x_values),
            y=np.concatenate(y_values),
            angle_deg=np.concatenate(angle_values),
            intensity=np.concatenate(intensity_values),
            background=np.concatenate(background_values),
            sigma=np.concatenate(sigma_values),
            thresholds=thresholds,
            mode="pixels",
        )
    return sparse, stats


def compute_observations(sparse: SparsePixels, geometry: GeometryConfig) -> ObservationData:
    q = pixel_to_q_sample(sparse.x, sparse.y, sparse.angle_deg, geometry)
    return ObservationData(
        frame=sparse.frame,
        x=sparse.x,
        y=sparse.y,
        q=q.astype(np.float32),
        intensity=sparse.intensity,
        background=sparse.background,
        sigma=sparse.sigma,
    )
