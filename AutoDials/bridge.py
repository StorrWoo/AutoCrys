"""Export a portable, native DIALS geometry for AutoR3D (no XDS intermediate)."""
import json
from pathlib import Path


def export_reference(run):
    import numpy as np
    from dxtbx.model.experiment_list import ExperimentListFactory
    from dials.array_family import flex
    run = Path(run)
    work = run / "work"
    experiments = ExperimentListFactory.from_json_file(str(work / "integrated.expt"), check_format=False)
    if len(experiments) != 1 or len(experiments[0].detector) != 1:
        raise ValueError("AutoR3D requires one rotation experiment with one detector panel")
    e = experiments[0]
    p, g = e.detector[0], e.goniometer
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    a = np.array(e.crystal.get_A()).reshape(3, 3)
    data = dict(schema="autocrys.dials.geometry.v1", source="DIALS", dataset=manifest["source"],
                model="final static datum (same convention as DIALS reciprocal lattice viewer)",
                cell=e.crystal.get_unit_cell().parameters(), space_group_number=e.crystal.get_space_group().type().number(),
                space_group=str(e.crystal.get_space_group().info()), reciprocal_basis=a.tolist(),
                beam_center_px=[x - 0.5 for x in p.get_beam_centre_px(e.beam.get_s0())],
                detector_distance_mm=p.get_distance(), pixel_size_mm=p.get_pixel_size(),
                detector_origin_mm=p.get_origin(), detector_fast_axis=p.get_fast_axis(), detector_slow_axis=p.get_slow_axis(),
                beam_s0=e.beam.get_s0(), wavelength_angstrom=e.beam.get_wavelength(),
                rotation_axis_vector=g.get_rotation_axis_datum(), setting_rotation=g.get_setting_rotation(),
                fixed_rotation=g.get_fixed_rotation(), oscillation=e.scan.get_oscillation(), image_range=e.scan.get_image_range(),
                acquired_frames=manifest["frames"], zero_frames=manifest["zero_frames"],
                scan_varying_crystal_points=e.crystal.num_scan_points)
    (run / "reference.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Independent numerical reference for testing the NumPy implementation used
    # by AutoR3D against DIALS' own centroid mapping.
    r = flex.reflection_table.from_file(str(work / "refined.refl"))
    r = r.select(r["id"] == 0)
    r.centroid_px_to_mm(experiments)
    r.map_centroids_to_reciprocal_space(experiments)
    px = r["xyzobs.px.value"].as_numpy_array()
    mm = r["xyzobs.mm.value"].as_numpy_array()
    np.savez_compressed(run / "geometry_check.npz", x=px[:, 0] - .5, y=px[:, 1] - .5,
                        angle_deg=np.degrees(mm[:, 2]), q=r["rlp"].as_numpy_array(),
                        miller_index=np.asarray(list(r["miller_index"]), dtype=np.int32))
    return str(run / "reference.json")
