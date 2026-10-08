from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .cred2 import CRED2Parameters
from .geometry import build_geometry
from .observations import SparsePixels, compute_observations
from .reconstruct import VolumeData, grid_observations


MAP_SPHERICAL_PROJECTION_SCORE_METHOD = "map_spherical_direction_concentration"
AXIS_WEAK_INTENSITY_DROP_FRACTION = 0.10


@dataclass(frozen=True)
class AxisScore:
    rotation_axis_deg: float
    focus_score: float
    lattice_score: float
    projection_score: float
    combined_score: float
    occupied_voxels: int
    sampled_observations: int
    stage: str

    @property
    def score(self) -> float:
        return self.combined_score

    def to_dict(self) -> dict:
        return {
            "rotation_axis_deg": self.rotation_axis_deg,
            "score": self.combined_score,
            "combined_score": self.combined_score,
            "focus_score": self.focus_score,
            "lattice_score": self.lattice_score,
            "projection_score": self.projection_score,
            "occupied_voxels": self.occupied_voxels,
            "sampled_observations": self.sampled_observations,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class AxisRefinementResult:
    enabled: bool
    base_axis_deg: float
    chosen_axis_deg: float
    score_method: str
    range_deg: float
    coarse_step_deg: float
    fine_step_deg: float
    fine_range_deg: float
    scores: list[AxisScore]

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "base_axis_deg": self.base_axis_deg,
            "chosen_rotation_axis_deg": self.chosen_axis_deg,
            "score_method": self.score_method,
            "range_deg": self.range_deg,
            "coarse_step_deg": self.coarse_step_deg,
            "fine_step_deg": self.fine_step_deg,
            "fine_range_deg": self.fine_range_deg,
            "scores": [score.to_dict() for score in self.scores],
        }


def parse_axis_candidates(text: str | None) -> list[float] | None:
    if not text:
        return None
    values: list[float] = []
    for item in text.split(","):
        item = item.strip()
        if item:
            values.append(float(item))
    return values


def _dedupe_axes(values: list[float]) -> list[float]:
    deduped: list[float] = []
    seen: set[float] = set()
    for value in values:
        key = round(float(value), 6)
        if key not in seen:
            seen.add(key)
            deduped.append(float(value))
    return deduped


def _axis_grid(center: float, half_range: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("Axis refinement step must be positive.")
    count = int(np.floor((2.0 * half_range) / step + 0.5))
    values = [center - half_range + i * step for i in range(count + 1)]
    values.append(center)
    return _dedupe_axes([round(value, 10) for value in values])


def global_axis_grid(step_deg: float = 10.0, range_deg: float = 360.0) -> list[float]:
    if step_deg <= 0:
        raise ValueError("Global axis search step must be positive.")
    count = int(np.floor(float(range_deg) / float(step_deg)))
    return [round(index * float(step_deg), 10) for index in range(count)]


def _sample_sparse_pixels(sparse: SparsePixels, max_observations: int) -> np.ndarray:
    n = sparse.size
    if n <= max_observations:
        return np.arange(n)
    return np.argpartition(sparse.intensity, -max_observations)[-max_observations:]


def _normalize(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values
    low = float(np.min(values))
    high = float(np.max(values))
    if high - low <= 1e-12:
        return np.zeros_like(values, dtype=np.float64)
    return (values - low) / (high - low)


def points_to_unit_vectors(
    points: np.ndarray,
    center: np.ndarray | None = None,
    min_radius: float = 1e-12,
) -> np.ndarray:
    """Project 3D points onto the unit sphere."""

    points = np.asarray(points, dtype=float)

    if center is None:
        center = np.zeros(3)
    else:
        center = np.asarray(center, dtype=float)

    shifted = points - center
    r = np.linalg.norm(shifted, axis=1)

    mask = r > min_radius
    shifted = shifted[mask]
    r = r[mask]

    dirs = shifted / r[:, None]
    return dirs


def spherical_angles(
    points: np.ndarray,
    center: np.ndarray | None = None,
    min_radius: float = 1e-12,
) -> tuple[np.ndarray, np.ndarray]:
    """Return theta and phi after spherical projection.

    theta: azimuth angle [-pi, pi]
    phi: elevation angle [-pi/2, pi/2]
    """

    dirs = points_to_unit_vectors(
        points,
        center=center,
        min_radius=min_radius,
    )

    x, y, z = dirs[:, 0], dirs[:, 1], dirs[:, 2]

    theta = np.arctan2(y, x)
    phi = np.arcsin(np.clip(z, -1.0, 1.0))

    return theta, phi


def q_to_spherical_angles(
    q: np.ndarray,
    center: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert q vectors to spherical angular coordinates in degrees.

    Returns azimuth, elevation, and zenith angles. The projection used for
    scoring and plotting is azimuth/elevation; zenith is kept for reporting
    and is equivalent to 90 - elevation.
    """

    theta, phi = spherical_angles(q, center=center)
    if theta.size == 0:
        return (
            np.empty((0,), dtype=np.float64),
            np.empty((0,), dtype=np.float64),
            np.empty((0,), dtype=np.float64),
        )
    azimuth = np.degrees(theta)
    elevation = np.degrees(phi)
    zenith = 90.0 - elevation
    return azimuth, elevation, zenith


def _canonicalize_antipodal_directions(dirs: np.ndarray) -> np.ndarray:
    dirs = np.asarray(dirs, dtype=np.float64).copy()
    if dirs.size == 0:
        return dirs
    eps = 1e-12
    flip = (dirs[:, 2] < -eps) | (
        (np.abs(dirs[:, 2]) <= eps)
        & ((dirs[:, 1] < -eps) | ((np.abs(dirs[:, 1]) <= eps) & (dirs[:, 0] < 0)))
    )
    dirs[flip] *= -1.0
    return dirs


def merge_directions(
    dirs: np.ndarray,
    merge_deg: float = 0.6,
    antipodal: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Merge close directions using a fast theta/phi binning approximation."""

    dirs = np.asarray(dirs, dtype=float)
    if dirs.ndim != 2 or dirs.shape[1] != 3 or dirs.shape[0] == 0:
        return np.empty((0, 3), dtype=np.float64), np.empty((0,), dtype=np.int32)
    norms = np.linalg.norm(dirs, axis=1)
    valid = norms > 1e-12
    dirs = dirs[valid] / norms[valid, None]
    if dirs.shape[0] == 0:
        return np.empty((0, 3), dtype=np.float64), np.empty((0,), dtype=np.int32)
    if antipodal:
        dirs = _canonicalize_antipodal_directions(dirs)

    theta = np.arctan2(dirs[:, 1], dirs[:, 0])
    phi = np.arcsin(np.clip(dirs[:, 2], -1.0, 1.0))
    bin_rad = max(np.deg2rad(merge_deg), 1e-12)
    theta_bin = np.floor((theta + np.pi) / bin_rad).astype(np.int64)
    phi_bin = np.floor((phi + np.pi / 2.0) / bin_rad).astype(np.int64)
    keys = np.column_stack([theta_bin, phi_bin])
    unique_keys, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)

    sums = np.zeros((unique_keys.shape[0], 3), dtype=np.float64)
    np.add.at(sums, inverse, dirs)
    center_norms = np.linalg.norm(sums, axis=1)
    nonzero = center_norms > 1e-12
    centers = sums[nonzero] / center_norms[nonzero, None]
    counts = counts[nonzero]
    return centers, counts.astype(np.int32)


def direction_repeat_score(
    dirs: np.ndarray,
    merge_deg: float = 0.6,
    antipodal: bool = True,
) -> dict:
    """Direction repeat/concentration score; higher means more points share directions."""

    centers, counts = merge_directions(dirs, merge_deg=merge_deg, antipodal=antipodal)
    n_total = int(np.sum(counts))
    if n_total == 0 or counts.size == 0:
        return {
            "score": 0.0,
            "repeat_fraction": 0.0,
            "concentration": 0.0,
            "n_total": int(n_total),
            "n_unique": 0,
            "max_multiplicity": 0,
            "mean_multiplicity": 0.0,
        }

    repeat_fraction = 1.0 - len(centers) / n_total
    probabilities = counts.astype(np.float64) / float(n_total)
    hhi = float(np.sum(probabilities * probabilities))
    if n_total > 1:
        concentration = (hhi - 1.0 / n_total) / (1.0 - 1.0 / n_total)
    else:
        concentration = 1.0
    concentration = float(np.clip(concentration, 0.0, 1.0))
    score = float(np.clip(0.7 * repeat_fraction + 0.3 * concentration, 0.0, 1.0))
    return {
        "score": score,
        "repeat_fraction": float(repeat_fraction),
        "concentration": concentration,
        "n_total": int(n_total),
        "n_unique": int(len(centers)),
        "max_multiplicity": int(np.max(counts)),
        "mean_multiplicity": float(np.mean(counts)),
    }


def spherical_lattice_order_index(
    points: np.ndarray,
    center: np.ndarray | None = None,
    min_radius: float = 1e-12,
    duplicate_deg: float = 0.6,
    band_deg: float = 2.0,
    antipodal: bool = True,
) -> dict:
    """Compatibility wrapper for the direction repeat/concentration score."""

    dirs = points_to_unit_vectors(points, center=center, min_radius=min_radius)

    repeat = direction_repeat_score(
        dirs,
        merge_deg=duplicate_deg,
        antipodal=antipodal,
    )
    repeat_score = float(repeat["score"])
    return {
        "SLOI": repeat_score,
        "direction_concentration_score": repeat_score,
        "direction_repeat_score": repeat_score,
        "great_circle_score": 0.0,
        "repeat_details": repeat,
        "hough_details": {},
        "n_projected_dirs": int(len(dirs)),
    }


def projection_variance_score(q: np.ndarray) -> float:
    """Compatibility wrapper for the direction-concentration map score."""

    q = np.asarray(q, dtype=np.float64)
    if q.ndim != 2 or q.shape[1] != 3 or q.shape[0] < 4:
        return 0.0
    return float(spherical_lattice_order_index(q)["SLOI"])


def reconstruct_map_for_axis(
    sparse: SparsePixels,
    params: CRED2Parameters,
    detector_shape: tuple[int, int],
    rotation_axis_deg: float,
    center_x: float,
    center_y: float,
    voxel_size: float,
    rotation_sign: float,
    gridding: str = "trilinear",
    max_observations: int = 80000,
) -> VolumeData:
    if sparse.size == 0:
        raise ValueError("Cannot reconstruct an axis map without sparse observations.")
    keep = _sample_sparse_pixels(sparse, max_observations)
    sparse_sample = SparsePixels(
        frame=sparse.frame[keep],
        x=sparse.x[keep],
        y=sparse.y[keep],
        angle_deg=sparse.angle_deg[keep],
        intensity=sparse.intensity[keep],
        background=sparse.background[keep],
        sigma=sparse.sigma[keep],
        thresholds=sparse.thresholds,
        mode=sparse.mode,
    )
    geometry = build_geometry(
        params,
        detector_shape=detector_shape,
        rotation_axis_deg=rotation_axis_deg,
        center_x=center_x,
        center_y=center_y,
        rotation_sign=rotation_sign,
    )
    observations = compute_observations(sparse_sample, geometry)
    return grid_observations(observations, voxel_size=voxel_size, method=gridding)


def volume_projection_points(
    volume: VolumeData,
    weak_intensity_drop_fraction: float = AXIS_WEAK_INTENSITY_DROP_FRACTION,
) -> tuple[np.ndarray, np.ndarray]:
    mask = volume.hit_count > 0
    if not np.any(mask):
        return np.empty((0, 3), dtype=np.float64), np.empty((0,), dtype=np.float64)
    ix, iy, iz = np.nonzero(mask)
    weights = volume.i_mean[ix, iy, iz].astype(np.float64)
    positive = weights > 0
    ix = ix[positive]
    iy = iy[positive]
    iz = iz[positive]
    weights = weights[positive]
    if weights.size == 0:
        return np.empty((0, 3), dtype=np.float64), np.empty((0,), dtype=np.float64)
    if 0.0 < weak_intensity_drop_fraction < 1.0 and weights.size > 1:
        cutoff = float(np.quantile(weights, weak_intensity_drop_fraction))
        strong = weights > cutoff
        if np.any(strong):
            ix = ix[strong]
            iy = iy[strong]
            iz = iz[strong]
            weights = weights[strong]
    q = np.column_stack(
        [
            volume.qx[ix],
            volume.qy[iy],
            volume.qz[iz],
        ]
    ).astype(np.float64)
    return q, weights


def score_projection_axis_with_map(
    sparse: SparsePixels,
    params: CRED2Parameters,
    detector_shape: tuple[int, int],
    rotation_axis_deg: float,
    center_x: float,
    center_y: float,
    voxel_size: float,
    rotation_sign: float,
    stage: str = "single",
    max_observations: int = 80000,
    gridding: str = "trilinear",
) -> tuple[AxisScore, np.ndarray, np.ndarray, VolumeData]:
    """Reconstruct a 3D map for one axis, then score its spherical projection."""

    volume = reconstruct_map_for_axis(
        sparse,
        params=params,
        detector_shape=detector_shape,
        rotation_axis_deg=rotation_axis_deg,
        center_x=center_x,
        center_y=center_y,
        voxel_size=voxel_size,
        rotation_sign=rotation_sign,
        gridding=gridding,
        max_observations=max_observations,
    )
    q, weights = volume_projection_points(volume)
    if q.size == 0 or np.sum(weights) <= 0:
        projection = 0.0
        repeat = 0.0
        combined = 0.0
    else:
        sloi = spherical_lattice_order_index(q)
        projection = float(sloi["SLOI"])
        repeat = float(sloi["direction_repeat_score"])
        combined = float(np.clip(projection, 0.0, 1.0))
    score = AxisScore(
        rotation_axis_deg=float(rotation_axis_deg),
        focus_score=repeat,
        lattice_score=0.0,
        projection_score=float(projection),
        combined_score=combined,
        occupied_voxels=int(np.count_nonzero(volume.hit_count)),
        sampled_observations=int(weights.size),
        stage=stage,
    )
    return score, q, weights, volume


def score_projection_axis(
    sparse: SparsePixels,
    params: CRED2Parameters,
    detector_shape: tuple[int, int],
    rotation_axis_deg: float,
    center_x: float,
    center_y: float,
    voxel_size: float,
    rotation_sign: float,
    stage: str = "single",
    max_observations: int = 80000,
    gridding: str = "trilinear",
) -> AxisScore:
    score, _q, _weights, _volume = score_projection_axis_with_map(
        sparse,
        params=params,
        detector_shape=detector_shape,
        rotation_axis_deg=rotation_axis_deg,
        center_x=center_x,
        center_y=center_y,
        voxel_size=voxel_size,
        rotation_sign=rotation_sign,
        stage=stage,
        max_observations=max_observations,
        gridding=gridding,
    )
    return score


def _score_axes(
    axes: list[float],
    sparse: SparsePixels,
    params: CRED2Parameters,
    detector_shape: tuple[int, int],
    center_x: float,
    center_y: float,
    voxel_size: float,
    rotation_sign: float,
    stage: str,
    max_observations: int,
) -> list[AxisScore]:
    raw = [
        score_projection_axis(
            sparse,
            params=params,
            detector_shape=detector_shape,
            rotation_axis_deg=axis,
            center_x=center_x,
            center_y=center_y,
            voxel_size=voxel_size,
            rotation_sign=rotation_sign,
            stage=stage,
            max_observations=max_observations,
        )
        for axis in _dedupe_axes(axes)
    ]
    return _renormalize_scores(raw)


def _renormalize_scores(scores: list[AxisScore]) -> list[AxisScore]:
    projection_norm = _normalize(np.asarray([score.projection_score for score in scores], dtype=np.float64))
    normalized: list[AxisScore] = []
    for score, projection in zip(scores, projection_norm):
        normalized.append(
            AxisScore(
                rotation_axis_deg=score.rotation_axis_deg,
                focus_score=score.focus_score,
                lattice_score=score.lattice_score,
                projection_score=score.projection_score,
                combined_score=float(projection),
                occupied_voxels=score.occupied_voxels,
                sampled_observations=score.sampled_observations,
                stage=score.stage,
            )
        )
    return normalized


def _best_projection_score(scores: list[AxisScore], base_axis_deg: float) -> AxisScore:
    return max(
        scores,
        key=lambda score: (
            score.combined_score,
            -abs(score.rotation_axis_deg - base_axis_deg),
        ),
    )


def refine_rotation_axis(
    sparse: SparsePixels,
    params: CRED2Parameters,
    detector_shape: tuple[int, int],
    base_axis_deg: float,
    center_x: float,
    center_y: float,
    voxel_size: float,
    rotation_sign: float,
    refine_enabled: bool,
    explicit_candidates: list[float] | None = None,
    refine_range_deg: float = 3.0,
    coarse_step_deg: float = 0.25,
    fine_step_deg: float = 0.05,
    fine_range_deg: float = 0.25,
    max_observations: int = 80000,
) -> AxisRefinementResult:
    if not refine_enabled:
        score = AxisScore(
            rotation_axis_deg=float(base_axis_deg),
            focus_score=0.0,
            lattice_score=0.0,
            projection_score=0.0,
            combined_score=0.0,
            occupied_voxels=0,
            sampled_observations=min(sparse.size, max_observations),
            stage="fixed",
        )
        return AxisRefinementResult(
            enabled=False,
            base_axis_deg=float(base_axis_deg),
            chosen_axis_deg=float(base_axis_deg),
            score_method="disabled_fixed_axis",
            range_deg=float(refine_range_deg),
            coarse_step_deg=float(coarse_step_deg),
            fine_step_deg=float(fine_step_deg),
            fine_range_deg=float(fine_range_deg),
            scores=[score],
        )

    coarse_axes = _axis_grid(float(base_axis_deg), float(refine_range_deg), float(coarse_step_deg))
    if explicit_candidates:
        coarse_axes = _dedupe_axes(coarse_axes + [float(axis) for axis in explicit_candidates])
    coarse_scores = _score_axes(
        coarse_axes,
        sparse=sparse,
        params=params,
        detector_shape=detector_shape,
        center_x=center_x,
        center_y=center_y,
        voxel_size=voxel_size,
        rotation_sign=rotation_sign,
        stage="coarse",
        max_observations=max_observations,
    )
    coarse_best = _best_projection_score(coarse_scores, float(base_axis_deg))
    fine_axes = _axis_grid(coarse_best.rotation_axis_deg, float(fine_range_deg), float(fine_step_deg))
    fine_scores = _score_axes(
        fine_axes,
        sparse=sparse,
        params=params,
        detector_shape=detector_shape,
        center_x=center_x,
        center_y=center_y,
        voxel_size=voxel_size,
        rotation_sign=rotation_sign,
        stage="fine",
        max_observations=max_observations,
    )
    all_scores = _renormalize_scores(coarse_scores + fine_scores)
    fine_axes_set = {round(axis, 6) for axis in fine_axes}
    final_pool = [
        score for score in all_scores if score.stage == "fine" or round(score.rotation_axis_deg, 6) in fine_axes_set
    ]
    fine_best = _best_projection_score(final_pool, float(base_axis_deg))
    return AxisRefinementResult(
        enabled=True,
        base_axis_deg=float(base_axis_deg),
        chosen_axis_deg=float(fine_best.rotation_axis_deg),
        score_method=MAP_SPHERICAL_PROJECTION_SCORE_METHOD,
        range_deg=float(refine_range_deg),
        coarse_step_deg=float(coarse_step_deg),
        fine_step_deg=float(fine_step_deg),
        fine_range_deg=float(fine_range_deg),
        scores=all_scores,
    )
