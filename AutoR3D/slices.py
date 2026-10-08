"""Indexed 2D slices of the AutoR3D point cloud.

The reconstruction produces lab-frame q vectors; ``XdsReference.index_q``
turns them into fractional hkl.  A slice is a thin slab around a lattice
plane (``hk0`` for l=0, ``h0l`` for k=0, ``0kl`` for h=0); observations
inside the slab are binned into a 2D intensity histogram and rendered as a
log-scaled PNG next to the raw binned array as NPZ.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import gaussian_filter


FAMILIES = ("hk0", "h0l", "0kl")
DEFAULT_SLICE_THICKNESS_RLU = 0.20
DEFAULT_SLICE_STEP_RLU = 0.05
MAX_SLICE_BINS_PER_AXIS = 1600
SLICE_HEADER_HEIGHT = 64
RECIPROCAL_AXIS_COLORS = {
    "h": (238, 82, 83),
    "k": (73, 201, 105),
    "l": (68, 139, 255),
}


@dataclass(frozen=True)
class SliceSpec:
    family: str = "hk0"
    layer: int = 0
    thickness_rlu: float = DEFAULT_SLICE_THICKNESS_RLU
    step_rlu: float = DEFAULT_SLICE_STEP_RLU

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"Unknown slice family: {self.family!r}; expected one of {FAMILIES}")
        if self.thickness_rlu <= 0:
            raise ValueError("Slice thickness must be positive.")
        if self.step_rlu <= 0:
            raise ValueError("Slice step must be positive.")

    def plane_axes(self) -> tuple[int, int, int]:
        """Return (u, v, out-of-plane) hkl axis indices for this family."""

        if self.family == "hk0":
            return 0, 1, 2
        if self.family == "h0l":
            return 0, 2, 1
        return 1, 2, 0

    def axis_labels(self) -> tuple[str, str, str]:
        u, v, out = self.plane_axes()
        names = ("h", "k", "l")
        return names[u], names[v], names[out]

    def to_dict(self) -> dict:
        data = {
            "family": self.family,
            "layer": int(self.layer),
            "thickness_rlu": float(self.thickness_rlu),
            "step_rlu": float(self.step_rlu),
        }
        data["axis_u"], data["axis_v"], data["axis_out_of_plane"] = self.axis_labels()
        return data


@dataclass
class SliceData:
    spec: SliceSpec
    axis_u: np.ndarray
    axis_v: np.ndarray
    image: np.ndarray
    counts: np.ndarray
    point_count: int
    total_intensity: float
    effective_step_rlu: float

    @property
    def shape(self) -> tuple[int, int]:
        return tuple(int(v) for v in self.image.shape)

    def to_dict(self) -> dict:
        data = self.spec.to_dict()
        data.update(
            {
                "shape": list(self.shape),
                "point_count": int(self.point_count),
                "total_intensity": float(self.total_intensity),
                "effective_step_rlu": float(self.effective_step_rlu),
                "axis_u_range": [float(self.axis_u[0]), float(self.axis_u[-1])] if self.axis_u.size else [],
                "axis_v_range": [float(self.axis_v[0]), float(self.axis_v[-1])] if self.axis_v.size else [],
            }
        )
        return data


def build_slice(hkl: np.ndarray, intensity: np.ndarray, spec: SliceSpec) -> SliceData:
    """Bin observations inside the slab around the requested lattice plane."""

    hkl = np.asarray(hkl, dtype=np.float64)
    intensity = np.asarray(intensity, dtype=np.float64)
    if hkl.ndim != 2 or hkl.shape[1] != 3:
        raise ValueError("hkl must be an (N, 3) array")
    if hkl.shape[0] != intensity.shape[0]:
        raise ValueError("hkl and intensity must have the same length")

    finite = np.isfinite(hkl).all(axis=1) & np.isfinite(intensity)
    u_index, v_index, out_index = spec.plane_axes()
    keep = finite & (
        np.abs(hkl[:, out_index] - float(spec.layer)) <= spec.thickness_rlu / 2.0
    )
    points = hkl[keep]
    values = intensity[keep]

    step = float(spec.step_rlu)
    if points.shape[0] == 0:
        empty = np.zeros((1, 1), dtype=np.float64)
        return SliceData(
            spec=spec,
            axis_u=np.zeros((1,), dtype=np.float64),
            axis_v=np.zeros((1,), dtype=np.float64),
            image=empty,
            counts=empty.copy(),
            point_count=0,
            total_intensity=0.0,
            effective_step_rlu=step,
        )

    u = points[:, u_index]
    v = points[:, v_index]
    span_u = max(float(u.max() - u.min()), step)
    span_v = max(float(v.max() - v.min()), step)
    step = max(
        step,
        span_u / float(MAX_SLICE_BINS_PER_AXIS),
        span_v / float(MAX_SLICE_BINS_PER_AXIS),
    )
    u_start = float(np.floor(u.min() / step) * step)
    u_stop = float(np.ceil(u.max() / step) * step)
    v_start = float(np.floor(v.min() / step) * step)
    v_stop = float(np.ceil(v.max() / step) * step)
    edges_u = np.arange(u_start, u_stop + 1.5 * step, step)
    edges_v = np.arange(v_start, v_stop + 1.5 * step, step)

    image, _, _ = np.histogram2d(v, u, bins=(edges_v, edges_u), weights=values)
    counts, _, _ = np.histogram2d(v, u, bins=(edges_v, edges_u))

    axis_u = edges_u[:-1] + step / 2.0
    axis_v = edges_v[:-1] + step / 2.0
    return SliceData(
        spec=spec,
        axis_u=axis_u,
        axis_v=axis_v,
        image=image,
        counts=counts,
        point_count=int(points.shape[0]),
        total_intensity=float(values.sum()),
        effective_step_rlu=step,
    )


def slice_title(data: SliceData) -> str:
    spec = data.spec
    label_u, label_v, out_label = spec.axis_labels()
    return (
        f"{spec.family} slice | {out_label}={spec.layer} "
        f"(+-{spec.thickness_rlu / 2.0:.3f}) | {label_u}-{label_v} plane"
    )


def _colorize(image: np.ndarray) -> np.ndarray:
    positive_image = np.maximum(image.astype(np.float32), 0.0)
    view = np.log1p(gaussian_filter(positive_image, sigma=1.0, mode="constant"))
    positive = view[view > 0]
    if positive.size:
        lo = float(np.percentile(positive, 1.0))
        hi = float(np.percentile(positive, 99.5))
    else:
        lo, hi = 0.0, 1.0
    t = np.clip((view - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    red = (255.0 * t).astype(np.uint8)
    green = (255.0 * np.sqrt(t) * (1.0 - 0.25 * t)).astype(np.uint8)
    blue = (255.0 * (1.0 - t) * (0.15 + 0.85 * t)).astype(np.uint8)
    return np.stack([red, green, blue], axis=-1)


def render_slice_image(
    data: SliceData,
    show_cell: bool = True,
    threshold_percent: float = 0.0,
) -> Image.Image:
    """Render a centered, square, equal-scale reciprocal-space slice.

    The data extent is padded symmetrically around zero, so the reciprocal
    origin remains at the exact canvas center for every family and layer.
    """

    threshold_percent = float(np.clip(threshold_percent, 0.0, 100.0))
    source_image = np.asarray(data.image, dtype=np.float64)
    max_intensity = float(np.max(source_image)) if source_image.size else 0.0
    if threshold_percent > 0.0 and max_intensity > 0.0:
        cutoff = max_intensity * threshold_percent / 100.0
        source_image = np.where(source_image >= cutoff, source_image, 0.0)
    rgb = _colorize(np.flipud(source_image))
    source_height, source_width = rgb.shape[:2]
    step = max(float(data.effective_step_rlu), 1e-9)
    u_start = float(data.axis_u[0] - step / 2.0) if data.axis_u.size else -step / 2.0
    u_stop = float(data.axis_u[-1] + step / 2.0) if data.axis_u.size else step / 2.0
    v_start = float(data.axis_v[0] - step / 2.0) if data.axis_v.size else -step / 2.0
    v_stop = float(data.axis_v[-1] + step / 2.0) if data.axis_v.size else step / 2.0
    half_bins = max(1, int(np.ceil(max(abs(u_start), abs(u_stop), abs(v_start), abs(v_stop)) / step)))
    half_extent = half_bins * step
    plot_bins = half_bins * 2
    background = _colorize(np.zeros((1, 1), dtype=np.float32))[0, 0]
    square_rgb = np.empty((plot_bins, plot_bins, 3), dtype=np.uint8)
    square_rgb[...] = background
    offset_x = int(round((u_start + half_extent) / step))
    offset_y = int(round((half_extent - v_stop) / step))

    target_x0 = max(0, offset_x)
    target_y0 = max(0, offset_y)
    target_x1 = min(plot_bins, offset_x + source_width)
    target_y1 = min(plot_bins, offset_y + source_height)
    if target_x1 > target_x0 and target_y1 > target_y0:
        source_x0 = target_x0 - offset_x
        source_y0 = target_y0 - offset_y
        square_rgb[target_y0:target_y1, target_x0:target_x1] = rgb[
            source_y0 : source_y0 + (target_y1 - target_y0),
            source_x0 : source_x0 + (target_x1 - target_x0),
        ]

    plot_size = min(1024, max(512, plot_bins))
    pil = Image.fromarray(square_rgb, mode="RGB").resize(
        (plot_size, plot_size),
        resample=Image.Resampling.BILINEAR,
    )
    label_u, label_v, _out_label = data.spec.axis_labels()
    if show_cell:
        overlay = Image.new("RGBA", pil.size, (0, 0, 0, 0))
        grid_draw = ImageDraw.Draw(overlay)

        def plot_x(value: float) -> float:
            return (value + half_extent) / (2.0 * half_extent) * plot_size

        def plot_y(value: float) -> float:
            return (half_extent - value) / (2.0 * half_extent) * plot_size

        grid_color = (130, 140, 150, 52)
        for value in range(int(np.ceil(-half_extent)), int(np.floor(half_extent)) + 1):
            x = plot_x(float(value))
            color = (*RECIPROCAL_AXIS_COLORS[label_u], 125) if value == 0 else grid_color
            grid_draw.line((x, 0, x, plot_size), fill=color, width=2 if value == 0 else 1)
        for value in range(int(np.ceil(-half_extent)), int(np.floor(half_extent)) + 1):
            y = plot_y(float(value))
            color = (*RECIPROCAL_AXIS_COLORS[label_v], 125) if value == 0 else grid_color
            grid_draw.line((0, y, plot_size, y), fill=color, width=2 if value == 0 else 1)
        pil = Image.alpha_composite(pil.convert("RGBA"), overlay).convert("RGB")

    canvas = Image.new("RGB", (plot_size, plot_size + SLICE_HEADER_HEIGHT), (16, 18, 20))
    canvas.paste(pil, (0, SLICE_HEADER_HEIGHT))
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 8), slice_title(data), fill=(238, 241, 243))
    axis_u_range = data.to_dict()["axis_u_range"]
    axis_v_range = data.to_dict()["axis_v_range"]
    draw.text(
        (10, 26),
        f"log color | Gaussian sigma 1 bin | threshold {threshold_percent:.0f}% max | "
        f"step {data.effective_step_rlu:.4g} r.l.u.",
        fill=(150, 158, 165),
    )
    draw.text(
        (10, 44),
        f"{label_u} [{axis_u_range[0]:.2f}, {axis_u_range[-1]:.2f}]  "
        f"{label_v} [{axis_v_range[0]:.2f}, {axis_v_range[-1]:.2f}]",
        fill=(150, 158, 165),
    )
    label_u_color = RECIPROCAL_AXIS_COLORS[label_u] if show_cell else (200, 208, 214)
    label_v_color = RECIPROCAL_AXIS_COLORS[label_v] if show_cell else (200, 208, 214)
    draw.text((pil.width - 24, pil.height + SLICE_HEADER_HEIGHT - 14), label_u, fill=label_u_color)
    draw.text((6, SLICE_HEADER_HEIGHT + 6), label_v, fill=label_v_color)
    return canvas


def render_slice_png(path: str | Path, data: SliceData) -> Path:
    """Write a log-scaled PNG with hkl axis labels."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render_slice_image(data).save(path)
    return path


def save_slice_npz(path: str | Path, data: SliceData) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        image=data.image.astype(np.float32),
        counts=data.counts.astype(np.float32),
        axis_u=data.axis_u.astype(np.float32),
        axis_v=data.axis_v.astype(np.float32),
        family=np.asarray([data.spec.family]),
        layer=np.asarray([data.spec.layer], dtype=np.int32),
        thickness_rlu=np.asarray([data.spec.thickness_rlu], dtype=np.float32),
        step_rlu=np.asarray([data.spec.step_rlu], dtype=np.float32),
        effective_step_rlu=np.asarray([data.effective_step_rlu], dtype=np.float32),
    )
    return path


def write_slice(
    out_dir: str | Path,
    hkl: np.ndarray,
    intensity: np.ndarray,
    spec: SliceSpec,
) -> dict[str, object]:
    """Build and write one slice; returns its metadata with file paths."""

    out_dir = Path(out_dir)
    data = build_slice(hkl, intensity, spec)
    stem = f"slice_{spec.family}_{spec.layer}"
    png_path = render_slice_png(out_dir / f"{stem}.png", data)
    npz_path = save_slice_npz(out_dir / f"{stem}.npz", data)
    info = data.to_dict()
    info.update({"png": str(png_path), "npz": str(npz_path), "title": slice_title(data)})
    return info


def write_canonical_slices(
    out_dir: str | Path,
    hkl: np.ndarray,
    intensity: np.ndarray,
    thickness_rlu: float = DEFAULT_SLICE_THICKNESS_RLU,
    step_rlu: float = DEFAULT_SLICE_STEP_RLU,
    layer: int = 0,
) -> dict[str, dict[str, object]]:
    """Write the hk0/h0l/0kl slices for the default layer."""

    result: dict[str, dict[str, object]] = {}
    for family in FAMILIES:
        spec = SliceSpec(
            family=family,
            layer=layer,
            thickness_rlu=thickness_rlu,
            step_rlu=step_rlu,
        )
        result[family] = write_slice(out_dir, hkl, intensity, spec)
    return result
