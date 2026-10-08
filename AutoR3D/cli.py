from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import numpy as np

from .axis import parse_axis_candidates, refine_rotation_axis
from .cred2 import find_cred2_file, parse_cred2_parameters
from .exports import (
    write_json as write_export_json,
    write_observations_sample_ply,
    write_peaks_ply,
    write_vti,
)
from .geometry import build_geometry
from .images import (
    build_exclusion_mask,
    discover_frames,
    inspect_frames,
    load_image,
    save_exclusion_mask_preview,
)
from .observations import (
    DEFAULT_FRAME_WORKERS,
    collect_peak_pixels_with_stats,
    collect_sparse_pixels_with_stats,
    compute_observations,
)
from .panel import write_panel
from .peaks import find_peak_candidates, save_peak_candidates
from .qc import write_json, write_summary, write_volume_slices
from .reconstruct import grid_observations
from .slices import (
    DEFAULT_SLICE_STEP_RLU,
    DEFAULT_SLICE_THICKNESS_RLU,
    write_canonical_slices,
)
from .xds import load_xds_reference
from .reference import load_reference


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _resolve_dataset_relative_path(path: Path | None, dataset_dir: Path) -> Path | None:
    if path is None:
        return None
    if path.is_absolute():
        return path.resolve()
    dataset_candidate = dataset_dir / path
    if dataset_candidate.exists():
        return dataset_candidate.resolve()
    return path.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autor3d",
        description="AutoR3D geometry-first 3DED reciprocal-space reconstruction",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Inspect a cRED2 dataset without reconstructing")
    inspect.add_argument("dataset_dir", type=Path)
    inspect.add_argument("--frames", default="diff")
    inspect.add_argument("--pattern", default="frame_*.tif")

    ui = subparsers.add_parser("ui", help="Launch the AutoR3D desktop UI")
    ui.add_argument("dataset_dir", nargs="?", type=Path, default=None)
    ui.add_argument("--frames", default="diff")

    run = subparsers.add_parser("run", help="Run the full AutoR3D reconstruction workflow")
    run.add_argument("dataset_dir", type=Path)
    run.add_argument("--frames", default="diff", help="Frame folder inside the dataset")
    run.add_argument("--pattern", default="frame_*.tif")
    run.add_argument("--reference-source", choices=("auto", "xds", "dials", "manual"), default="auto")
    run.add_argument("--dials-run", type=Path, help="Successful AutoDials run belonging to this dataset")
    run.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: log/AutoR3D/<dataset-name> under the current directory)",
    )
    run.add_argument("--voxel-size", type=_positive_float, default=0.01, help="q-grid voxel size in A^-1")
    run.add_argument("--center-x", type=float, default=None)
    run.add_argument("--center-y", type=float, default=None)
    run.add_argument(
        "--rotation-axis-deg",
        type=float,
        default=142.0,
        help="Fallback rotation axis used only when the dataset has no XDS reference",
    )
    run.add_argument(
        "--axis-candidates",
        default=None,
        help=argparse.SUPPRESS,
    )
    run.add_argument("--refine-axis", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--axis-refine-range-deg", type=_positive_float, default=3.0, help=argparse.SUPPRESS)
    run.add_argument("--axis-refine-coarse-step-deg", type=_positive_float, default=0.25, help=argparse.SUPPRESS)
    run.add_argument("--axis-refine-fine-step-deg", type=_positive_float, default=0.05, help=argparse.SUPPRESS)
    run.add_argument("--no-axis-scan", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--rotation-sign", type=float, choices=[-1.0, 1.0], default=-1.0)
    run.add_argument(
        "--slice-thickness-rlu",
        type=_positive_float,
        default=DEFAULT_SLICE_THICKNESS_RLU,
        help="Total slab thickness around an hkl plane for indexed slices (r.l.u.).",
    )
    run.add_argument(
        "--slice-step-rlu",
        type=_positive_float,
        default=DEFAULT_SLICE_STEP_RLU,
        help="2D bin size of the indexed hkl slices (r.l.u.).",
    )
    run.add_argument("--no-slices", action="store_true", help="Skip indexed hkl slice generation.")
    run.add_argument("--threshold-sigma", type=float, default=6.0)
    run.add_argument("--min-intensity", type=float, default=20.0)
    run.add_argument("--center-mask-radius", type=float, default=15.0)
    run.add_argument(
        "--mask-file",
        "--exclusion-mask",
        dest="mask_file",
        type=Path,
        default=None,
        help="Optional exclusion mask image/NPY/NPZ. Nonzero pixels are excluded before preprocessing.",
    )
    run.add_argument("--mask-threshold", type=float, default=0.0)
    run.add_argument(
        "--mask-invert",
        action="store_true",
        help="Invert the external exclusion mask after thresholding before combining with the center mask.",
    )
    run.add_argument(
        "--no-mask-preview",
        action="store_true",
        help="Do not write observations/exclusion_mask.png and .npy.",
    )
    run.add_argument("--max-pixels-per-frame", type=int, default=2500)
    run.add_argument(
        "--preprocess",
        choices=["peaks", "pixels"],
        default="peaks",
        help="Use 2D local-maximum spot picking or raw thresholded pixels before 3D gridding",
    )
    run.add_argument("--peak-threshold", type=float, default=30.0)
    run.add_argument("--peak-filter-size", type=int, default=12)
    run.add_argument(
        "--peak-footprint",
        choices=["circle", "square"],
        default="circle",
        help="Local-maximum neighborhood shape; circle treats --peak-filter-size as the radius in pixels.",
    )
    run.add_argument("--close-point-radius", type=float, default=8.0)
    run.add_argument(
        "--mask-edge-guard-px",
        type=float,
        default=5.0,
        help="Extra guard band outside the center mask for peak picking; default gives 20 px exclusion with the 15 px center mask.",
    )
    run.add_argument("--max-spots-per-frame", type=int, default=1000)
    run.add_argument(
        "--frame-workers",
        type=_positive_int,
        default=DEFAULT_FRAME_WORKERS,
        help="Parallel TIFF preprocessing workers (default: up to 8 based on CPU count)",
    )
    run.add_argument(
        "--no-spot-log",
        action="store_true",
        help="Do not write observations/spots_with_intensity.txt",
    )
    run.add_argument("--max-frames", type=int, default=None, help="Use only this many nonzero frames")
    run.add_argument("--gridding", choices=["trilinear", "nearest"], default="trilinear")
    run.add_argument("--peak-percentile", type=float, default=99.5)
    run.add_argument("--max-peaks", type=_positive_int, default=2000)
    run.add_argument("--peak-radius-voxels", type=float, default=2.5)
    run.add_argument("--min-peak-hit-count", type=float, default=0.1)
    run.add_argument("--panel-observations", type=_positive_int, default=20000)
    run.add_argument("--ply-observations", type=_positive_int, default=50000)
    run.add_argument("--vti-max-dim", type=_positive_int, default=128)
    return parser


def inspect_dataset(dataset_dir: Path, frames_dir: str, pattern: str) -> dict:
    params = parse_cred2_parameters(find_cred2_file(dataset_dir))
    frames = discover_frames(dataset_dir, frames_dir=frames_dir, pattern=pattern)
    stats = inspect_frames(frames)
    zero_frames = [item.frame_number for item in stats if item.is_zero]
    shapes = sorted({tuple(item.shape) for item in stats})
    return {
        "dataset": str(dataset_dir),
        "parameters": params.to_dict(),
        "frames_discovered": len(frames),
        "frame_min": frames[0].frame_number,
        "frame_max": frames[-1].frame_number,
        "image_shapes": [list(shape) for shape in shapes],
        "placeholder_frame_count": len(zero_frames),
        "placeholder_frames": zero_frames,
        "zero_frame_count": len(zero_frames),
        "zero_frames": zero_frames,
        "nonzero_frame_count": len(frames) - len(zero_frames),
    }


def build_frame_geometry_table(frames, stats_by_frame, params) -> list[dict]:
    table = []
    for record in frames:
        stats = stats_by_frame[record.frame_number]
        is_placeholder = bool(stats.is_zero)
        table.append(
            {
                "frame_number": int(record.frame_number),
                "path": str(record.path),
                "angle_deg": float(params.angle_for_frame(record.frame_number)),
                "is_placeholder_zero_frame": is_placeholder,
                "used_for_intensity": not is_placeholder,
                "total_intensity": float(stats.total_intensity),
                "max_intensity": float(stats.max_intensity),
                "nonzero_pixels": int(stats.nonzero_pixels),
            }
        )
    return table


def run_workflow(args: argparse.Namespace) -> dict:
    start_time = time.perf_counter()
    stage_seconds: dict[str, float] = {}
    progress_callback = getattr(args, "progress_callback", None)

    def start_stage(key: str, label: str) -> float:
        if callable(progress_callback):
            progress_callback("started", key, label, None)
        return time.perf_counter()

    def finish_stage(key: str, label: str, started: float) -> None:
        elapsed = time.perf_counter() - started
        stage_seconds[key] = elapsed
        if callable(progress_callback):
            progress_callback("finished", key, label, elapsed)

    setup_started = start_stage("setup", "Dataset setup")
    dataset_dir = args.dataset_dir.resolve()
    output_arg = getattr(args, "out", None)
    out_dir = (
        output_arg.resolve()
        if output_arg is not None
        else (Path.cwd() / "log" / "AutoR3D" / dataset_dir.name).resolve()
    )
    reconstruction_dir = out_dir / "reconstruction"
    observations_dir = out_dir / "observations"
    peaks_dir = out_dir / "peaks"
    exports_dir = out_dir / "exports"
    panel_dir = out_dir / "panel"
    for folder in (reconstruction_dir, observations_dir, peaks_dir, exports_dir, panel_dir):
        folder.mkdir(parents=True, exist_ok=True)

    params = parse_cred2_parameters(find_cred2_file(dataset_dir))
    reference = load_reference(dataset_dir, getattr(args, "reference_source", "auto"), getattr(args, "dials_run", None))
    is_dials = getattr(reference, "source", None) == "DIALS"
    if is_dials:
        params = replace(params, starting_angle_deg=reference.starting_angle_deg - (reference.first_frame - 1) * reference.oscillation_angle_deg,
                         oscillation_angle_deg=reference.oscillation_angle_deg)
    explicit_candidates = parse_axis_candidates(args.axis_candidates)
    axis_search_enabled = bool(args.refine_axis or explicit_candidates) and not args.no_axis_scan
    if is_dials and axis_search_enabled:
        raise ValueError("DIALS native geometry uses its refined rotation axis; disable axis search.")
    frames = discover_frames(dataset_dir, frames_dir=args.frames, pattern=args.pattern)
    first_image = load_image(frames[0].path)
    detector_shape = tuple(int(value) for value in first_image.shape)

    beam_center = reference.beam_center_px if reference is not None else None
    if args.center_x is None:
        center_x = float(beam_center[0] if beam_center is not None else detector_shape[1] / 2.0)
    else:
        center_x = float(args.center_x)
    if args.center_y is None:
        center_y = float(beam_center[1] if beam_center is not None else detector_shape[0] / 2.0)
    else:
        center_y = float(args.center_y)
    detector_distance_mm = reference.detector_distance_mm if reference is not None else None
    wavelength_angstrom = reference.wavelength_angstrom if reference is not None else None
    angle_offset_deg = (
        0.5 * float(params.oscillation_angle_deg)
        if reference is not None and reference.angle_mid_frame and not axis_search_enabled
        else 0.0
    )
    angle_params = replace(params, starting_angle_deg=params.starting_angle_deg + angle_offset_deg)
    mask_file = _resolve_dataset_relative_path(args.mask_file, dataset_dir)
    exclusion_mask = build_exclusion_mask(
        detector_shape,
        center_x=center_x,
        center_y=center_y,
        center_mask_radius=args.center_mask_radius,
        mask_file=mask_file,
        mask_threshold=args.mask_threshold,
        mask_invert=args.mask_invert,
    )
    exclusion_mask_png_path = observations_dir / "exclusion_mask.png"
    exclusion_mask_npy_path = observations_dir / "exclusion_mask.npy"
    if exclusion_mask is not None and not args.no_mask_preview:
        np.save(exclusion_mask_npy_path, exclusion_mask.astype(np.uint8))
        save_exclusion_mask_preview(exclusion_mask, exclusion_mask_png_path)
    excluded_pixel_count = int(np.count_nonzero(exclusion_mask)) if exclusion_mask is not None else 0
    excluded_pixel_fraction = float(excluded_pixel_count / np.prod(detector_shape))
    finish_stage("setup", "Dataset setup", setup_started)

    spot_log_path = observations_dir / "spots_with_intensity.txt"
    frame_workers = int(getattr(args, "frame_workers", DEFAULT_FRAME_WORKERS))
    preprocess_started = start_stage("frame_preprocessing", "TIFF read and peak detection")
    if args.preprocess == "peaks":
        sparse, stats = collect_peak_pixels_with_stats(
            frames=frames,
            params=angle_params,
            center_x=center_x,
            center_y=center_y,
            peak_threshold=args.peak_threshold,
            peak_filter_size=args.peak_filter_size,
            center_mask_radius=args.center_mask_radius,
            close_point_radius=args.close_point_radius,
            mask_edge_guard_px=args.mask_edge_guard_px,
            max_spots_per_frame=args.max_spots_per_frame,
            max_frames=args.max_frames,
            output_file=None if args.no_spot_log else spot_log_path,
            exclusion_mask=exclusion_mask,
            peak_footprint_shape=args.peak_footprint,
            frame_workers=frame_workers,
            preloaded_images={frames[0].frame_number: first_image},
        )
    else:
        sparse, stats = collect_sparse_pixels_with_stats(
            frames=frames,
            params=angle_params,
            center_x=center_x,
            center_y=center_y,
            threshold_sigma=args.threshold_sigma,
            min_intensity=args.min_intensity,
            center_mask_radius=0.0,
            max_pixels_per_frame=args.max_pixels_per_frame,
            max_frames=args.max_frames,
            exclusion_mask=exclusion_mask,
            frame_workers=frame_workers,
            preloaded_images={frames[0].frame_number: first_image},
        )
    finish_stage("frame_preprocessing", "TIFF read and peak detection", preprocess_started)
    stats_by_frame = {item.frame_number: item for item in stats}
    placeholder_frames = [item.frame_number for item in stats if item.is_zero]
    nonzero_stats = [item for item in stats if not item.is_zero]
    if not nonzero_stats:
        raise RuntimeError("All frames are zero; cannot reconstruct.")
    if sparse.size == 0:
        raise RuntimeError(
            "No significant observations were collected. Lower --peak-threshold in peak "
            "preprocessing mode, or lower --min-intensity/--threshold-sigma in pixel mode."
        )

    axis_started = start_stage("axis_selection", "Rotation-axis selection")
    if axis_search_enabled:
        axis_refinement = refine_rotation_axis(
            sparse=sparse,
            params=params,
            detector_shape=detector_shape,
            base_axis_deg=args.rotation_axis_deg,
            center_x=center_x,
            center_y=center_y,
            voxel_size=args.voxel_size,
            rotation_sign=args.rotation_sign,
            refine_enabled=True,
            explicit_candidates=explicit_candidates,
            refine_range_deg=args.axis_refine_range_deg,
            coarse_step_deg=args.axis_refine_coarse_step_deg,
            fine_step_deg=args.axis_refine_fine_step_deg,
        )
        chosen_axis = axis_refinement.chosen_axis_deg
        chosen_sign = float(args.rotation_sign)
        axis_source = f"refined_{chosen_axis:.4f}deg"
    elif reference is not None:
        chosen_axis = float(reference.rotation_axis_deg)
        chosen_sign = float(reference.rotation_sign)
        axis_source = "dials_refined" if is_dials else "xds_" + ("correct_lp" if "CORRECT.LP" in reference.files else "xds_inp")
        axis_refinement = refine_rotation_axis(
            sparse=sparse,
            params=params,
            detector_shape=detector_shape,
            base_axis_deg=chosen_axis,
            center_x=center_x,
            center_y=center_y,
            voxel_size=args.voxel_size,
            rotation_sign=chosen_sign,
            refine_enabled=False,
        )
    else:
        chosen_axis = float(args.rotation_axis_deg)
        chosen_sign = float(args.rotation_sign)
        axis_source = f"fixed_{chosen_axis:.4f}deg"
        axis_refinement = refine_rotation_axis(
            sparse=sparse,
            params=params,
            detector_shape=detector_shape,
            base_axis_deg=chosen_axis,
            center_x=center_x,
            center_y=center_y,
            voxel_size=args.voxel_size,
            rotation_sign=chosen_sign,
            refine_enabled=False,
        )
    finish_stage("axis_selection", "Rotation-axis selection", axis_started)

    geometry_started = start_stage("geometry", "Reciprocal-space geometry")
    geometry = build_geometry(
        params,
        detector_shape=detector_shape,
        rotation_axis_deg=chosen_axis,
        center_x=center_x,
        center_y=center_y,
        rotation_sign=chosen_sign,
        detector_distance_mm=detector_distance_mm,
        wavelength_angstrom=wavelength_angstrom,
    )
    if is_dials:
        geometry = reference.apply_geometry(geometry)
    observations = compute_observations(sparse, geometry)
    finish_stage("geometry", "Reciprocal-space geometry", geometry_started)

    gridding_started = start_stage("gridding", "3D volume gridding")
    volume = grid_observations(observations, voxel_size=args.voxel_size, method=args.gridding)
    finish_stage("gridding", "3D volume gridding", gridding_started)

    peaks_started = start_stage("peak_candidates", "3D peak candidates")
    peaks = find_peak_candidates(
        volume=volume,
        observations=observations,
        percentile=args.peak_percentile,
        max_peaks=args.max_peaks,
        min_hit_count=args.min_peak_hit_count,
        radius_voxels=args.peak_radius_voxels,
    )
    finish_stage("peak_candidates", "3D peak candidates", peaks_started)

    indexing_started = start_stage("indexing_and_slices", "hkl indexing and slices")
    indexed_path: Path | None = None
    indexed_observations: np.ndarray | None = None
    indexing_report: dict | None = None
    slice_report: dict[str, dict] = {}
    if reference is not None and reference.has_orientation:
        indexed_observations = reference.index_q(observations.q.astype(np.float64))
        indexed_path = observations_dir / "indexed_observations.npz"
        np.savez_compressed(
            indexed_path,
            frame=observations.frame.astype(np.int32),
            x=observations.x.astype(np.float32),
            y=observations.y.astype(np.float32),
            q=observations.q.astype(np.float32),
            hkl=indexed_observations.astype(np.float32),
            intensity=observations.intensity.astype(np.float32),
        )
        residual = np.abs(indexed_observations - np.rint(indexed_observations))
        indexing_report = {
            "count": int(observations.size),
            "mean_abs_fractional_residual": [float(value) for value in residual.mean(axis=0)],
            "within_0_1_rlu": [float(value) for value in (residual <= 0.1).mean(axis=0)],
        }
        if not args.no_slices:
            slice_report = write_canonical_slices(
                reconstruction_dir,
                indexed_observations,
                observations.intensity,
                thickness_rlu=args.slice_thickness_rlu,
                step_rlu=args.slice_step_rlu,
            )
        peaks = [
            replace(
                peak,
                hkl=tuple(
                    float(value)
                    for value in reference.index_q(np.asarray([peak.q], dtype=np.float64))[0]
                ),
            )
            for peak in peaks
        ]
    finish_stage("indexing_and_slices", "hkl indexing and slices", indexing_started)

    outputs_started = start_stage("outputs", "QC and output generation")
    volume_path = reconstruction_dir / "volume.npz"
    observations_path = observations_dir / "spot_observations.npz"
    frame_geometry_path = observations_dir / "frame_geometry.json"
    peaks_path = peaks_dir / "peak_candidates.json"
    qc_path = out_dir / "qc_report.json"
    summary_path = out_dir / "summary.md"
    metadata_path = exports_dir / "metadata.json"
    peaks_ply_path = exports_dir / "peaks.ply"
    observations_ply_path = exports_dir / "observations_sample.ply"
    vti_path = exports_dir / "volume.vti"

    volume.save_npz(volume_path)
    observations.save_npz(observations_path)
    frame_geometry = build_frame_geometry_table(frames, stats_by_frame, angle_params)
    write_export_json(
        frame_geometry_path,
        {
            "frames": frame_geometry,
            "count": len(frame_geometry),
            "angle_offset_deg": angle_offset_deg,
        },
    )
    save_peak_candidates(peaks_path, peaks)
    slice_paths = write_volume_slices(volume, reconstruction_dir)
    write_peaks_ply(peaks_ply_path, peaks)
    obs_sample_indices = write_observations_sample_ply(
        observations_ply_path,
        observations,
        max_points=args.ply_observations,
    )
    vti_info = write_vti(vti_path, volume, max_dim=args.vti_max_dim)
    panel_path = write_panel(
        panel_dir,
        peaks=peaks,
        observations=observations,
        volume=volume,
        max_observations=args.panel_observations,
        reciprocal_pixel_per_angstrom=params.reciprocal_pixel_per_angstrom,
        detector_dimensions_px=params.detector_dimensions_px,
        indexed_observations=indexed_observations,
        indexed_slices=slice_report,
        crystal={
            "cell": list(reference.cell_constants) if reference and reference.cell_constants else None,
            "space_group_number": reference.space_group_number if reference else None,
            "reciprocal_basis": (
                [list(row) for row in reference.reciprocal_basis]
                if reference and reference.reciprocal_basis
                else None
            ),
        },
    )
    finish_stage("outputs", "QC and output generation", outputs_started)

    used_frames = sorted(int(v) for v in sparse.thresholds.keys())
    frames_with_observations = sorted(int(v) for v in np.unique(observations.frame))
    frame_min = int(frames[0].frame_number)
    frame_max = int(frames[-1].frame_number)
    old_reference_notes = {
        "ed3d_present": (dataset_dir / "1.ed3d").exists(),
        "pets_pts2_present": (dataset_dir / "pets.pts2").exists(),
        "note": "Existing PETS/XDS files are references only; AutoR3D uses actual frame_0001.. ordering.",
    }
    outputs = {
        "volume_npz": str(volume_path),
        "spot_observations_npz": str(observations_path),
        "indexed_observations_npz": str(indexed_path) if indexed_path is not None else None,
        "frame_geometry_json": str(frame_geometry_path),
        "spots_with_intensity_txt": str(spot_log_path) if args.preprocess == "peaks" and not args.no_spot_log else None,
        "exclusion_mask_png": (
            str(exclusion_mask_png_path) if exclusion_mask is not None and not args.no_mask_preview else None
        ),
        "exclusion_mask_npy": (
            str(exclusion_mask_npy_path) if exclusion_mask is not None and not args.no_mask_preview else None
        ),
        "peak_candidates_json": str(peaks_path),
        "qc_report_json": str(qc_path),
        "summary_md": str(summary_path),
        "panel_html": str(panel_path),
        "peaks_ply": str(peaks_ply_path),
        "observations_sample_ply": str(observations_ply_path),
        "volume_vti": str(vti_path),
        "metadata_json": str(metadata_path),
        "slices": {family: entry["png"] for family, entry in slice_report.items()},
    }
    report = {
        "dataset": str(dataset_dir),
        "parameters": params.to_dict(),
        "geometry": geometry.to_dict(),
        "frames": {
            "discovered": len(frames),
            "range": [frame_min, frame_max],
            "placeholder_zero_frames": placeholder_frames,
            "placeholder_frame_count": len(placeholder_frames),
            "zero_frames_excluded": placeholder_frames,
            "zero_frame_count": len(placeholder_frames),
            "nonzero_frame_count": len(nonzero_stats),
            "intensity_frame_count": len(used_frames),
            "used_frame_count": len(used_frames),
            "used_frames_first_last": [used_frames[0], used_frames[-1]] if used_frames else None,
            "frames_with_observations_count": len(frames_with_observations),
            "frames_with_observations_first_last": (
                [frames_with_observations[0], frames_with_observations[-1]]
                if frames_with_observations
                else None
            ),
            "angle_slots_preserved": len(frames),
            "placeholder_policy": (
                "Placeholder zero frames keep their frame number and angle slot; "
                "they contribute no intensity pixels to gridding."
            ),
        },
        "sparse_observations": {
            "count": observations.size,
            "preprocess_mode": sparse.mode,
            "frame_workers": frame_workers,
            "threshold_sigma": args.threshold_sigma,
            "min_intensity": args.min_intensity,
            "peak_threshold": args.peak_threshold,
            "peak_filter_size": args.peak_filter_size,
            "peak_footprint": args.peak_footprint,
            "close_point_radius": args.close_point_radius,
            "mask_edge_guard_px": args.mask_edge_guard_px,
            "center_peak_exclusion_radius": args.center_mask_radius + max(0.0, args.mask_edge_guard_px),
            "max_spots_per_frame": args.max_spots_per_frame,
            "center_mask_radius": args.center_mask_radius,
            "external_mask_file": str(mask_file) if mask_file is not None else None,
            "external_mask_threshold": args.mask_threshold,
            "external_mask_inverted": bool(args.mask_invert),
            "excluded_pixel_count": excluded_pixel_count,
            "excluded_pixel_fraction": excluded_pixel_fraction,
            "mask_preview_written": bool(exclusion_mask is not None and not args.no_mask_preview),
            "max_pixels_per_frame": args.max_pixels_per_frame,
            "sample_export_count": int(len(obs_sample_indices)),
        },
        "axis_scan": {
            "enabled": axis_refinement.enabled,
            "chosen_rotation_axis_deg": chosen_axis,
            "scores": [score.to_dict() for score in axis_refinement.scores],
            "deprecated": True,
        },
        "axis_refinement": {
            **axis_refinement.to_dict(),
            "axis_source": axis_source,
            "explicit_candidates": explicit_candidates or [],
            "disabled_by_no_axis_scan": bool(args.no_axis_scan),
        },
        "reference": reference.to_dict() if reference is not None else None,
        "reference_source": getattr(reference, "source", "XDS") if reference else "manual",
        "xds": reference.to_dict() if reference is not None and not is_dials else None,
        "crystal": {
            "cell": list(reference.cell_constants) if reference is not None and reference.cell_constants else None,
            "space_group_number": reference.space_group_number if reference is not None else None,
            "reciprocal_basis": (
                [list(row) for row in reference.reciprocal_basis]
                if reference is not None and reference.reciprocal_basis
                else None
            ),
            "frame_angle_offset_deg": angle_offset_deg,
            "indexing": indexing_report,
            "slices": slice_report,
        },
        "panel": {
            "scale_bar": {
                "enabled": True,
                "reciprocal_pixel_per_angstrom": params.reciprocal_pixel_per_angstrom,
                "detector_dimensions_px": list(params.detector_dimensions_px),
                "source": "Continuous 3D ED (cRED2) parameters",
            }
        },
        "volume": {
            "shape": list(volume.shape),
            "voxel_size": volume.voxel_size,
            "method": volume.method,
            "nonzero_voxels": int(np.count_nonzero(volume.hit_count)),
            "max_i_mean": float(np.max(volume.i_mean)) if volume.i_mean.size else 0.0,
            "slice_paths": slice_paths,
        },
        "peaks": {
            "count": len(peaks),
            "percentile": args.peak_percentile,
            "max_peaks": args.max_peaks,
            "radius_voxels": args.peak_radius_voxels,
        },
        "exports": {
            "vti": vti_info,
            "outputs": outputs,
        },
        "reference_files": old_reference_notes,
        "timings": {
            "stage_seconds": {name: float(value) for name, value in stage_seconds.items()},
            "frame_workers": frame_workers,
        },
        "runtime_seconds": time.perf_counter() - start_time,
    }
    write_json(qc_path, report)
    write_export_json(metadata_path, report)
    summary = {
        "dataset": str(dataset_dir),
        "frames_discovered": len(frames),
        "placeholder_zero_frames": len(placeholder_frames),
        "zero_frames_excluded": len(placeholder_frames),
        "frames_used": len(used_frames),
        "preprocess_mode": sparse.mode,
        "exclusion_mask_pixels": excluded_pixel_count,
        "exclusion_mask_fraction": excluded_pixel_fraction,
        "observations": observations.size,
        "volume_shape": list(volume.shape),
        "voxel_size": volume.voxel_size,
        "chosen_rotation_axis_deg": chosen_axis,
        "rotation_sign": chosen_sign,
        "axis_refinement_enabled": axis_refinement.enabled,
        "axis_source": axis_source,
        "reference_source": getattr(reference, "source", "XDS") if reference else "manual",
        "reference_files": reference.files if reference is not None else None,
        "xds_files": reference.files if reference is not None and not is_dials else None,
        "xds_axis_angle_deg": reference.axis_angle_deg if reference is not None and not is_dials else None,
        "cell_constants": list(reference.cell_constants) if reference is not None and reference.cell_constants else None,
        "space_group_number": reference.space_group_number if reference is not None else None,
        "indexing": indexing_report,
        "slices": {family: entry["png"] for family, entry in slice_report.items()},
        "panel_pixelsize": params.reciprocal_pixel_per_angstrom,
        "panel_detector_dimensions_px": list(params.detector_dimensions_px),
        "detector_distance_mm": geometry.detector_distance_mm,
        "peak_candidates": len(peaks),
        "frame_workers": frame_workers,
        "stage_seconds": stage_seconds,
        "runtime_seconds": report["runtime_seconds"],
        "outputs": outputs,
    }
    write_summary(summary_path, summary)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect_dataset(args.dataset_dir.resolve(), args.frames, args.pattern)
            print(json.dumps(report, indent=2, default=str))
            return 0
        if args.command == "run":
            report = run_workflow(args)
            print(f"AutoR3D complete: {report['exports']['outputs']['summary_md']}")
            print(f"3D panel: {report['exports']['outputs']['panel_html']}")
            return 0
        if args.command == "ui":
            from UI.ui import main as ui_main

            argv = []
            if args.dataset_dir is not None:
                argv.append(str(args.dataset_dir))
            argv.extend(["--frames", args.frames])
            return ui_main(argv)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:
        print(f"AutoR3D error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
