"""Choose an explicit processing reference without mixing XDS and DIALS models."""
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import numpy as np
from .xds import XdsReference, load_xds_reference


@dataclass(frozen=True)
class DialsReference(XdsReference):
    source: str = "DIALS"
    model: dict | None = None

    def apply_geometry(self, geometry):
        model = dict(self.model)
        # A user-specified beam-centre shift remains meaningful in native axes.
        origin = np.asarray(model["detector_origin_mm"], dtype=float)
        for i, key in enumerate(("detector_fast_axis", "detector_slow_axis")):
            centre = (geometry.center_x, geometry.center_y)[i]
            origin += (self.beam_center_px[i] - centre) * model["pixel_size_mm"][i] * np.asarray(model[key])
        model["detector_origin_mm"] = origin.tolist()
        return replace(geometry, native_detector=model, rotation_axis_vector=self.axis_vector,
                       physical_pixel_mm=model["pixel_size_mm"][0])


def load_reference(dataset_dir, source="auto", dials_run=None):
    dataset = Path(dataset_dir).resolve()
    if source == "manual":
        return None
    if source == "xds" or (source == "auto" and not dials_run):
        ref = load_xds_reference(dataset)
        if ref is not None or source == "xds":
            return ref
    if source not in ("auto", "dials"):
        raise ValueError(f"Unknown reference source: {source}")
    if dials_run:
        run = Path(dials_run).resolve()
        # References must come from this acquisition, not another crystal.
        if run.parent.parent != dataset or run.parent.name not in ("AutoDials", "DIALS"):
            raise ValueError("DIALS run must belong to the selected dataset")
    else:
        from AutoDials.service import successful_runs
        run = next((p for p in successful_runs(dataset) if (p / "reference.json").exists()), None)
    if run is None or not (run / "reference.json").is_file():
        if source == "auto":
            return None
        raise ValueError("No DIALS geometry reference. Process or send a result from the AutoDials tab first.")
    data = json.loads((run / "reference.json").read_text(encoding="utf-8"))
    if data.get("schema") != "autocrys.dials.geometry.v1":
        raise ValueError("Unsupported DIALS reference schema")
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "success":
        raise ValueError("DIALS run did not finish successfully")
    basis = np.asarray(data["reciprocal_basis"], dtype=float)
    axes = np.linalg.inv(basis)  # rows of direct basis
    axis = tuple(data["rotation_axis_vector"])
    angle = math.degrees(math.atan2(axis[1], axis[0]))
    return DialsReference(dataset=str(dataset), files={"DIALS": str(run / "reference.json")},
        axis_vector=axis, axis_angle_deg=angle, rotation_axis_deg=angle, rotation_sign=-1., angle_mid_frame=True,
        cell_constants=tuple(data["cell"]), cell_axes=tuple(map(tuple, axes)), reciprocal_basis=tuple(map(tuple, basis)),
        beam_center_px=tuple(data["beam_center_px"]), detector_distance_mm=data["detector_distance_mm"],
        wavelength_angstrom=data["wavelength_angstrom"], space_group_number=data["space_group_number"],
        starting_angle_deg=data["oscillation"][0], oscillation_angle_deg=data["oscillation"][1],
        first_frame=data["image_range"][0], last_frame=data["image_range"][1],
        notes=(data["model"], "Original acquisition numbering and zero-frame positions are preserved."), model=data)
