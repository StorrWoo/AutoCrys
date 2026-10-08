from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re

import numpy as np
from PIL import Image
from scipy.ndimage import maximum_filter


_FRAME_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class FrameRecord:
    frame_number: int
    path: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FrameStats:
    frame_number: int
    path: str
    shape: tuple[int, int]
    dtype: str
    total_intensity: float
    max_intensity: float
    mean_intensity: float
    nonzero_pixels: int
    is_zero: bool

    def to_dict(self) -> dict:
        data = asdict(self)
        data["shape"] = list(self.shape)
        return data


def _frame_number(path: Path) -> int:
    matches = _FRAME_RE.findall(path.stem)
    if not matches:
        raise ValueError(f"Cannot find frame number in {path.name}")
    return int(matches[-1])


def discover_frames(dataset_dir: Path, frames_dir: str = "diff", pattern: str = "frame_*.tif") -> list[FrameRecord]:
    folder = dataset_dir / frames_dir
    if not folder.exists():
        raise FileNotFoundError(f"Frame directory does not exist: {folder}")
    frames = [FrameRecord(_frame_number(path), str(path)) for path in folder.glob(pattern)]
    frames.sort(key=lambda item: item.frame_number)
    if not frames:
        raise FileNotFoundError(f"No frames matching {pattern!r} found in {folder}")
    return frames


def load_image(path: str | Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image)


def inspect_frame(record: FrameRecord) -> FrameStats:
    array = load_image(record.path)
    return inspect_image(record, array)


def inspect_image(record: FrameRecord, array: np.ndarray) -> FrameStats:
    """Build frame statistics from an image that is already in memory."""

    total = float(np.sum(array, dtype=np.float64))
    max_value = float(np.max(array)) if array.size else 0.0
    nonzero = int(np.count_nonzero(array))
    return FrameStats(
        frame_number=record.frame_number,
        path=record.path,
        shape=tuple(int(v) for v in array.shape),
        dtype=str(array.dtype),
        total_intensity=total,
        max_intensity=max_value,
        mean_intensity=float(total / array.size) if array.size else 0.0,
        nonzero_pixels=nonzero,
        is_zero=total == 0.0,
    )


def inspect_frames(frames: list[FrameRecord]) -> list[FrameStats]:
    return [inspect_frame(frame) for frame in frames]


def robust_background_sigma(array: np.ndarray) -> tuple[float, float]:
    values = array.astype(np.float32, copy=False)
    positive = values[values > 0]
    if positive.size == 0:
        return 0.0, 1.0
    background = float(np.percentile(positive, 50.0))
    mad = float(np.median(np.abs(positive - background)))
    sigma = max(1.4826 * mad, 1.0)
    return background, sigma


def significant_pixel_mask(
    array: np.ndarray,
    threshold_sigma: float,
    min_intensity: float,
    center_x: float,
    center_y: float,
    center_mask_radius: float,
    exclusion_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, float, float, float]:
    background, sigma = robust_background_sigma(array)
    threshold = max(float(min_intensity), background + threshold_sigma * sigma)
    mask = array.astype(np.float32, copy=False) >= threshold
    if center_mask_radius > 0:
        yy, xx = np.indices(array.shape)
        rr = np.sqrt((xx - center_x) ** 2 + (yy - center_y) ** 2)
        mask &= rr > center_mask_radius
    if exclusion_mask is not None:
        if exclusion_mask.shape != array.shape:
            raise ValueError(f"Exclusion mask shape {exclusion_mask.shape} does not match image shape {array.shape}")
        mask &= ~exclusion_mask
    return mask, background, sigma, threshold


def center_exclusion_mask(
    shape: tuple[int, int],
    center_x: float,
    center_y: float,
    radius: float,
) -> np.ndarray:
    if radius <= 0:
        return np.zeros(shape, dtype=bool)
    yy, xx = np.indices(shape)
    rr = np.sqrt((xx - center_x) ** 2 + (yy - center_y) ** 2)
    return rr <= float(radius)


def load_exclusion_mask(
    path: str | Path,
    detector_shape: tuple[int, int],
    threshold: float = 0.0,
    invert: bool = False,
) -> np.ndarray:
    mask_path = Path(path)
    suffix = mask_path.suffix.lower()
    if suffix == ".npy":
        array = np.load(mask_path)
    elif suffix == ".npz":
        with np.load(mask_path) as data:
            key = "mask" if "mask" in data.files else data.files[0]
            array = data[key]
    else:
        with Image.open(mask_path) as image:
            array = np.asarray(image)

    if array.ndim == 3:
        if array.shape[-1] == 4:
            array = array[..., 3]
        else:
            array = np.max(array[..., :3], axis=-1)
    if array.shape != detector_shape:
        raise ValueError(
            f"Exclusion mask shape {array.shape} does not match detector shape {detector_shape}"
        )

    if array.dtype == np.bool_:
        mask = array.astype(bool, copy=False)
    else:
        mask = array.astype(np.float32, copy=False) > float(threshold)
    return ~mask if invert else mask


def build_exclusion_mask(
    detector_shape: tuple[int, int],
    center_x: float,
    center_y: float,
    center_mask_radius: float,
    mask_file: str | Path | None = None,
    mask_threshold: float = 0.0,
    mask_invert: bool = False,
) -> np.ndarray | None:
    mask = center_exclusion_mask(detector_shape, center_x, center_y, center_mask_radius)
    if mask_file is not None:
        mask |= load_exclusion_mask(
            mask_file,
            detector_shape=detector_shape,
            threshold=mask_threshold,
            invert=mask_invert,
        )
    if not np.any(mask):
        return None
    return mask


def save_exclusion_mask_preview(mask: np.ndarray, path: str | Path) -> None:
    image = Image.fromarray(np.where(mask, 255, 0).astype(np.uint8))
    image.save(path)


def filter_close_points(
    points_with_intensity: list[tuple[tuple[float, float], float]],
    min_distance_px: float,
) -> list[tuple[tuple[float, float], float]]:
    """Keep the brightest point within each local neighborhood.

    The input coordinate convention is ((x, y), intensity).
    """

    if min_distance_px <= 0 or len(points_with_intensity) <= 1:
        return points_with_intensity
    ordered = sorted(points_with_intensity, key=lambda item: item[1], reverse=True)
    kept: list[tuple[tuple[float, float], float]] = []
    min_dist2 = float(min_distance_px) ** 2
    for point, intensity in ordered:
        x, y = point
        too_close = False
        for kept_point, _ in kept:
            kx, ky = kept_point
            if (x - kx) ** 2 + (y - ky) ** 2 < min_dist2:
                too_close = True
                break
        if not too_close:
            kept.append(((float(x), float(y)), float(intensity)))
    kept.sort(key=lambda item: (item[0][1], item[0][0]))
    return kept


def circular_footprint(radius_px: int) -> np.ndarray:
    radius = max(1, int(radius_px))
    yy, xx = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return (xx * xx + yy * yy) <= radius * radius


def circular_local_maxima(image: np.ndarray, radius_px: int) -> np.ndarray:
    """Return exact circular-neighborhood maxima using a fast candidate pass.

    A separable maximum filter over the largest square contained in the
    circle cheaply rejects almost every pixel.  The remaining candidates are
    checked against the exact circular footprint, preserving the result of a
    full footprint filter without paying its cost over the whole detector.
    """

    radius = max(1, int(radius_px))
    half_square = int(np.floor(radius / np.sqrt(2.0)))
    if half_square > 0:
        candidate_max = maximum_filter(
            image,
            size=2 * half_square + 1,
            mode="reflect",
        )
        candidate_mask = (image == candidate_max) & (image > 0)
    else:
        candidate_mask = image > 0

    candidates = np.column_stack(np.nonzero(candidate_mask))
    if candidates.size == 0:
        return candidates

    footprint = circular_footprint(radius)
    height, width = image.shape
    exact: list[tuple[int, int]] = []
    for y, x in candidates:
        y0 = max(0, int(y) - radius)
        y1 = min(height, int(y) + radius + 1)
        x0 = max(0, int(x) - radius)
        x1 = min(width, int(x) + radius + 1)
        fy0 = y0 - (int(y) - radius)
        fy1 = fy0 + (y1 - y0)
        fx0 = x0 - (int(x) - radius)
        fx1 = fx0 + (x1 - x0)
        local = image[y0:y1, x0:x1]
        local_footprint = footprint[fy0:fy1, fx0:fx1]
        if image[y, x] == np.max(local[local_footprint]):
            exact.append((int(y), int(x)))
    return np.asarray(exact, dtype=np.int64).reshape(-1, 2)


def extract_diffraction_points(
    image: np.ndarray,
    threshold: float,
    filter_size: int,
    center_x: float,
    center_y: float,
    center_mask_radius: float,
    min_distance_px: float,
    max_spots: int,
    exclusion_mask: np.ndarray | None = None,
    mask_edge_guard_px: float | None = None,
    footprint_shape: str = "circle",
) -> tuple[list[tuple[tuple[float, float], float]], float, float]:
    """Extract per-frame diffraction spot maxima.

    This follows the user's prototype: threshold, circular local maximum
    filtering, then close-point filtering. The central beam is masked before
    local maxima are selected.
    """

    img_array = image.astype(np.float32, copy=True)
    background, sigma = robust_background_sigma(img_array)
    img_array[img_array < threshold] = 0.0
    size = max(1, int(filter_size))
    if mask_edge_guard_px is None:
        mask_edge_guard_px = size / 2.0
    if center_mask_radius > 0:
        yy, xx = np.indices(img_array.shape)
        rr = np.sqrt((xx - center_x) ** 2 + (yy - center_y) ** 2)
        img_array[rr <= center_mask_radius + max(0.0, float(mask_edge_guard_px))] = 0.0
    if exclusion_mask is not None:
        if exclusion_mask.shape != img_array.shape:
            raise ValueError(
                f"Exclusion mask shape {exclusion_mask.shape} does not match image shape {img_array.shape}"
            )
        img_array[exclusion_mask] = 0.0
    if not np.any(img_array > 0):
        return [], background, sigma

    if footprint_shape == "circle":
        coordinates = circular_local_maxima(img_array, size)
    elif footprint_shape == "square":
        max_filtered = maximum_filter(img_array, size=size)
        coordinates = np.column_stack(np.nonzero((img_array == max_filtered) & (img_array > 0)))
    else:
        raise ValueError(f"Unknown peak footprint shape: {footprint_shape}")
    if coordinates.size == 0:
        return [], background, sigma
    intensities = img_array[coordinates[:, 0], coordinates[:, 1]]
    points = [
        ((float(coord[1]), float(coord[0])), float(intensity))
        for coord, intensity in zip(coordinates, intensities)
    ]
    points = filter_close_points(points, min_distance_px=min_distance_px)
    if max_spots > 0 and len(points) > max_spots:
        points = sorted(points, key=lambda item: item[1], reverse=True)[:max_spots]
        points.sort(key=lambda item: (item[0][1], item[0][0]))
    return points, background, sigma
