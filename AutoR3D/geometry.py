from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np

from .cred2 import CRED2Parameters


@dataclass(frozen=True)
class GeometryConfig:
    center_x: float
    center_y: float
    detector_distance_mm: float
    physical_pixel_mm: float
    wavelength_angstrom: float
    rotation_axis_deg: float
    rotation_axis_vector: tuple[float, float, float]
    rotation_sign: float = -1.0
    native_detector: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def axis_vector_from_angle(angle_deg: float) -> np.ndarray:
    angle = math.radians(angle_deg)
    vector = np.array([math.cos(angle), math.sin(angle), 0.0], dtype=np.float64)
    norm = np.linalg.norm(vector)
    if norm == 0:
        raise ValueError(f"Invalid rotation axis angle: {angle_deg}")
    return vector / norm


def build_geometry(
    params: CRED2Parameters,
    detector_shape: tuple[int, int],
    rotation_axis_deg: float,
    center_x: float | None = None,
    center_y: float | None = None,
    rotation_sign: float = -1.0,
    detector_distance_mm: float | None = None,
    wavelength_angstrom: float | None = None,
) -> GeometryConfig:
    height, width = detector_shape
    cx = float(width / 2.0 if center_x is None else center_x)
    cy = float(height / 2.0 if center_y is None else center_y)
    axis = axis_vector_from_angle(rotation_axis_deg)
    return GeometryConfig(
        center_x=cx,
        center_y=cy,
        detector_distance_mm=float(
            params.detector_distance_mm if detector_distance_mm is None else detector_distance_mm
        ),
        physical_pixel_mm=float(params.physical_pixel_mm),
        wavelength_angstrom=float(
            params.wavelength_angstrom if wavelength_angstrom is None else wavelength_angstrom
        ),
        rotation_axis_deg=float(rotation_axis_deg),
        rotation_axis_vector=tuple(float(v) for v in axis),
        rotation_sign=float(rotation_sign),
    )


def pixel_to_q_lab(x: np.ndarray, y: np.ndarray, geometry: GeometryConfig) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if geometry.native_detector is not None:
        model = geometry.native_detector
        # DIALS coordinates measure pixels from the corner, NumPy from centres.
        rays = (np.asarray(model["detector_origin_mm"])[None, :]
                + (x + .5)[:, None] * model["pixel_size_mm"][0] * np.asarray(model["detector_fast_axis"])
                + (y + .5)[:, None] * model["pixel_size_mm"][1] * np.asarray(model["detector_slow_axis"]))
        return rays / np.linalg.norm(rays, axis=1)[:, None] / geometry.wavelength_angstrom - np.asarray(model["beam_s0"])
    dx = (x - geometry.center_x) * geometry.physical_pixel_mm
    dy = (y - geometry.center_y) * geometry.physical_pixel_mm
    dz = np.full_like(dx, geometry.detector_distance_mm)
    rays = np.stack([dx, dy, dz], axis=1)
    rays /= np.linalg.norm(rays, axis=1)[:, None]
    k_scale = 1.0 / geometry.wavelength_angstrom
    k_out = rays * k_scale
    k_in = np.array([0.0, 0.0, k_scale], dtype=np.float64)
    return k_out - k_in


def rotate_vectors(vectors: np.ndarray, axis: np.ndarray, angles_deg: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float64)
    angles = np.radians(np.asarray(angles_deg, dtype=np.float64))
    axis = np.asarray(axis, dtype=np.float64)
    axis = axis / np.linalg.norm(axis)

    cos_a = np.cos(angles)[:, None]
    sin_a = np.sin(angles)[:, None]
    cross = np.cross(np.broadcast_to(axis, vectors.shape), vectors)
    dot = np.sum(vectors * axis[None, :], axis=1)[:, None]
    return vectors * cos_a + cross * sin_a + axis[None, :] * dot * (1.0 - cos_a)


def pixel_to_q_sample(
    x: np.ndarray,
    y: np.ndarray,
    frame_angles_deg: np.ndarray,
    geometry: GeometryConfig,
) -> np.ndarray:
    q_lab = pixel_to_q_lab(x, y, geometry)
    if geometry.native_detector is not None:
        model = geometry.native_detector
        setting = np.asarray(model["setting_rotation"]).reshape(3, 3)
        fixed = np.asarray(model["fixed_rotation"]).reshape(3, 3)
        q = q_lab @ np.linalg.inv(setting).T
        q = rotate_vectors(q, np.asarray(model["rotation_axis_vector"]), -np.asarray(frame_angles_deg))
        return q @ np.linalg.inv(fixed).T
    axis = np.array(geometry.rotation_axis_vector, dtype=np.float64)
    sample_angles = geometry.rotation_sign * np.asarray(frame_angles_deg, dtype=np.float64)
    return rotate_vectors(q_lab, axis, sample_angles)
