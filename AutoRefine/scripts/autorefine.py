#!/usr/bin/env python3
"""Inspect and run source-preserving SHELXL refinement stages for AutoCrys."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
import traceback


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "Data"
DEFAULT_SHELXL = PROJECT_ROOT / "AutoSolve" / "tools" / "shelxl"
DEFAULT_RUNNER = PROJECT_ROOT / "AutoSolve" / "scripts" / "auto_shelxl.py"
PROFILE_SIO2 = "zeolite-sio2"
R0_SI_O = 1.624
B_SI_O = 0.389


@dataclass(frozen=True)
class Atom:
    label: str
    element: str
    sfac_index: int
    frac: tuple[float, float, float]
    occupancy_code: float
    u_value: float
    u_components: tuple[float, ...]


@dataclass(frozen=True)
class SymOp:
    matrix: tuple[tuple[float, float, float], ...]
    offset: tuple[float, float, float]

    def apply(self, xyz: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = xyz
        first, second, third = self.matrix
        ox, oy, oz = self.offset
        return (
            first[0] * x + first[1] * y + first[2] * z + ox,
            second[0] * x + second[1] * y + second[2] * z + oy,
            third[0] * x + third[1] * y + third[2] * z + oz,
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _joined_shelx_lines(text: str) -> list[str]:
    result: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if pending:
            line = pending + " " + line
            pending = ""
        if line.endswith("="):
            pending = line[:-1].rstrip()
        else:
            result.append(line)
    if pending:
        result.append(pending)
    return result


def _candidate_rank(path: Path, query: str) -> tuple[int, int, str]:
    stem = query[:-4] if query.lower().endswith(".res") else query
    score = 100
    if path.stem.lower() == stem.lower():
        score = 0
    elif path.stem.lower().startswith("cluster_"):
        score = 10
    elif path.stem.lower().startswith("temp"):
        score = 30
    lowered = {part.lower() for part in path.parts}
    if "olex2" in lowered:
        score += 40
    if "originals" in lowered:
        score += 20
    return score, len(path.parts), str(path)


def locate_target(query: str, data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, object]:
    explicit = Path(query).expanduser()
    if explicit.is_file():
        if explicit.suffix.lower() != ".res":
            raise ValueError(f"expected .res file: {explicit}")
        resolved = explicit.resolve()
        return {"query": query, "res": str(resolved), "method": "explicit_path", "candidates": [str(resolved)]}

    if not data_root.is_dir():
        raise ValueError(f"data_root_not_found: {data_root}")

    stem = Path(query).stem.lower()
    candidates = [path.resolve() for path in data_root.rglob("*.res") if path.stem.lower() == stem]
    method = "exact_stem"

    if not candidates:
        match = re.fullmatch(r"(.+)_([a-z])", stem)
        if match:
            dataset, suffix = match.groups()
            dataset_dir = data_root / dataset
            if dataset_dir.is_dir():
                candidates = [
                    path.resolve()
                    for path in dataset_dir.rglob(f"*_{suffix}.res")
                    if "olex2" not in {part.lower() for part in path.parts}
                ]
                method = "dataset_suffix_alias"

    if not candidates:
        raise ValueError(f"no_res_match_for_target: {query}")

    ordered = sorted(set(candidates), key=lambda path: _candidate_rank(path, query))
    best_rank = _candidate_rank(ordered[0], query)[0]
    tied = [path for path in ordered if _candidate_rank(path, query)[0] == best_rank]
    if len(tied) > 1:
        raise ValueError("ambiguous_target: " + ", ".join(str(path) for path in tied))
    return {
        "query": query,
        "res": str(ordered[0]),
        "method": method,
        "candidates": [str(path) for path in ordered],
    }


def _resolve_files(
    target: str | None,
    res_value: str | None,
    hkl_value: str | None,
    data_root: Path,
) -> tuple[Path, Path, dict[str, object]]:
    if bool(target) == bool(res_value):
        raise ValueError("provide exactly one of --target or --res")
    located: dict[str, object]
    if target:
        located = locate_target(target, data_root)
        res = Path(str(located["res"]))
    else:
        res = Path(str(res_value)).expanduser().resolve()
        located = {"query": res_value, "res": str(res), "method": "explicit_res", "candidates": [str(res)]}
    if not res.is_file() or res.suffix.lower() != ".res":
        raise ValueError(f"missing_res: {res}")
    hkl = Path(hkl_value).expanduser().resolve() if hkl_value else res.with_suffix(".hkl")
    if not hkl.is_file() or hkl.suffix.lower() != ".hkl":
        raise ValueError(f"missing_hkl: {hkl}")
    return res, hkl, located


def _parse_number(value: str) -> float:
    value = value.strip().replace("D", "E").replace("d", "e")
    if "/" in value and re.fullmatch(r"[+-]?\d+(?:\.\d+)?/\d+(?:\.\d+)?", value):
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    return float(value)


def _parse_axis_expression(expression: str) -> tuple[tuple[float, float, float], float]:
    compact = expression.upper().replace(" ", "").replace("-", "+-")
    if compact.startswith("+"):
        compact = compact[1:]
    coefficients = [0.0, 0.0, 0.0]
    offset = 0.0
    for term in compact.split("+"):
        if not term:
            continue
        variable = next((axis for axis in "XYZ" if axis in term), None)
        if variable:
            factor = term.replace(variable, "")
            if factor in {"", "+"}:
                value = 1.0
            elif factor == "-":
                value = -1.0
            else:
                value = _parse_number(factor.rstrip("*"))
            coefficients["XYZ".index(variable)] += value
        else:
            offset += _parse_number(term)
    return (coefficients[0], coefficients[1], coefficients[2]), offset


def _parse_symop(value: str) -> SymOp:
    axes = [part.strip() for part in value.split(",")]
    if len(axes) != 3:
        raise ValueError(f"invalid_SYMM: {value}")
    parsed = [_parse_axis_expression(axis) for axis in axes]
    return SymOp(tuple(item[0] for item in parsed), tuple(item[1] for item in parsed))


def _centering_translations(latt: int) -> list[tuple[float, float, float]]:
    return {
        1: [(0, 0, 0)],
        2: [(0, 0, 0), (0.5, 0.5, 0.5)],
        3: [(0, 0, 0), (2 / 3, 1 / 3, 1 / 3), (1 / 3, 2 / 3, 2 / 3)],
        4: [(0, 0, 0), (0, 0.5, 0.5), (0.5, 0, 0.5), (0.5, 0.5, 0)],
        5: [(0, 0, 0), (0, 0.5, 0.5)],
        6: [(0, 0, 0), (0.5, 0, 0.5)],
        7: [(0, 0, 0), (0.5, 0.5, 0)],
    }.get(abs(latt), [(0, 0, 0)])


def _expand_symmetry(base_ops: list[SymOp], latt: int) -> list[SymOp]:
    operations = list(base_ops)
    if latt > 0:
        operations.extend(
            SymOp(tuple(tuple(-value for value in row) for row in op.matrix), tuple(-value for value in op.offset))
            for op in base_ops
        )
    expanded: list[SymOp] = []
    seen: set[tuple[float, ...]] = set()
    for op in operations:
        for translation in _centering_translations(latt):
            offset = tuple(op.offset[i] + translation[i] for i in (0, 1, 2))
            key = tuple(round(value, 8) for row in op.matrix for value in row) + tuple(round(value % 1, 8) for value in offset)
            if key not in seen:
                seen.add(key)
                expanded.append(SymOp(op.matrix, offset))
    return expanded


def parse_model(res: Path) -> dict[str, object]:
    text = _read(res)
    lines = _joined_shelx_lines(text)
    cell: tuple[float, ...] | None = None
    wavelength: float | None = None
    title = ""
    latt = 1
    base_ops = [SymOp(((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0, 0, 0))]
    sfac: list[str] = []
    unit: list[float] = []
    atoms: list[Atom] = []
    atom_section = False

    for line in lines:
        if not line:
            continue
        tokens = line.split()
        keyword = tokens[0].upper()
        if keyword == "TITL":
            title = line[5:].strip()
        elif keyword == "CELL" and len(tokens) >= 8:
            wavelength = _parse_number(tokens[1])
            cell = tuple(_parse_number(value) for value in tokens[2:8])
        elif keyword == "LATT" and len(tokens) >= 2:
            latt = int(float(tokens[1]))
        elif keyword == "SYMM":
            base_ops.append(_parse_symop(line[4:].strip()))
        elif keyword == "SFAC" and len(tokens) >= 2:
            symbolic = [value for value in tokens[1:] if re.fullmatch(r"[A-Za-z]{1,3}", value)]
            if len(symbolic) == len(tokens) - 1:
                sfac.extend(value.title() for value in symbolic)
            else:
                sfac.append(tokens[1].title())
        elif keyword == "UNIT":
            unit = [_parse_number(value) for value in tokens[1:]]
            # SHELXT result files can place atoms directly after UNIT and omit FVAR.
            atom_section = True
        elif keyword == "FVAR":
            atom_section = True
        elif keyword == "HKLF":
            atom_section = False
        elif (
            atom_section
            and len(tokens) >= 7
            and not keyword.startswith("Q")
            and bool(re.search(r"\d", tokens[0]))
        ):
            try:
                sfac_index = int(float(tokens[1]))
                values = [_parse_number(value) for value in tokens[2:]]
            except ValueError:
                continue
            if not 1 <= sfac_index <= len(sfac) or len(values) < 5:
                continue
            components = tuple(values[4:10])
            if len(components) >= 3:
                u_value = statistics.mean(components[:3])
            else:
                u_value = components[0]
            atoms.append(
                Atom(
                    label=tokens[0],
                    element=sfac[sfac_index - 1],
                    sfac_index=sfac_index,
                    frac=(values[0], values[1], values[2]),
                    occupancy_code=values[3],
                    u_value=u_value,
                    u_components=components,
                )
            )

    if cell is None:
        raise ValueError(f"CELL_not_found: {res}")
    symops = _expand_symmetry(base_ops, latt)
    return {
        "title": title,
        "wavelength": wavelength,
        "cell": cell,
        "latt": latt,
        "sfac": sfac,
        "unit": unit,
        "atoms": atoms,
        "symops": symops,
    }


def _frac_to_cart(frac: tuple[float, float, float], cell: tuple[float, ...]) -> tuple[float, float, float]:
    a, b, c, alpha, beta, gamma = cell
    ar, br, gr = (math.radians(value) for value in (alpha, beta, gamma))
    cos_a, cos_b, cos_g = math.cos(ar), math.cos(br), math.cos(gr)
    sin_g = math.sin(gr)
    volume_term = max(0.0, 1 - cos_a**2 - cos_b**2 - cos_g**2 + 2 * cos_a * cos_b * cos_g)
    matrix = (
        (a, b * cos_g, c * cos_b),
        (0.0, b * sin_g, c * (cos_a - cos_b * cos_g) / sin_g),
        (0.0, 0.0, c * math.sqrt(volume_term) / sin_g),
    )
    x, y, z = frac
    first, second, third = matrix
    return (
        first[0] * x + first[1] * y + first[2] * z,
        second[0] * x + second[1] * y + second[2] * z,
        third[0] * x + third[1] * y + third[2] * z,
    )


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _angle(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    denominator = _norm(first) * _norm(second)
    if denominator == 0:
        return float("nan")
    cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(first, second)) / denominator))
    return math.degrees(math.acos(cosine))


def _site_multiplicity(atom: Atom, symops: list[SymOp]) -> int:
    positions = {
        tuple(round(value % 1.0, 6) for value in op.apply(atom.frac))
        for op in symops
    }
    return len(positions)


def _neighbours(
    atom_index: int,
    atoms: list[Atom],
    symops: list[SymOp],
    cell: tuple[float, ...],
    lower: float = 1.35,
    upper: float = 1.90,
) -> list[dict[str, object]]:
    origin = atoms[atom_index]
    origin_cart = _frac_to_cart(origin.frac, cell)
    found: list[dict[str, object]] = []
    seen: set[tuple[str, float, float, float]] = set()
    for neighbour_index, neighbour in enumerate(atoms):
        for sym_index, op in enumerate(symops):
            transformed = op.apply(neighbour.frac)
            for translation in itertools.product((-1, 0, 1), repeat=3):
                frac = (
                    transformed[0] + translation[0],
                    transformed[1] + translation[1],
                    transformed[2] + translation[2],
                )
                cart = _frac_to_cart(frac, cell)
                vector = (
                    cart[0] - origin_cart[0],
                    cart[1] - origin_cart[1],
                    cart[2] - origin_cart[2],
                )
                distance = _norm(vector)
                if distance < 1e-5 or not lower <= distance <= upper:
                    continue
                key = (neighbour.label, *(round(value, 5) for value in vector))
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "label": neighbour.label,
                        "element": neighbour.element,
                        "distance": round(distance, 4),
                        "vector": vector,
                        "symmetry_operation": sym_index + 1,
                        "translation": translation,
                        "atom_index": neighbour_index,
                    }
                )
    return sorted(found, key=lambda item: float(item["distance"]))


def analyse_sio2(model: dict[str, object]) -> dict[str, object]:
    atoms = model["atoms"]
    symops = model["symops"]
    cell = model["cell"]
    assert isinstance(atoms, list) and isinstance(symops, list) and isinstance(cell, tuple)
    sites: list[dict[str, object]] = []
    bonds: dict[tuple[str, str, float], dict[str, object]] = {}
    warnings: list[dict[str, object]] = []
    all_angles: list[dict[str, object]] = []

    for index, atom in enumerate(atoms):
        neighbours = _neighbours(index, atoms, symops, cell)
        candidate = "Si" if len(neighbours) >= 3 else "O" if 1 <= len(neighbours) <= 2 else "unresolved"
        opposite = [item for item in neighbours if {atom.element, str(item["element"])} == {"Si", "O"}]
        bvs = sum(math.exp((R0_SI_O - float(item["distance"])) / B_SI_O) for item in opposite)
        site = {
            "label": atom.label,
            "assigned": atom.element,
            "contact_coordination": len(neighbours),
            "opposite_type_coordination": len(opposite),
            "coordination_candidate": candidate,
            "bond_valence_sum": round(bvs, 3),
            "neighbours": [{key: value for key, value in item.items() if key not in {"vector", "atom_index"}} for item in neighbours],
        }
        sites.append(site)

        expected = 4 if atom.element == "Si" else 2 if atom.element == "O" else None
        if expected is not None and len(opposite) != expected:
            warnings.append({"code": "coordination_mismatch", "atom": atom.label, "assigned": atom.element, "observed": len(opposite), "expected": expected})
        if candidate in {"Si", "O"} and candidate != atom.element:
            warnings.append({"code": "assignment_candidate_mismatch", "atom": atom.label, "assigned": atom.element, "candidate": candidate, "coordination": len(neighbours)})
        if atom.element == "Si" and not 3.4 <= bvs <= 4.6:
            warnings.append({"code": "si_bvs_outlier", "atom": atom.label, "bvs": round(bvs, 3)})
        if atom.element == "O" and not 1.6 <= bvs <= 2.4:
            warnings.append({"code": "o_bvs_outlier", "atom": atom.label, "bvs": round(bvs, 3)})

        for item in opposite:
            distance = float(item["distance"])
            key = tuple(sorted((atom.label, str(item["label"])))) + (round(distance, 4),)
            bonds[key] = {"atom_1": atom.label, "atom_2": item["label"], "distance": distance}
            if atom.element == "Si" and (distance < 1.50 or distance > 1.75):
                severity = "severe" if distance < 1.40 or distance > 1.90 else "warning"
                warnings.append({"code": "si_o_distance_outlier", "atom_1": atom.label, "atom_2": item["label"], "distance": distance, "severity": severity})

        if atom.element == "Si" and len(opposite) >= 2:
            angle_kind, angle_min, angle_max = "O-Si-O", 100.0, 120.0
        elif atom.element == "O" and len(opposite) >= 2:
            angle_kind, angle_min, angle_max = "Si-O-Si", 130.0, 180.0
        else:
            continue
        for first, second in itertools.combinations(opposite, 2):
            angle = _angle(first["vector"], second["vector"])
            record = {"centre": atom.label, "kind": angle_kind, "atom_1": first["label"], "atom_2": second["label"], "angle": round(angle, 2)}
            all_angles.append(record)
            if not angle_min <= angle <= angle_max:
                warnings.append({"code": "angle_outlier", **record, "expected_band": [angle_min, angle_max]})

    bond_values = [float(item["distance"]) for item in bonds.values()]
    return {
        "contact_shell_angstrom": [1.35, 1.90],
        "bond_valence_parameters": {"r0_angstrom": R0_SI_O, "b_angstrom": B_SI_O},
        "site_count": len(sites),
        "sites": sites,
        "si_o_bonds": list(bonds.values()),
        "si_o_distance_summary": {
            "count": len(bond_values),
            "minimum": round(min(bond_values), 4) if bond_values else None,
            "maximum": round(max(bond_values), 4) if bond_values else None,
            "mean": round(statistics.mean(bond_values), 4) if bond_values else None,
        },
        "angles": all_angles,
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def parse_metrics(res: Path, lst: Path | None) -> dict[str, object]:
    res_text = _read(res)
    lst_text = _read(lst)
    text = res_text + "\n" + lst_text
    metrics: dict[str, object] = {}

    r1_matches = list(re.finditer(r"R1\s*=\s*([0-9.]+)\s+for\s+(\d+)\s+Fo\s*>\s*4sig\(Fo\)\s+and\s+([0-9.]+)\s+for\s+all\s+(\d+)\s+data", text, re.I))
    if r1_matches:
        match = r1_matches[-1]
        metrics.update(r1_gt=float(match.group(1)), reflections_gt=int(match.group(2)), r1_all=float(match.group(3)), reflections_all=int(match.group(4)))
    for key, pattern in {
        "wr2": r"wR2\s*=\s*([0-9.]+)",
        "gof": r"GooF\s*=\s*(?:S\s*=\s*)?([0-9.]+)",
    }.items():
        matches = re.findall(pattern, text, re.I)
        if matches:
            metrics[key] = float(matches[-1])

    shift_matches = list(re.finditer(r"Mean shift/esd\s*=\s*([+-]?[0-9.]+)\s+Maximum\s*=\s*([+-]?[0-9.]+)(?:\s+for\s+([^\r\n]+))?", lst_text, re.I))
    if shift_matches:
        match = shift_matches[-1]
        metrics["mean_shift_esd"] = abs(float(match.group(1)))
        metrics["max_shift_esd"] = abs(float(match.group(2)))
        if match.group(3):
            metrics["max_shift_parameter"] = match.group(3).strip()
    coordinate_shifts = re.findall(r"Max\. shift\s*=\s*([+-]?[0-9.]+)\s*A\s+for\s+([^\r\n]+?)\s+Max\. dU", lst_text, re.I)
    if coordinate_shifts:
        metrics["max_coordinate_shift_angstrom"] = abs(float(coordinate_shifts[-1][0]))
        metrics["max_coordinate_shift_atom"] = coordinate_shifts[-1][1].strip()

    peak_matches = list(re.finditer(r"Highest difference peak\s+([+-]?[0-9.]+),?\s+deepest hole\s+([+-]?[0-9.]+)(?:,?\s+1-sigma level\s+([0-9.]+))?", text, re.I))
    if peak_matches:
        match = peak_matches[-1]
        peak, hole = float(match.group(1)), float(match.group(2))
        metrics.update(highest_peak=peak, deepest_hole=hole)
        if match.group(3):
            sigma = float(match.group(3))
            metrics["difference_map_sigma"] = sigma
            if sigma > 0:
                metrics["difference_extreme_sigma"] = round(max(abs(peak), abs(hole)) / sigma, 3)
    for prefix, pattern in {
        "highest_peak": r"Highest peak\s+[+-]?[0-9.]+\s+at\s+([+-]?[0-9.]+)\s+([+-]?[0-9.]+)\s+([+-]?[0-9.]+)\s+\[\s*([0-9.]+)\s+A\s+from\s+(\S+)\s*\]",
        "deepest_hole": r"Deepest hole\s+[+-]?[0-9.]+\s+at\s+([+-]?[0-9.]+)\s+([+-]?[0-9.]+)\s+([+-]?[0-9.]+)\s+\[\s*([0-9.]+)\s+A\s+from\s+(\S+)\s*\]",
    }.items():
        matches = re.findall(pattern, lst_text, re.I)
        if matches:
            x, y, z, distance, atom = matches[-1]
            metrics[f"{prefix}_fractional"] = [float(x), float(y), float(z)]
            metrics[f"{prefix}_nearest_atom"] = atom
            metrics[f"{prefix}_nearest_distance_angstrom"] = float(distance)

    npd = re.findall(r"(\d+)\s+atoms NPD", text, re.I)
    metrics["npd_atoms"] = int(npd[-1]) if npd else 0
    warnings: list[str] = []
    warning_phrases = {
        "exti_or_swat_may_be_required": "Extinction (EXTI) or solvent water (SWAT) correction may be required",
        "weight_parameters_excessive": "Weight parameters refined to",
        "unit_atom_count_mismatch": "Cell contents from UNIT instruction and atom list do not agree",
        "absolute_structure_indeterminate": "Absolute structure cannot be determined",
    }
    for code, phrase in warning_phrases.items():
        if phrase.lower() in text.lower():
            warnings.append(code)
    metrics["warnings"] = warnings
    return metrics


def analyse_adps(atoms: list[Atom]) -> dict[str, object]:
    by_element: dict[str, list[float]] = {}
    for atom in atoms:
        by_element.setdefault(atom.element, []).append(atom.u_value)
    medians = {element: statistics.median(values) for element, values in by_element.items() if values}
    warnings: list[dict[str, object]] = []
    elevated: list[dict[str, object]] = []
    for atom in atoms:
        median = medians.get(atom.element, 0.0)
        if atom.u_value < 0:
            warnings.append({"code": "negative_adp", "severity": "hard", "atom": atom.label, "u": round(atom.u_value, 5)})
        elif atom.u_value < 0.003:
            warnings.append({"code": "very_small_adp", "severity": "warning", "atom": atom.label, "u": round(atom.u_value, 5)})
        elif atom.u_value > 0.15:
            warnings.append({"code": "very_large_adp", "severity": "severe", "atom": atom.label, "u": round(atom.u_value, 5)})
        elif atom.u_value > 0.10:
            warnings.append({"code": "large_adp", "severity": "warning", "atom": atom.label, "u": round(atom.u_value, 5)})
        elif atom.u_value > 0.08:
            elevated.append({"atom": atom.label, "u": round(atom.u_value, 5)})
        if median > 0 and (atom.u_value > 2.5 * median or atom.u_value < 0.4 * median):
            warnings.append({"code": "relative_adp_outlier", "severity": "warning", "atom": atom.label, "u": round(atom.u_value, 5), "element_median": round(median, 5)})
    values = [atom.u_value for atom in atoms]
    return {
        "element_medians": {key: round(value, 5) for key, value in medians.items()},
        "minimum": round(min(values), 5) if values else None,
        "maximum": round(max(values), 5) if values else None,
        "elevated_over_0_08_count": len(elevated),
        "elevated_over_0_08": elevated,
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def classify(metrics: dict[str, object], adps: dict[str, object], geometry: dict[str, object] | None) -> tuple[str, list[str]]:
    reasons: list[str] = []
    adp_warnings = adps.get("warnings", [])
    hard_adp = any(isinstance(item, dict) and item.get("severity") == "hard" for item in adp_warnings)
    if int(metrics.get("npd_atoms", 0)) > 0 or hard_adp:
        return "halt_invalid", ["negative or non-positive-definite displacement parameter"]

    shift = metrics.get("max_shift_esd")
    settled = isinstance(shift, (int, float)) and float(shift) <= 0.01
    poor_fit = False
    if isinstance(metrics.get("r1_gt"), (int, float)) and float(metrics["r1_gt"]) > 0.25:
        poor_fit = True
        reasons.append("R1(gt) is poor for a kinematic ED refinement")
    if isinstance(metrics.get("gof"), (int, float)) and (float(metrics["gof"]) > 1.5 or float(metrics["gof"]) < 0.7):
        poor_fit = True
        reasons.append("GooF indicates a poor model/error/weight fit")
    if isinstance(metrics.get("difference_extreme_sigma"), (int, float)) and float(metrics["difference_extreme_sigma"]) > 5:
        poor_fit = True
        reasons.append("difference-map extreme exceeds 5 sigma")
    model_warnings = int(adps.get("warning_count", 0)) + (int(geometry.get("warning_count", 0)) if geometry else 0)
    if metrics.get("warnings"):
        reasons.append("SHELXL emitted model/data warnings")
    if geometry and int(geometry.get("warning_count", 0)):
        reasons.append("SiO2 topology or geometry needs review")
    if int(adps.get("warning_count", 0)):
        reasons.append("ADP outliers need review")

    if settled and (poor_fit or model_warnings or metrics.get("warnings")):
        return "inspect_model", reasons
    if not settled:
        reasons.insert(0, "least-squares shifts have not reached the final convergence band")
        return "continue_cycles", reasons
    return "final_candidate", ["numerically converged with no detected hard review alerts"]


def inspect_files(res: Path, hkl: Path, located: dict[str, object], profile: str) -> dict[str, object]:
    model = parse_model(res)
    atoms = model["atoms"]
    assert isinstance(atoms, list)
    lst = res.with_suffix(".lst")
    metrics = parse_metrics(res, lst if lst.is_file() else None)
    adps = analyse_adps(atoms)
    geometry = analyse_sio2(model) if profile == PROFILE_SIO2 else None
    symops = model["symops"]
    assert isinstance(symops, list)
    unit_cell_counts: dict[str, int] = {}
    site_multiplicities: dict[str, int] = {}
    for atom in atoms:
        multiplicity = _site_multiplicity(atom, symops)
        site_multiplicities[atom.label] = multiplicity
        unit_cell_counts[atom.element] = unit_cell_counts.get(atom.element, 0) + multiplicity
    sfac = model["sfac"]
    unit = model["unit"]
    assert isinstance(sfac, list) and isinstance(unit, list)
    declared_unit = {
        str(element): float(unit[index])
        for index, element in enumerate(sfac)
        if index < len(unit)
    }
    unit_mismatch = {
        element: {"declared": declared_unit.get(element), "model": count}
        for element, count in unit_cell_counts.items()
        if declared_unit.get(element) is not None and abs(float(declared_unit[element]) - count) > 1e-6
    }
    if unit_mismatch and "unit_atom_count_mismatch" not in metrics["warnings"]:
        metrics["warnings"].append("unit_atom_count_mismatch")
    classification, reasons = classify(metrics, adps, geometry)
    result: dict[str, object] = {
        "status": "inspected",
        "requested_target": located,
        "res": str(res),
        "hkl": str(hkl),
        "lst": str(lst) if lst.is_file() else None,
        "sha256": {"res": sha256(res), "hkl": sha256(hkl)},
        "title": model["title"],
        "data_kind": "electron_diffraction" if isinstance(model["wavelength"], float) and model["wavelength"] < 0.1 else "xray_or_other",
        "wavelength_angstrom": model["wavelength"],
        "cell": model["cell"],
        "latt": model["latt"],
        "symmetry_operation_count": len(model["symops"]),
        "sfac": sfac,
        "unit": unit,
        "declared_unit_cell_counts": declared_unit,
        "model_unit_cell_counts": unit_cell_counts,
        "unit_cell_count_mismatch": unit_mismatch,
        "site_multiplicities": site_multiplicities,
        "atom_count": len(atoms),
        "atom_counts": {element: sum(atom.element == element for atom in atoms) for element in sorted({atom.element for atom in atoms})},
        "metrics": metrics,
        "adp_analysis": adps,
        "classification": classification,
        "classification_reasons": reasons,
    }
    if geometry is not None:
        result["zeolite_sio2_analysis"] = geometry
    if classification == "continue_cycles":
        result["next_action"] = "Run another isolated 2-4 cycle stage, then compare shift and fit metrics."
    elif classification == "inspect_model":
        result["next_action"] = "Do not add blind cycles; review ranked topology, ADP, residual-density, EXTI/weight, and data-quality hypotheses."
    elif classification == "halt_invalid":
        result["next_action"] = "Stop refinement and repair the invalid parameter/model in an isolated copy."
    else:
        result["next_action"] = "Perform final full-matrix and human validation before accepting the model."
    return result


def _run_refinement(args: argparse.Namespace) -> dict[str, object]:
    data_root = Path(args.data_root).expanduser().resolve()
    res, hkl, located = _resolve_files(args.target, args.res, args.hkl, data_root)
    before = {"res": sha256(res), "hkl": sha256(hkl)}
    runner = Path(args.runner).expanduser().resolve()
    shelxl = Path(args.shelxl_bin).expanduser().resolve()
    if not runner.is_file():
        raise ValueError(f"SHELXL_runner_not_found: {runner}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    outdir = (
        Path(args.outdir).expanduser().resolve()
        if args.outdir
        else PROJECT_ROOT / "log" / "AutoRefine" / "runs" / f"{res.stem}_{stamp}"
    )
    command = [
        sys.executable,
        str(runner),
        "--res", str(res),
        "--hkl", str(hkl),
        "--outdir", str(outdir),
        "--cycles", str(args.cycles),
        "--peaks", str(args.peaks),
        "--threads", str(args.threads),
        "--timeout", str(args.timeout),
        "--shelxl-bin", str(shelxl),
        "--json",
    ]
    if args.dry_run:
        command.append("--dry-run")
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=args.timeout + 30, check=False)
    try:
        runner_result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        runner_result = {"status": "refinement_failed", "reason": "runner_returned_non_json", "stdout": completed.stdout, "stderr": completed.stderr}
    after = {"res": sha256(res), "hkl": sha256(hkl)}
    if after != before:
        raise RuntimeError("source_files_changed_during_refinement")
    result: dict[str, object] = {
        "status": runner_result.get("status", "refinement_failed"),
        "requested_target": located,
        "source_sha256_before": before,
        "source_sha256_after": after,
        "source_unchanged": before == after,
        "runner_command": command,
        "runner_returncode": completed.returncode,
        "refinement": runner_result,
    }
    output_res = Path(str(runner_result.get("res", "")))
    output_hkl = Path(str(runner_result.get("hkl", "")))
    if runner_result.get("status") == "refined" and output_res.is_file() and output_hkl.is_file():
        output_location = {"query": str(output_res), "res": str(output_res), "method": "isolated_refinement_output", "candidates": [str(output_res)]}
        result["inspection"] = inspect_files(output_res, output_hkl, output_location, args.profile)
    return result


def _print_result(result: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoRefine: source-preserving SHELXL refinement and diagnosis")
    subparsers = parser.add_subparsers(dest="command", required=True)

    locate = subparsers.add_parser("locate", help="Resolve a dataset alias to a .res model")
    locate.add_argument("target")
    locate.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    locate.add_argument("--json", action="store_true")

    for name in ("inspect", "refine"):
        command = subparsers.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--target", help="Dataset/model alias, e.g. sample3_1_a")
        source.add_argument("--res", help="Explicit .res path")
        command.add_argument("--hkl", help="Explicit .hkl path; defaults to sibling of .res")
        command.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
        command.add_argument("--profile", choices=[PROFILE_SIO2, "generic"], default="generic")
        command.add_argument("--json", action="store_true")

    refine = subparsers.choices["refine"]
    refine.add_argument("--outdir", help="New output directory; must not exist")
    refine.add_argument("--cycles", type=int, default=3)
    refine.add_argument("--peaks", type=int, default=20)
    refine.add_argument("--threads", type=int, default=4)
    refine.add_argument("--timeout", type=int, default=900)
    refine.add_argument("--shelxl-bin", default=str(DEFAULT_SHELXL))
    refine.add_argument("--runner", default=str(DEFAULT_RUNNER))
    refine.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "locate":
            result = {"status": "located", **locate_target(args.target, Path(args.data_root).expanduser().resolve())}
        elif args.command == "inspect":
            res, hkl, located = _resolve_files(args.target, args.res, args.hkl, Path(args.data_root).expanduser().resolve())
            result = inspect_files(res, hkl, located, args.profile)
        else:
            if args.cycles < 1 or args.peaks < 0 or args.threads < 1:
                parser.error("--cycles and --threads must be positive; --peaks cannot be negative")
            result = _run_refinement(args)
    except subprocess.TimeoutExpired:
        result = {"status": "refinement_failed", "reason": "timeout"}
    except Exception as exc:
        result = {
            "status": "input_or_analysis_failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    _print_result(result, args.json)
    return 0 if result.get("status") in {"located", "inspected", "refined", "dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
