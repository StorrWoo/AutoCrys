from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .reconstruct import VolumeData


def json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return str(value)


def write_json(path: str | Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2, default=json_default), encoding="utf-8")


def _colorize(image: np.ndarray) -> np.ndarray:
    view = np.log1p(np.maximum(image.astype(np.float32), 0.0))
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


def _plot_slice(path: Path, image: np.ndarray, title: str, xlabel: str, ylabel: str) -> None:
    rgb = _colorize(image.T)
    pil = Image.fromarray(rgb, mode="RGB").resize(
        (max(512, rgb.shape[1]), max(512, rgb.shape[0])),
        resample=Image.Resampling.NEAREST,
    )
    canvas = Image.new("RGB", (pil.width, pil.height + 34), (16, 18, 20))
    canvas.paste(pil, (0, 34))
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 8), f"{title} | {xlabel}/{ylabel} | log color", fill=(238, 241, 243))
    canvas.save(path)


def write_volume_slices(volume: VolumeData, out_dir: str | Path) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = volume.i_mean
    ix, iy, iz = (dim // 2 for dim in data.shape)
    paths = {
        "xy": out_dir / "slice_xy.png",
        "xz": out_dir / "slice_xz.png",
        "yz": out_dir / "slice_yz.png",
    }
    _plot_slice(paths["xy"], data[:, :, iz], f"qz = {volume.qz[iz]:.4f} A^-1", "qx", "qy")
    _plot_slice(paths["xz"], data[:, iy, :], f"qy = {volume.qy[iy]:.4f} A^-1", "qx", "qz")
    _plot_slice(paths["yz"], data[ix, :, :], f"qx = {volume.qx[ix]:.4f} A^-1", "qy", "qz")
    return {name: str(path) for name, path in paths.items()}


def write_summary(path: str | Path, summary: dict) -> None:
    cell = summary.get("cell_constants")
    cell_text = (
        " ".join(f"{float(value):.3f}" for value in cell) if cell else "not available"
    )
    indexing = summary.get("indexing") or {}
    indexing_text = "not available"
    if indexing:
        residual = indexing.get("mean_abs_fractional_residual") or []
        if residual:
            indexing_text = "mean |frac| " + " ".join(f"{float(value):.3f}" for value in residual) + " r.l.u."
    slices = summary.get("slices") or {}
    lines = [
        "# AutoR3D Summary",
        "",
        f"- Dataset: `{summary.get('dataset')}`",
        f"- Frames discovered: {summary.get('frames_discovered')}",
        f"- Placeholder zero frames: {summary.get('placeholder_zero_frames', summary.get('zero_frames_excluded'))}",
        f"- Frames used for intensity: {summary.get('frames_used')}",
        f"- Preprocessing mode: {summary.get('preprocess_mode', 'unknown')}",
        (
            f"- Exclusion mask pixels: {summary.get('exclusion_mask_pixels')} "
            f"({summary.get('exclusion_mask_fraction', 0.0):.4%})"
        ),
        f"- Sparse observations: {summary.get('observations')}",
        f"- Volume shape: {summary.get('volume_shape')}",
        f"- Voxel size: {summary.get('voxel_size')} A^-1",
        f"- Chosen rotation axis: {summary.get('chosen_rotation_axis_deg')} deg",
        f"- Rotation sign: {summary.get('rotation_sign', 'n/a')}",
        f"- Axis source: {summary.get('axis_source', 'unknown')}",
    ]
    if summary.get("xds_axis_angle_deg") is not None:
        xds_files = summary.get("xds_files") or {}
        used = ", ".join(sorted(xds_files)) or "none"
        lines.append(
            f"- XDS reference: axis {summary.get('xds_axis_angle_deg'):.3f} deg "
            f"(files: {used})"
        )
    lines.extend(
        [
            f"- Crystal cell: {cell_text}",
            f"- Space group: {summary.get('space_group_number', 'n/a')}",
            f"- hkl indexing: {indexing_text}",
            (
                f"- Panel scale bar: pixelsize {summary.get('panel_pixelsize')} A^-1/px, "
                f"dimension {summary.get('panel_detector_dimensions_px')} px"
            ),
            f"- Detector distance: {summary.get('detector_distance_mm'):.4f} mm",
            f"- Peak candidates: {summary.get('peak_candidates')}",
            f"- Frame workers: {summary.get('frame_workers', 1)}",
            f"- Total runtime: {summary.get('runtime_seconds', 0.0):.3f} s",
        ]
    )
    if slices:
        lines.append("- Indexed slices: " + ", ".join(f"{family} -> `{path}`" for family, path in slices.items()))
    stage_seconds = summary.get("stage_seconds") or {}
    if stage_seconds:
        lines.extend(
            [
                "",
                "## Stage timings",
                "",
                *[
                    f"- {name}: {float(seconds):.3f} s"
                    for name, seconds in stage_seconds.items()
                ],
            ]
        )
    lines.extend(
        [
            "",
            "## Outputs",
            "",
        ]
    )
    for name, output_path in summary.get("outputs", {}).items():
        if name == "slices":
            continue
        lines.append(f"- {name}: `{output_path}`")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `spot_observations.npz` keeps frame/pixel provenance for future integration.",
            "- `indexed_observations.npz` adds hkl from the XDS orientation matrix when available.",
            "- `frame_geometry.json` preserves every frame angle slot, including zero placeholder frames.",
            "- `volume.npz` is a gridded derivative for reconstruction QC and peak finding.",
            "- Existing PETS/XDS files in the demo are treated as references only.",
        ]
    )
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
