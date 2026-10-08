"""XDS reference geometry for AutoR3D.

AutoR3D reconstructs the reciprocal-space point cloud from the frames.  The
crystal lattice (cell + orientation) is taken from the XDS run of the same
dataset instead of being fitted by AutoR3D:

- rotation axis       CORRECT.LP "LAB COORDINATES OF ROTATION AXIS" (refined),
                      fallback XDS_ASCII.HKL / XDS.INP
- unit cell           CORRECT.LP "COORDINATES OF UNIT CELL A/B/C-AXIS" (refined),
                      fallback XDS_ASCII.HKL header / XDS.INP
- beam center         CORRECT.LP "DETECTOR COORDINATES (PIXELS) OF DIRECT BEAM"
- distance/wavelength XDS files (see ``load_xds_reference``)

Frame convention (calibrated against Data/sample2_1 and XDS's own calculated
spot positions, residual 0.06 r.l.u.):

- XDS pixel coordinates equal image array coordinates plus one pixel in both
  axes (validated against the independent PETS beam center: XDS 260.16/259.79
  minus 1 equals PETS 259.16/258.83 within 0.04 px).
- The detector y axis is not flipped.
- The spindle rotation is ``q_lab(phi) = R(axis, +phi) @ q_crystal`` with
  ``phi`` measured from the absolute scan start; AutoR3D obtains the crystal
  frame with ``rotation_sign=-1`` and the mid-frame angle.  The historical
  ``axis+180 deg, sign=-1`` default reconstructs the mirrored lattice
  (indexing residual 0.26 r.l.u. instead of 0.06 r.l.u.).
- Reciprocal indexing uses ``hkl = B^-1 q`` with ``B`` built from the
  lab-frame real-space cell vectors ``M = [a b c]``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re

import numpy as np


_FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _last_match(pattern: str, text: str) -> re.Match[str] | None:
    match = None
    for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
        pass
    return match


def _floats(pattern: str, text: str, count: int) -> tuple[float, ...] | None:
    match = _last_match(pattern, text)
    if match is None:
        return None
    return tuple(float(match.group(index)) for index in range(1, count + 1))


def _cell_axes_from_correct(text: str) -> tuple[tuple[float, float, float], ...] | None:
    axes = []
    for label in ("A", "B", "C"):
        values = _floats(
            rf"COORDINATES OF UNIT CELL {label}-AXIS\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})",
            text,
            3,
        )
        if values is None:
            return None
        axes.append(values)
    return (axes[0], axes[1], axes[2])


def _cell_axes_from_ascii_header(text: str) -> tuple[tuple[float, float, float], ...] | None:
    axes = []
    for label in ("A", "B", "C"):
        values = _floats(
            rf"!UNIT_CELL_{label}-AXIS=\s*({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})",
            text,
            3,
        )
        if values is None:
            return None
        axes.append(values)
    return (axes[0], axes[1], axes[2])


def _cell_from_axes(axes: tuple[tuple[float, float, float], ...]) -> tuple[float, ...]:
    vectors = [np.asarray(vector, dtype=np.float64) for vector in axes]
    a, b, c = vectors
    length_a, length_b, length_c = (float(np.linalg.norm(vector)) for vector in vectors)
    alpha = math.degrees(math.acos(float(np.dot(b, c)) / (length_b * length_c)))
    beta = math.degrees(math.acos(float(np.dot(a, c)) / (length_a * length_c)))
    gamma = math.degrees(math.acos(float(np.dot(a, b)) / (length_a * length_b)))
    return (length_a, length_b, length_c, alpha, beta, gamma)


def reciprocal_basis_from_axes(
    axes: tuple[tuple[float, float, float], ...]
) -> tuple[tuple[float, ...], ...]:
    """Return B with ``q = B @ hkl`` from lab-frame real-space cell vectors."""

    matrix = np.asarray(axes, dtype=np.float64).T  # columns a, b, c
    basis = np.linalg.inv(matrix).T
    return tuple(tuple(float(value) for value in row) for row in basis)


def find_xds_files(dataset_dir: Path) -> dict[str, Path]:
    """Locate XDS output files belonging to a dataset."""

    dataset_dir = Path(dataset_dir)
    directories: list[Path] = []
    direct = dataset_dir / "diff" / "p"
    if direct.is_dir():
        directories.append(direct)
    for pattern in ("*/p", "*/diff/p", "*/*/p", "*/*/diff/p"):
        for path in sorted(dataset_dir.glob(pattern)):
            if path.is_dir() and path not in directories:
                directories.append(path)

    found: dict[str, Path] = {}
    for directory in directories:
        for name in ("CORRECT.LP", "XDS_ASCII.HKL", "XDS.INP", "GXPARM.XDS"):
            path = directory / name
            if path.is_file():
                found.setdefault(name, path)

    if not found:
        for path in sorted(dataset_dir.rglob("XDS.INP")):
            found["XDS.INP"] = path
            for name in ("CORRECT.LP", "XDS_ASCII.HKL", "GXPARM.XDS"):
                candidate = path.parent / name
                if candidate.is_file():
                    found[name] = candidate
            break
    return found


@dataclass(frozen=True)
class XdsReference:
    dataset: str
    files: dict[str, str]
    axis_vector: tuple[float, float, float]
    axis_angle_deg: float
    rotation_axis_deg: float
    rotation_sign: float
    angle_mid_frame: bool
    cell_constants: tuple[float, ...] | None
    cell_axes: tuple[tuple[float, float, float], ...] | None
    reciprocal_basis: tuple[tuple[float, ...], ...] | None
    beam_center_px: tuple[float, float] | None
    detector_distance_mm: float | None
    wavelength_angstrom: float | None
    space_group_number: int | None
    starting_angle_deg: float | None
    oscillation_angle_deg: float | None
    first_frame: int | None
    last_frame: int | None
    notes: tuple[str, ...] = ()

    @property
    def has_orientation(self) -> bool:
        return self.reciprocal_basis is not None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["has_orientation"] = self.has_orientation
        return data

    def index_q(self, q: np.ndarray) -> np.ndarray:
        """Map lab-frame q vectors (A^-1) to fractional hkl.

        ``hkl = B^-1 q`` with ``B`` the reciprocal basis (columns a*, b*, c*);
        in row form this is ``q @ inv(B).T``.
        """

        if not self.has_orientation:
            raise ValueError("XDS reference has no orientation matrix.")
        basis = np.asarray(self.reciprocal_basis, dtype=np.float64)
        q = np.asarray(q, dtype=np.float64)
        return q @ np.linalg.inv(basis).T

    def frame_angle(self, frame_number: int, params) -> float:
        """Spindle angle AutoR3D should use for this frame."""

        angle = params.angle_for_frame(frame_number)
        if self.angle_mid_frame and params.oscillation_angle_deg:
            angle += 0.5 * float(params.oscillation_angle_deg)
        return angle


def load_xds_reference(dataset_dir: Path) -> XdsReference | None:
    """Parse the XDS reference geometry for a dataset.

    Returns ``None`` when the dataset has no usable XDS axis at all.
    """

    dataset_dir = Path(dataset_dir).resolve()
    files = find_xds_files(dataset_dir)
    if not files:
        return None

    correct_text = _read(files["CORRECT.LP"]) if "CORRECT.LP" in files else ""
    ascii_text = _read(files["XDS_ASCII.HKL"]) if "XDS_ASCII.HKL" in files else ""
    inp_text = _read(files["XDS.INP"]) if "XDS.INP" in files else ""
    notes: list[str] = []

    axis = _floats(
        rf"LAB COORDINATES OF ROTATION AXIS\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})",
        correct_text,
        3,
    )
    axis_source = "CORRECT.LP refined rotation axis"
    if axis is None:
        axis = _floats(rf"!ROTATION_AXIS=\s*({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})", ascii_text, 3)
        axis_source = "XDS_ASCII.HKL !ROTATION_AXIS"
    if axis is None:
        axis = _floats(rf"^ROTATION_AXIS=\s*({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})", inp_text, 3)
        axis_source = "XDS.INP ROTATION_AXIS"
    if axis is None:
        return None
    axis_array = np.asarray(axis, dtype=np.float64)
    norm = float(np.linalg.norm(axis_array))
    if norm <= 0:
        return None
    axis_array /= norm
    axis = (float(axis_array[0]), float(axis_array[1]), float(axis_array[2]))
    axis_angle = math.degrees(math.atan2(axis[1], axis[0]))

    cell_axes = _cell_axes_from_correct(correct_text)
    if cell_axes is None:
        cell_axes = _cell_axes_from_ascii_header(ascii_text)
    cell_constants = _floats(
        rf"UNIT_CELL_CONSTANTS=\s*({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})",
        correct_text,
        6,
    )
    if cell_constants is None:
        cell_constants = _floats(
            rf"!UNIT_CELL_CONSTANTS=\s*({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})",
            ascii_text,
            6,
        )
    if cell_constants is None:
        cell_constants = _floats(
            rf"^UNIT_CELL_CONSTANTS=\s*({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})",
            inp_text,
            6,
        )
    if cell_axes is not None:
        cell_constants = _cell_from_axes(cell_axes)
    basis = reciprocal_basis_from_axes(cell_axes) if cell_axes is not None else None

    beam = _floats(
        rf"DETECTOR COORDINATES \(PIXELS\) OF DIRECT BEAM\s+({_FLOAT})\s+({_FLOAT})",
        correct_text,
        2,
    )
    beam_center: tuple[float, float] | None = None
    if beam is not None:
        beam_center = (beam[0] - 1.0, beam[1] - 1.0)
        notes.append("beam center converted from XDS pixels to image pixels (-1 px)")
    else:
        origin = _floats(rf"^ORGX=\s*({_FLOAT})\s+ORGY=\s*({_FLOAT})", inp_text, 2)
        if origin is not None:
            beam_center = (origin[0] - 1.0, origin[1] - 1.0)
            notes.append("beam center from XDS.INP ORGX/ORGY (-1 px)")

    distance = None
    values = _floats(rf"CRYSTAL TO DETECTOR DISTANCE \(mm\)\s+({_FLOAT})", correct_text, 1)
    if values is not None:
        distance = values[0]
    else:
        values = _floats(rf"^DETECTOR_DISTANCE=\s*({_FLOAT})", inp_text, 1)
        if values is not None:
            distance = abs(values[0])

    wavelength = None
    values = _floats(rf"!X-RAY_WAVELENGTH=\s*({_FLOAT})", ascii_text, 1)
    if values is not None:
        wavelength = values[0]
    else:
        values = _floats(rf"^X-RAY_WAVELENGTH=\s*({_FLOAT})", inp_text, 1)
        if values is not None:
            wavelength = values[0]
    if wavelength is None:
        direct = _floats(
            rf"DIRECT BEAM COORDINATES \(REC. ANGSTROEM\)\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})",
            correct_text,
            3,
        )
        if direct is not None:
            length = float(np.linalg.norm(np.asarray(direct, dtype=np.float64)))
            if length > 0:
                wavelength = 1.0 / length

    space_group = None
    match = _last_match(r"SPACE_GROUP_NUMBER=\s*(\d+)", correct_text)
    if match is None:
        match = _last_match(r"!SPACE_GROUP_NUMBER=\s*(\d+)", ascii_text)
    if match is not None:
        space_group = int(match.group(1))

    starting_angle = None
    values = _floats(rf"!STARTING_ANGLE=\s*({_FLOAT})", ascii_text, 1)
    if values is None:
        values = _floats(rf"^STARTING_ANGLE=\s*({_FLOAT})", inp_text, 1)
    if values is not None:
        starting_angle = values[0]

    oscillation = None
    values = _floats(rf"!OSCILLATION_RANGE=\s*({_FLOAT})", ascii_text, 1)
    if values is None:
        values = _floats(rf"^OSCILLATION_RANGE=\s*({_FLOAT})", inp_text, 1)
    if values is not None:
        oscillation = values[0]

    first_frame = None
    last_frame = None
    match = _last_match(r"!DATA_RANGE=\s*(\d+)\s+(\d+)", ascii_text)
    if match is not None:
        first_frame = int(match.group(1))
        last_frame = int(match.group(2))

    notes.append(f"rotation axis from {axis_source}")
    if cell_axes is None:
        notes.append("no XDS orientation matrix; hkl indexing and slices are disabled")
    else:
        notes.append("hkl = B^-1 q with B from the XDS unit cell axes")

    return XdsReference(
        dataset=str(dataset_dir),
        files={name: str(path) for name, path in files.items()},
        axis_vector=axis,
        axis_angle_deg=axis_angle,
        rotation_axis_deg=axis_angle,
        rotation_sign=-1.0,
        angle_mid_frame=True,
        cell_constants=cell_constants,
        cell_axes=cell_axes,
        reciprocal_basis=basis,
        beam_center_px=beam_center,
        detector_distance_mm=distance,
        wavelength_angstrom=wavelength,
        space_group_number=space_group,
        starting_angle_deg=starting_angle,
        oscillation_angle_deg=oscillation,
        first_frame=first_frame,
        last_frame=last_frame,
        notes=tuple(notes),
    )
