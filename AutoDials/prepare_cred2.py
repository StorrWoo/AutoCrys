"""Lossless cRED2 TIFF -> generic miniCBF + DIALS geometry/processing parameters.

Run with DIALS' Python. Source TIFFs and XDS outputs are never modified.
The CBFs alone lack the electron probe and arbitrary rotation-axis metadata;
always import with the generated import.phil/reference.expt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

try:
    from .dials_compat import apply
except ImportError:
    from dials_compat import apply
apply()

import numpy as np
from PIL import Image
import dxtbx
from dxtbx.ext import compress
from dxtbx.model import BeamFactory, DetectorFactory, GoniometerFactory, ScanFactory
from dxtbx.model.experiment_list import Experiment, ExperimentList
from scitbx.array_family import flex


def read_parameters(path):
    raw = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            raw[key.lstrip("# ").strip()] = value.strip()
    def number(key):
        return float(re.search(r"[-+]?(?:\d*\.)?\d+(?:[eE][-+]?\d+)?", raw[key])[0])
    return raw, number


def ranges(numbers):
    result = []
    for n in sorted(numbers):
        if result and n == result[-1][1] + 1:
            result[-1][1] = n
        else:
            result.append([n, n])
    return result


def write_cbf(path, data, *, pixel, distance, wavelength, centre, start, step, exposure, overload):
    packed = compress(flex.int(np.ascontiguousarray(data, dtype=np.int32)))
    ny, nx = data.shape
    header = f'''###CBF: VERSION 1.0
data_cred2
_array_data.header_convention GENERIC_MINI
_array_data.header_contents
;
# Detector: AutoCrys cRED2
# Pixel_size {pixel / 1000:.12g} m x {pixel / 1000:.12g} m
# Exposure_time {exposure:.12g} s
# Exposure_period {exposure:.12g} s
# Count_cutoff {overload} counts
# Wavelength {wavelength:.12g} A
# Detector_distance {distance / 1000:.12g} m
# Beam_xy ({centre[0]:.12g}, {centre[1]:.12g}) pixels
# Start_angle {start:.12g} deg.
# Angle_increment {step:.12g} deg.
;
_array_data.data
;
--CIF-BINARY-FORMAT-SECTION--
Content-Type: application/octet-stream;
     conversions="x-CBF_BYTE_OFFSET"
Content-Transfer-Encoding: BINARY
X-Binary-Size: {len(packed)}
X-Binary-ID: 1
X-Binary-Element-Type: "signed 32-bit integer"
X-Binary-Element-Byte-Order: LITTLE_ENDIAN
X-Binary-Number-of-Elements: {nx * ny}
X-Binary-Size-Fastest-Dimension: {nx}
X-Binary-Size-Second-Dimension: {ny}
X-Binary-Size-Padding: 0

'''
    path.write_bytes(header.encode("ascii") + b"\x0c\x1a\x04\xd5" + packed + b"\n--CIF-BINARY-FORMAT-SECTION----\n;\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--xparm", type=Path, help="Use XDS geometry only, never its crystal/orientation/indexing")
    parser.add_argument("--axis", type=float, nargs=3, help="Axis in DIALS laboratory coordinates")
    parser.add_argument("--centre", type=float, nargs=2, help="Fast, slow centre in pixels (DIALS edge convention)")
    parser.add_argument("--overload", type=int, default=130000, help="Detector count cutoff; verify for other detectors")
    parser.add_argument("--d-min", type=float, default=1.0)
    args = parser.parse_args()
    if args.xparm and (args.axis or args.centre):
        parser.error("--xparm cannot be combined with --axis or --centre")
    if args.overload <= 0 or args.d_min <= 0:
        parser.error("Overload and resolution must be positive")
    source, output = args.dataset.resolve(), args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("Output must be empty; use a new directory to preserve earlier runs")
    parameter_files = sorted(source.glob("*cRED2*parameters*.txt"))
    if len(parameter_files) != 1:
        parser.error("Expected exactly one cRED2 parameters file")
    raw, number = read_parameters(parameter_files[0])
    for key in ("Stretch amplitude", "Stretch azimuth"):
        if raw.get(key, "None").split()[0] not in ("None", "0", "0.0"):
            parser.error("Nonzero distortion requires a calibrated DIALS distortion map")
    frames = sorted(source.joinpath("diff").glob("frame_*.tif"))
    ids = [int(re.fullmatch(r"frame_(\d+)", p.stem)[1]) for p in frames]
    expected = int(number("Number of frames"))
    if ids != list(range(1, expected + 1)):
        parser.error("Missing/duplicate/non-contiguous frame numbers; refusing angle compression")
    with Image.open(frames[0]) as im:
        shape = np.asarray(im).shape
    if len(shape) != 2:
        parser.error("Only 2D TIFFs are supported")
    pixel, wavelength = number("Physical pixelsize"), number("Wavelength")
    distance = pixel / (wavelength * number("Pixelsize"))
    step, start = number("Oscillation angle"), number("Starting angle")
    if min(pixel, wavelength, distance, step) <= 0:
        parser.error("Expected positive pixel/wavelength/distance/oscillation; reverse scans need explicit geometry")
    centre = args.centre or [shape[1] / 2, shape[0] / 2]
    beam = BeamFactory.simple(wavelength)
    beam.set_probe(beam.get_probe_from_name("electron"))
    detector = DetectorFactory.simple("PAD", distance, tuple(c * pixel for c in centre), "+x", "-y", (pixel, pixel), (shape[1], shape[0]), (0, args.overload))
    # cRED2 legacy azimuth: +90 degrees gives the XDS image-plane axis.
    # A 180-degree X rotation maps XDS (x,y,z) to DIALS (x,-y,-z).
    theta = math.radians(number("Rotation axis") + 90)
    axis = args.axis or (math.cos(theta), -math.sin(theta), 0)
    gonio = GoniometerFactory.known_axis(axis)
    geometry_source = "cRED2 calibrated reciprocal pixel scale; nominal axis convention requires validation"
    if args.xparm:
        model = dxtbx.load(str(args.xparm.resolve()))
        beam, detector, gonio = model.get_beam(), model.get_detector(), model.get_goniometer()
        beam.set_probe(beam.get_probe_from_name("electron"))
        for panel in detector:
            panel.set_trusted_range((0, args.overload))
        step = model.get_scan().get_oscillation()[1]
        start = model.get_scan().get_oscillation()[0]
        pixel = detector[0].get_pixel_size()[0]
        wavelength = beam.get_wavelength()
        distance = detector[0].get_distance()
        centre = detector[0].get_beam_centre_px(beam.get_s0())
        geometry_source = str(args.xparm.resolve()) + " (geometry only; crystal discarded)"
    if len(detector) != 1 or tuple(detector[0].get_image_size()) != (shape[1], shape[0]):
        parser.error("Reference detector must be a single panel matching the TIFF dimensions")
    if step <= 0:
        parser.error("Reverse scans require explicit geometry handling")
    scan = ScanFactory.make_scan((1, expected), [number("Exposure Time")] * expected, (start, step), list(range(expected)))
    output.mkdir(parents=True, exist_ok=True)
    cbf_dir = output / "cbf"
    cbf_dir.mkdir()
    reference = ExperimentList([Experiment(beam=beam, detector=detector, goniometer=gonio, scan=scan)])
    reference.as_file(str(output / "reference.expt"))
    manifest, zeros = [], []
    for n, path in zip(ids, frames):
        original = path.read_bytes()
        with Image.open(path) as im:
            data = np.asarray(im)
        if data.shape != shape or data.dtype.kind not in "iu" or data.min() < 0 or data.max() > np.iinfo(np.int32).max:
            raise ValueError(f"Unsupported TIFF shape/type/range: {path}")
        if not np.any(data):
            zeros.append(n)
        converted = cbf_dir / f"frame_{n:04d}.cbf"
        write_cbf(converted, data, pixel=pixel, distance=distance, wavelength=wavelength,
                  centre=centre, start=start + (n - 1) * step, step=step,
                  exposure=number("Exposure Time"), overload=args.overload)
        decoded = dxtbx.load(str(converted)).get_raw_data().as_numpy_array()
        if not np.array_equal(data, decoded):
            raise AssertionError(f"CBF roundtrip failed: {path}")
        manifest.append({"frame": n, "source_sha256": hashlib.sha256(original).hexdigest(),
                         "max": int(data.max()), "zero": n in zeros})
        if n % 25 == 0 or n == expected:
            print(f"TIFF -> CBF: {n}/{expected} frames verified", flush=True)
    excluded = ranges(zeros)
    (output / "import.phil").write_text(
        'input.template="cbf/frame_####.cbf"\ninput.reference_geometry="reference.expt"\ninput.check_reference_geometry=False\n'
        'geometry.beam.probe=electron\ngeometry.scan.oscillation=' + f'{start},{step}\n', encoding="utf-8")
    exclusions = "".join(f"exclude_images=0:{a}:{b}\n" for a, b in excluded)
    (output / "exclude.phil").write_text(exclusions, encoding="utf-8")
    (output / "spots.phil").write_text(
        f"spotfinder.filter.d_min={args.d_min}\nspotfinder.filter.d_max=20\n"
        "spotfinder.threshold.dispersion.gain=1\nspotfinder.filter.min_spot_size=3\n"
        "spotfinder.mp.nproc=1\n" + exclusions.replace("exclude_images=", "spotfinder.exclude_images="), encoding="utf-8")
    (output / "index.phil").write_text(
        "refinement.parameterisation.detector.fix=distance\n"
        "refinement.parameterisation.goniometer.fix=None\n"
        "indexing.known_symmetry.space_group=P1\n", encoding="utf-8")
    (output / "integrate.phil").write_text(
        f"prediction.d_min={args.d_min}\nintegration.mp.nproc=1\n" + exclusions, encoding="utf-8")
    report = {"source": str(source), "geometry_source": geometry_source,
              "frames": expected, "zero_frames": zeros, "excluded_ranges": excluded,
              "shape": shape, "nominal_camera_length_mm": number("Camera length"),
              "calibrated_distance_mm": distance, "wavelength_A": wavelength,
              "axis_dials": gonio.get_rotation_axis(), "centre_px": centre,
              "start_deg": start, "step_deg": step, "all_frames_lossless_verified": True,
              "overload": args.overload, "resolution_limit_A": args.d_min, "frame_manifest": manifest}
    (output / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "frame_manifest"}, indent=2))


if __name__ == "__main__":
    main()
