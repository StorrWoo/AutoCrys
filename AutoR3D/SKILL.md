---
name: AutoR3D
description: Script-first self-contained cRED2/3DED reciprocal-space geometry reconstruction for AutoCrys datasets. Use this skill for TIFF stacks with Continuous 3D ED parameters and diff/frame_????.tif when the task asks for AutoR3D reconstruction, 3D panel/QC, placeholder-frame-aware geometry, sparse observations, or peak candidates without XDS/PETS/DIALS.
---

# AutoR3D Skill

## Role

AutoR3D is the self-contained 3DED geometry-reconstruction module for cRED2-style datasets.

The skill is responsible for judgment:

- identify the dataset folder and the `diff/frame_????.tif` stack
- decide whether to use default geometry or user-provided center/axis/thresholds
- preserve acquisition frame numbering, including all-zero placeholder frames
- read and explain `summary.md`, `qc_report.json`, `frame_geometry.json`, and the 3D panel
- stop when the cRED2 parameter file or TIFF stack is missing

The script is responsible for mechanics:

```text
AutoR3D/
```

The skill also keeps a stable runnable copy here:

```text
%USERPROFILE%\.codex\skills\autor3d\scripts\autor3d_pkg\autor3d\
```

Do not manually redo reconstruction geometry. Use the CLI.

---

# Scope

Use this skill for:

- cRED2/3DED TIFF stacks with `Continuous 3D ED (cRED2) parameters.txt`
- geometry-first reciprocal-space reconstruction
- Ewald-sphere pixel-to-q conversion
- rotation axis and cell/orientation read from the dataset's XDS output
- hkl-indexed point clouds and hk0/h0l/0kl thin-slab slices
- placeholder zero-frame accounting
- sparse observation tables for future 3D peak integration
- 3D peak candidate generation
- WebGL panel, PLY, VTI, NPZ, JSON, and summary outputs

Do not use this skill for:

- XDS/PETS/DIALS processing
- SHELXT/SHELXL structure solution
- final reflection integration or profile fitting
- crystallographic indexing/cell refinement

---

# Required Script Contract

Use the `AutoCrys` conda environment.

```powershell
conda activate AutoCrys
```

Or run without activating:

```powershell
conda run -n AutoCrys autor3d --help
```

Inspect a dataset:

```powershell
conda run -n AutoCrys autor3d inspect Demo\Demo_single --frames diff
```

Run full reconstruction:

```powershell
conda run -n AutoCrys autor3d run Demo\Demo_single --frames diff --out log\AutoR3D\Demo_single
```

If running from inside `Demo/Demo_single`, this shorter command is valid:

```powershell
conda run -n AutoCrys autor3d run . --frames diff --out ..\..\log\AutoR3D\Demo_single
```

Useful tuning options:

```powershell
--center-x 254.16 --center-y 253.79   # only to override the XDS beam center
--voxel-size 0.01
--preprocess peaks
--peak-threshold 30
--peak-filter-size 20
--close-point-radius 8
--frame-workers 8
--center-mask-radius 24
--mask-file custom_mask.png
--threshold-sigma 6
--min-intensity 20
--slice-thickness-rlu 0.20
--slice-step-rlu 0.05
--no-slices
```

Mask behavior:

- `--center-mask-radius` creates a circular exclusion mask around the beam center.
- `--mask-file` or `--exclusion-mask` adds an external exclusion mask from PNG/TIF/NPY/NPZ; nonzero pixels are excluded by default.
- `--mask-threshold` changes the external mask cutoff, and `--mask-invert` flips the external mask before combining it with the center mask.
- AutoR3D writes `observations/exclusion_mask.png` and `observations/exclusion_mask.npy` unless `--no-mask-preview` is used.

Rotation-axis behavior:

- AutoR3D reads the rotation axis from the dataset's XDS output:
  `diff/p/CORRECT.LP` ("LAB COORDINATES OF ROTATION AXIS", refined), with
  fallbacks to `XDS_ASCII.HKL` and `XDS.INP`.  The instrument vector
  `0.7845 -0.6201 0.0041` maps to the detector-plane angle
  `atan2(y, x) = -38.3 deg`; AutoR3D uses that angle with
  `rotation_sign=-1` and the mid-frame spindle angle.  The historical fixed
  default `142.0 deg` is the same axis line flipped by 180 deg with the
  opposite sign, which reconstructs a mirrored lattice — do not use it when
  XDS output is available.
- The XDS beam center (`DETECTOR COORDINATES (PIXELS) OF DIRECT BEAM`, shifted
  by one pixel to image coordinates), the XDS detector distance and the XDS
  wavelength are used automatically unless `--center-x/--center-y` override
  them.
- Datasets without XDS output fall back to the manual axis (default `142.0`).
- `--refine-axis`, `--axis-candidates`, `--axis-refine-*` and `--no-axis-scan`
  remain functional but are hidden from `--help`; they are reserved for the
  future axis-search milestone.

---

# Outputs

After a successful run, read these first:

```text
log/AutoR3D/summary.md
log/AutoR3D/qc_report.json
log/AutoR3D/panel/index.html
```

Mechanically important outputs:

```text
log/AutoR3D/observations/frame_geometry.json
log/AutoR3D/observations/spot_observations.npz
log/AutoR3D/observations/indexed_observations.npz
log/AutoR3D/observations/spots_with_intensity.txt
log/AutoR3D/observations/exclusion_mask.png
log/AutoR3D/observations/exclusion_mask.npy
log/AutoR3D/reconstruction/volume.npz
log/AutoR3D/reconstruction/slice_hk0_0.png
log/AutoR3D/reconstruction/slice_h0l_0.png
log/AutoR3D/reconstruction/slice_0kl_0.png
log/AutoR3D/peaks/peak_candidates.json
log/AutoR3D/exports/peaks.ply
log/AutoR3D/exports/observations_sample.ply
log/AutoR3D/exports/volume.vti
```

Interpretation:

- `frame_geometry.json` preserves every acquisition angle slot. All-zero placeholder frames must remain in this table and must not cause angle renumbering.
- `spot_observations.npz` contains only intensity-bearing observations with frame/pixel/q provenance for future integration. The default mode is 2D local-maximum spot picking, not dense raw-pixel gridding.
- TIFF statistics and peak detection share one read pass and run in deterministic frame order with up to 8 frame workers by default. Circular peak detection uses a fast candidate pass followed by exact circular-neighborhood verification.
- `indexed_observations.npz` adds fractional hkl from the XDS orientation matrix when available; it is the input for the slice viewer.
- `slice_<family>_<layer>.png` and `.npz` are thin-slab 2D slices of the indexed point cloud (`hk0` = l=0, `h0l` = k=0, `0kl` = h=0) with axes in r.l.u.; regenerate them from the UI Slices viewer with any layer/thickness.
- Slice PNGs and UI previews use a fixed square 1:1 plot area with equal h/k/l axis scaling; unequal data extents are centered with padding rather than stretched.
- `spots_with_intensity.txt` records the per-frame picked `(x, y, intensity)` spots for quick inspection and parameter tuning.
- `exclusion_mask.png` shows the exact detector pixels excluded before peak picking or dense-pixel collection.
- `volume.npz` is a gridded derivative for QC and peak finding, not the primary raw evidence.
- `peak_candidates.json` is a first-pass list for later integration development and carries `hkl` when indexing is available.
- `panel/index.html` is the orthographic 3D viewer with Peak Candidates, Volume, and Raw Observations tabs. Point positions stay in physical q (A^-1). When indexing is available, the XDS reciprocal basis is used to draw and align the a*/b*/c* axes and the non-orthogonal reciprocal-cell guide; hkl remains available for indexing and slice operations. The point-cloud tabs use a fixed simple point rendering path with intensity threshold, point size, and frame-range filters.
- The panel scale bar reads cRED2 `Pixelsize` as reciprocal pixel scale and optional detector dimension. If dimension is absent, use `516 x 516 px`.
- `qc_report.json` and `summary.md` include per-stage timings; the UI also reports each completed stage and total runtime.

---

# XDS reference and indexed slices

When the dataset has XDS output, AutoR3D reads and uses it automatically:

- rotation axis: `diff/p/CORRECT.LP` refined `LAB COORDINATES OF ROTATION AXIS`
  (fallbacks `XDS_ASCII.HKL`, `XDS.INP`)
- beam center: `DETECTOR COORDINATES (PIXELS) OF DIRECT BEAM` minus one pixel
  (image coordinates), detector distance, wavelength
- cell + orientation: refined `COORDINATES OF UNIT CELL A/B/C-AXIS`; the
  reciprocal basis `B` gives `hkl = B^-1 q` (`q @ inv(B).T`)

Indexing is only possible when the cell axes are present.  A reference dataset
(sample2_1, 211 frames) indexes with a mean fractional residual of about
0.06-0.08 r.l.u.; values near 0.25 mean the q frame and the orientation matrix
disagree.

The canonical slices are written for the l=0/k=0/h=0 layers:

```text
reconstruction/slice_hk0_0.png   (axes h, k; out-of-plane l)
reconstruction/slice_h0l_0.png   (axes h, l; out-of-plane k)
reconstruction/slice_0kl_0.png   (axes k, l; out-of-plane h)
```

and the matching `.npz` files carry `axis_u`, `axis_v`, `image`, `counts`.
The UI AutoR3D tab has a Slices viewer for arbitrary family/layer, thickness
(default 0.20 r.l.u.) and bin step (default 0.05 r.l.u.). Slice images use a
square equal-scale plot with the reciprocal origin fixed at the canvas center;
the optional overlay uses the full integer reciprocal grid from the original
slice style, and the UI wheel zoom stays centered on the origin. The Slice
Threshold control is a display/export filter expressed as a percentage of the
current binned slice maximum; it does not modify the raw NPZ arrays.

The 3D panel is always orthographic. Its embedded UI uses one observations
view with a full-width intensity threshold; low-intensity points fade into the
background while strong points approach a single bright blue-white color.
Each observation is one filled circle; no halo or multi-layer point is drawn.
Threshold, Size, Brightness, Contrast, and Gamma remain as point controls, but
all three tone controls only adjust the single-circle opacity curve and never
add extra drawing layers; contrast is anchored at the black background so a
zero-signal point stays invisible. The point-source, display-mode, and Reset View
controls are intentionally hidden. The four Size/Brightness/Contrast/Gamma
sliders have equal lengths. When indexed hkl observations are available, the
Indexed switch keeps only points whose h, k, and l are each within 0.1 r.l.u.
of the nearest integer; it stays disabled for q-space-only output. Its optional
cell guide is a centered 2 x 2 x 2 reciprocal grid transformed into physical q
by the XDS reciprocal basis, so non-orthogonal cells retain their true reciprocal
angles and lengths. It is colored a* red, b* green and c* blue. A/B/C align
those physical axes with c*/c*/b* fixed upward, respectively; I restores the
isometric view, F fits, and G toggles the guide. Brightness and Contrast default
to their maximum values. Left drag uses screen-relative trackball-style
360-degree orbiting;
Shift+drag rolls in the screen plane, Alt+drag constrains unrestricted vertical
rotation, Ctrl+drag constrains unrestricted horizontal rotation, right/middle
drag pans, and the wheel zooms.

---

# Placeholder Frame Policy

All-zero frames in `diff/` are acquisition placeholders, not scientific outliers.

Required behavior:

- keep the original frame number
- keep the cRED2 angle slot
- mark `is_placeholder_zero_frame: true`
- mark `used_for_intensity: false`
- do not include them in `spot_observations.npz`
- do not compress or renumber neighboring frame angles

For the current demo, expected QC is:

```text
frames discovered: 421
placeholder zero frames: 42
frames used for intensity: 379
```

---

# Result Handling

Report at least:

```text
Dataset:
Frame count / placeholder count / intensity frame count:
Frames with picked observations, if different:
Exclusion mask pixel count / source:
Detector distance:
Beam center:
XDS reference files found:
Rotation axis (XDS angle and AutoR3D value/sign) and axis source:
Crystal cell / space group:
hkl indexing residual (mean |frac| r.l.u.):
Panel scale bar pixelsize / dimension:
Observation count:
Volume shape and voxel size:
Peak candidate count:
Indexed slice paths:
Panel:
Main outputs:
Notes / next parameters to try:
```

If reconstruction is empty or diffuse, adjust one parameter at a time:

1. Lower or raise `--peak-threshold` for the default `--preprocess peaks` mode.
2. Tune `--peak-filter-size` and `--close-point-radius` if one diffraction spot is split into too many points or neighboring spots are being merged.
3. Increase `--center-mask-radius` if the direct beam/center spot is leaking into observations.
4. Use `--preprocess pixels` only when you intentionally want dense thresholded pixels; then tune `--min-intensity` and `--threshold-sigma`.
5. Add `--mask-file` if the center spot, beam stop, detector gaps, or damaged pixels are non-circular.
6. Check that placeholder frames are preserved in `frame_geometry.json`.
7. Try explicit `--center-x` and `--center-y` (the XDS beam center is used by default when available).
8. If hkl residuals are large, confirm the dataset `diff/p` XDS files match the frames being reconstructed; the hidden `--refine-axis`/`--axis-candidates` flags are reserved for the future axis-search milestone.
9. Increase `--voxel-size` if the volume is too sparse or too large.
10. Use `--slice-thickness-rlu`/`--slice-step-rlu` to tune the indexed slices if the hk0/h0l/0kl previews look sparse.

Do not claim final reflection integration is complete. AutoR3D v1 prepares the geometry, sparse observations, indexed slices, volume, and candidate peaks needed to build integration later.
