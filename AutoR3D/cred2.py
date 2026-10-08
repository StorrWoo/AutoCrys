from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re


_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def _first_float(text: str, default: float | None = None) -> float | None:
    match = _FLOAT_RE.search(text)
    if match is None:
        return default
    return float(match.group(0))


def _first_int(text: str, default: int | None = None) -> int | None:
    value = _first_float(text, None)
    if value is None:
        return default
    return int(round(value))


def _dimension_pair(raw: dict[str, str], default: tuple[int, int] = (516, 516)) -> tuple[int, int]:
    dimension_keys = [
        "Dimension",
        "Dimensions",
        "Image dimension",
        "Image dimensions",
        "Detector dimension",
        "Detector dimensions",
        "Camera dimension",
        "Camera dimensions",
        "Resolution",
    ]
    text = ""
    for key in dimension_keys:
        if key in raw:
            text = raw[key]
            break
    if not text:
        for key, value in raw.items():
            lowered = key.lower()
            if "dimension" in lowered or "resolution" in lowered:
                text = value
                break
    values = [int(round(float(item))) for item in _FLOAT_RE.findall(text)]
    if len(values) >= 2:
        return int(values[0]), int(values[1])
    if len(values) == 1:
        return int(values[0]), int(values[0])
    return default


@dataclass(frozen=True)
class CRED2Parameters:
    path: str
    data_collection_time: str | None
    starting_angle_deg: float
    ending_angle_deg: float | None
    rotation_range_deg: float | None
    exposure_time_s: float | None
    acquisition_time_s: float | None
    total_time_s: float | None
    high_tension_v: float | None
    camera_length_mm: float | None
    reciprocal_pixel_per_angstrom: float
    physical_pixel_mm: float
    wavelength_angstrom: float
    stretch_amplitude_percent: float | None
    stretch_azimuth_deg: float | None
    rotation_axis_deg: float | None
    oscillation_angle_deg: float
    number_of_frames: int
    detector_dimensions_px: tuple[int, int] = (516, 516)

    @property
    def detector_distance_mm(self) -> float:
        """Infer detector distance from q-pixel scale, wavelength, and pixel size.

        For small detector angles, q_per_pixel ~= pixel_mm / (lambda * distance_mm).
        The demo cRED2 values give 0.055 / (0.02508 * 0.004950299) = 443 mm.
        """

        return self.physical_pixel_mm / (
            self.wavelength_angstrom * self.reciprocal_pixel_per_angstrom
        )

    def angle_for_frame(self, frame_number: int) -> float:
        return self.starting_angle_deg + self.oscillation_angle_deg * (frame_number - 1)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["detector_distance_mm"] = self.detector_distance_mm
        return data


def find_cred2_file(dataset_dir: Path) -> Path:
    candidates = sorted(dataset_dir.glob("*cRED2*parameters*.txt"))
    if candidates:
        return candidates[0]
    candidates = sorted(dataset_dir.glob("*3D ED*parameters*.txt"))
    if candidates:
        return candidates[0]
    raise FileNotFoundError(
        f"No cRED2 parameter file found in {dataset_dir}. "
        "Expected a file like 'Continuous 3D ED (cRED2) parameters.txt'."
    )


def parse_cred2_parameters(path: str | Path) -> CRED2Parameters:
    path = Path(path)
    raw: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        raw[key.strip().lstrip("#").strip()] = value.strip()

    def val(name: str, default: float | None = None) -> float | None:
        return _first_float(raw.get(name, ""), default)

    def intval(name: str, default: int | None = None) -> int | None:
        return _first_int(raw.get(name, ""), default)

    required = {
        "Starting angle": val("Starting angle"),
        "Pixelsize": val("Pixelsize"),
        "Physical pixelsize": val("Physical pixelsize"),
        "Wavelength": val("Wavelength"),
        "Oscillation angle": val("Oscillation angle"),
        "Number of frames": intval("Number of frames"),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(f"Missing required cRED2 parameter(s): {', '.join(missing)}")

    stretch_amp = None if "None" in raw.get("Stretch amplitude", "") else val("Stretch amplitude")
    stretch_az = None if "None" in raw.get("Stretch azimuth", "") else val("Stretch azimuth")

    return CRED2Parameters(
        path=str(path),
        data_collection_time=raw.get("Data Collection Time"),
        starting_angle_deg=float(required["Starting angle"]),
        ending_angle_deg=val("Ending angle"),
        rotation_range_deg=val("Rotation range"),
        exposure_time_s=val("Exposure Time"),
        acquisition_time_s=val("Acquisition time"),
        total_time_s=val("Total time"),
        high_tension_v=val("High Tension"),
        camera_length_mm=val("Camera length"),
        reciprocal_pixel_per_angstrom=float(required["Pixelsize"]),
        physical_pixel_mm=float(required["Physical pixelsize"]),
        wavelength_angstrom=float(required["Wavelength"]),
        stretch_amplitude_percent=stretch_amp,
        stretch_azimuth_deg=stretch_az,
        rotation_axis_deg=val("Rotation axis"),
        oscillation_angle_deg=float(required["Oscillation angle"]),
        number_of_frames=int(required["Number of frames"]),
        detector_dimensions_px=_dimension_pair(raw),
    )
