"""Minimal SHELX structure parsing and bond inference for the AutoSolve viewer.

The viewer deliberately works from the atoms explicitly present in ``.res`` or
``.ins`` files.  It does not expand symmetry or attempt crystallographic model
editing; that remains the job of a full program such as Olex2.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import math
import re

import numpy as np


# Single-bond covalent radii in angstrom, principally from Cordero et al.,
# Dalton Trans. 2008, 2832-2838 (DOI: 10.1039/B801115J).  High-spin values are
# used for Mn/Fe/Co because they are the safer visual bond-search defaults.
COVALENT_RADII: dict[str, float] = {
    "H": 0.31, "He": 0.28, "Li": 1.28, "Be": 0.96, "B": 0.84,
    "C": 0.76, "N": 0.71, "O": 0.66, "F": 0.57, "Ne": 0.58,
    "Na": 1.66, "Mg": 1.41, "Al": 1.21, "Si": 1.11, "P": 1.07,
    "S": 1.05, "Cl": 1.02, "Ar": 1.06, "K": 2.03, "Ca": 1.76,
    "Sc": 1.70, "Ti": 1.60, "V": 1.53, "Cr": 1.39, "Mn": 1.61,
    "Fe": 1.52, "Co": 1.50, "Ni": 1.24, "Cu": 1.32, "Zn": 1.22,
    "Ga": 1.22, "Ge": 1.20, "As": 1.19, "Se": 1.20, "Br": 1.20,
    "Kr": 1.16, "Rb": 2.20, "Sr": 1.95, "Y": 1.90, "Zr": 1.75,
    "Nb": 1.64, "Mo": 1.54, "Tc": 1.47, "Ru": 1.46, "Rh": 1.42,
    "Pd": 1.39, "Ag": 1.45, "Cd": 1.44, "In": 1.42, "Sn": 1.39,
    "Sb": 1.39, "Te": 1.38, "I": 1.39, "Xe": 1.40, "Cs": 2.44,
    "Ba": 2.15, "La": 2.07, "Ce": 2.04, "Pr": 2.03, "Nd": 2.01,
    "Pm": 1.99, "Sm": 1.98, "Eu": 1.98, "Gd": 1.96, "Tb": 1.94,
    "Dy": 1.92, "Ho": 1.92, "Er": 1.89, "Tm": 1.90, "Yb": 1.87,
    "Lu": 1.87, "Hf": 1.75, "Ta": 1.70, "W": 1.62, "Re": 1.51,
    "Os": 1.44, "Ir": 1.41, "Pt": 1.36, "Au": 1.36, "Hg": 1.32,
    "Tl": 1.45, "Pb": 1.46, "Bi": 1.48, "Po": 1.40, "At": 1.50,
    "Rn": 1.50, "Fr": 2.60, "Ra": 2.21, "Ac": 2.15, "Th": 2.06,
    "Pa": 2.00, "U": 1.96, "Np": 1.90, "Pu": 1.87, "Am": 1.80,
    "Cm": 1.69,
}


# Jmol/CPK-inspired element colours.  Less common elements fall back to a
# deterministic periodic-table-family colour instead of becoming invisible.
ELEMENT_COLORS: dict[str, str] = {
    "H": "#ffffff", "B": "#ffb5b5", "C": "#909090", "N": "#3050f8",
    "O": "#ff0d0d", "F": "#90e050", "P": "#ff8000", "S": "#ffff30",
    "Cl": "#1ff01f", "Br": "#a62929", "I": "#940094", "Si": "#f0c8a0",
    "Li": "#cc80ff", "Na": "#ab5cf2", "K": "#8f40d4", "Mg": "#8aff00",
    "Ca": "#3dff00", "Ti": "#bfc2c7", "Cr": "#8a99c7", "Mn": "#9c7ac7",
    "Fe": "#e06633", "Co": "#f090a0", "Ni": "#50d050", "Cu": "#c88033",
    "Zn": "#7d80b0", "Ag": "#c0c0c0", "Cd": "#ffd98f", "Pt": "#d0d0e0",
    "Au": "#ffd123", "Hg": "#b8b8d0", "Pb": "#575961", "U": "#008fff",
}

_ELEMENT_RE = re.compile(r"^([A-Za-z]{1,2})")
_SFAC_SYMBOL_RE = re.compile(r"^[A-Za-z]{1,2}$")
_CONTROL_WORDS = {
    "ACTA", "AFIX", "ANIS", "BASF", "BIND", "BLOC", "BOND", "CELL",
    "CONF", "CONN", "DAMP", "DEFS", "DISP", "END", "EQIV", "EXTI",
    "FMAP", "FRAG", "FREE", "FVAR", "GRID", "HKLF", "HTAB", "ISOR",
    "LATT", "LIST", "MERG", "MORE", "MOVE", "OMIT", "PART", "PLAN",
    "REM", "RESI", "SADI", "SAME", "SFAC", "SIMU", "SIZE", "SPEC",
    "SUMP", "SWAT", "SYMM", "TEMP", "TITL", "TWIN", "UNIT", "WGHT",
    "ZERR", "L.S.",
}


@dataclass(frozen=True)
class UnitCell:
    a: float
    b: float
    c: float
    alpha: float
    beta: float
    gamma: float

    def vectors(self) -> np.ndarray:
        """Return Cartesian a, b, c vectors as rows of a 3x3 matrix."""
        alpha, beta, gamma = np.deg2rad([self.alpha, self.beta, self.gamma])
        sin_gamma = math.sin(float(gamma))
        if abs(sin_gamma) < 1e-9:
            raise ValueError("CELL gamma produces a singular unit cell")
        a_vec = np.array([self.a, 0.0, 0.0], dtype=np.float64)
        b_vec = np.array(
            [self.b * math.cos(float(gamma)), self.b * sin_gamma, 0.0],
            dtype=np.float64,
        )
        c_x = self.c * math.cos(float(beta))
        c_y = self.c * (
            math.cos(float(alpha)) - math.cos(float(beta)) * math.cos(float(gamma))
        ) / sin_gamma
        c_z_sq = max(self.c * self.c - c_x * c_x - c_y * c_y, 0.0)
        c_vec = np.array([c_x, c_y, math.sqrt(c_z_sq)], dtype=np.float64)
        return np.vstack([a_vec, b_vec, c_vec])


@dataclass(frozen=True)
class Atom:
    label: str
    element: str
    fractional: tuple[float, float, float]
    cartesian: tuple[float, float, float]


@dataclass(frozen=True)
class Bond:
    first: int
    second: int
    length: float


@dataclass(frozen=True)
class SymmetryOperation:
    matrix: tuple[tuple[float, float, float], ...]
    offset: tuple[float, float, float]

    def apply(self, fractional: tuple[float, float, float]) -> np.ndarray:
        return np.asarray(self.matrix, dtype=np.float64) @ np.asarray(fractional, dtype=np.float64) + np.asarray(
            self.offset, dtype=np.float64
        )


@dataclass(frozen=True)
class StructureModel:
    source: Path
    cell: UnitCell
    atoms: tuple[Atom, ...]
    bonds: tuple[Bond, ...]
    asymmetric_atoms: tuple[Atom, ...]
    symmetry_operations: tuple[SymmetryOperation, ...]
    bond_tolerance: float


def canonical_element(value: str) -> str:
    value = value.strip()
    if not value:
        return "X"
    return value[0].upper() + value[1:].lower()


def element_color(element: str) -> str:
    symbol = canonical_element(element)
    if symbol in ELEMENT_COLORS:
        return ELEMENT_COLORS[symbol]
    # Stable fallback palette for elements not explicitly listed above.
    palette = ("#70c8ff", "#c28cff", "#ff9f5a", "#65d68a", "#e7a8d8", "#d2cf69")
    return palette[sum(ord(char) for char in symbol) % len(palette)]


def covalent_radius(element: str) -> float:
    return COVALENT_RADII.get(canonical_element(element), 0.77)


def infer_bonds(
    atoms: tuple[Atom, ...] | list[Atom],
    tolerance: float = 0.45,
    minimum_distance: float = 0.35,
) -> tuple[Bond, ...]:
    """Infer ordinary bonds from Cartesian distance and covalent radii sums."""
    tolerance = max(0.0, float(tolerance))
    if len(atoms) < 2:
        return ()
    coordinates = np.asarray([atom.cartesian for atom in atoms], dtype=np.float64)
    radii = np.asarray([covalent_radius(atom.element) for atom in atoms], dtype=np.float64)
    bonds: list[Bond] = []
    for first in range(len(atoms)):
        if first + 1 >= len(atoms):
            break
        distances = np.linalg.norm(coordinates[first + 1 :] - coordinates[first], axis=1)
        radii_sums = radii[first] + radii[first + 1 :]
        lower = np.maximum(float(minimum_distance), radii_sums * 0.45)
        bonded = np.flatnonzero((distances >= lower) & (distances <= radii_sums + tolerance))
        bonds.extend(
            Bond(first=first, second=first + 1 + int(offset), length=float(distances[offset]))
            for offset in bonded
        )
    return tuple(bonds)


def _number(value: str) -> float:
    return float(Fraction(value)) if "/" in value else float(value)


def _parse_symmetry_expression(expression: str) -> tuple[tuple[float, float, float], float]:
    coefficients = [0.0, 0.0, 0.0]
    offset = 0.0
    cleaned = expression.upper().replace(" ", "").replace("-", "+-")
    for term in (part for part in cleaned.split("+") if part):
        axis_index = next((index for index, axis in enumerate("XYZ") if axis in term), None)
        if axis_index is None:
            offset += _number(term)
            continue
        axis = "XYZ"[axis_index]
        coefficient_text = term.replace(axis, "")
        if coefficient_text in {"", "+"}:
            coefficient = 1.0
        elif coefficient_text == "-":
            coefficient = -1.0
        else:
            coefficient = _number(coefficient_text)
        coefficients[axis_index] += coefficient
    return tuple(coefficients), offset


def parse_symmetry_operation(text: str) -> SymmetryOperation:
    expressions = [value.strip() for value in text.split(",")]
    if len(expressions) != 3:
        raise ValueError(f"Invalid SYMM operation: {text}")
    rows: list[tuple[float, float, float]] = []
    offsets: list[float] = []
    for expression in expressions:
        row, offset = _parse_symmetry_expression(expression)
        rows.append(row)
        offsets.append(offset)
    return SymmetryOperation(tuple(rows), tuple(offsets))


def _centering_translations(latt: int) -> tuple[tuple[float, float, float], ...]:
    return {
        1: ((0.0, 0.0, 0.0),),
        2: ((0.0, 0.0, 0.0), (0.5, 0.5, 0.5)),
        3: ((0.0, 0.0, 0.0), (2 / 3, 1 / 3, 1 / 3), (1 / 3, 2 / 3, 2 / 3)),
        4: ((0.0, 0.0, 0.0), (0.0, 0.5, 0.5), (0.5, 0.0, 0.5), (0.5, 0.5, 0.0)),
        5: ((0.0, 0.0, 0.0), (0.0, 0.5, 0.5)),
        6: ((0.0, 0.0, 0.0), (0.5, 0.0, 0.5)),
        7: ((0.0, 0.0, 0.0), (0.5, 0.5, 0.0)),
    }.get(abs(int(latt)), ((0.0, 0.0, 0.0),))


def build_symmetry_operations(symm_records: list[str], latt: int) -> tuple[SymmetryOperation, ...]:
    identity = SymmetryOperation(
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        (0.0, 0.0, 0.0),
    )
    primitive = [identity]
    for record in symm_records:
        operation = parse_symmetry_operation(record)
        if operation not in primitive:
            primitive.append(operation)
    if latt > 0:
        primitive.extend(
            SymmetryOperation(
                tuple(tuple(-value for value in row) for row in operation.matrix),
                tuple(-value for value in operation.offset),
            )
            for operation in tuple(primitive)
        )

    operations: list[SymmetryOperation] = []
    seen: set[tuple[float, ...]] = set()
    for operation in primitive:
        for centering in _centering_translations(latt):
            offset = tuple((operation.offset[index] + centering[index]) % 1.0 for index in range(3))
            key = tuple(round(value, 8) for row in operation.matrix for value in row) + tuple(
                round(value, 8) for value in offset
            )
            if key in seen:
                continue
            seen.add(key)
            operations.append(SymmetryOperation(operation.matrix, offset))
    return tuple(operations)


def _atom_key(element: str, fractional: np.ndarray | tuple[float, float, float]) -> tuple[object, ...]:
    return (canonical_element(element),) + tuple(round(float(value), 5) for value in fractional)


def _grow_structure_once(model: StructureModel) -> StructureModel:
    """Add one shell of symmetry/translation-related atoms bonded to the model."""
    if not model.asymmetric_atoms:
        return model
    vectors = model.cell.vectors()
    current = list(model.atoms)
    existing = {_atom_key(atom.element, atom.fractional) for atom in current}
    fractional = np.asarray([atom.fractional for atom in current], dtype=np.float64)
    lower = np.floor(np.min(fractional, axis=0)).astype(int) - 1
    upper = np.ceil(np.max(fractional, axis=0)).astype(int) + 1
    additions: list[Atom] = []
    best_packing: tuple[float, list[Atom]] | None = None
    current_cartesian = np.asarray([atom.cartesian for atom in current], dtype=np.float64)
    current_radii = np.asarray([covalent_radius(atom.element) for atom in current], dtype=np.float64)
    current_center = np.mean(current_cartesian, axis=0)
    base_fractional = np.asarray([atom.fractional for atom in model.asymmetric_atoms], dtype=np.float64)
    base_radii = np.asarray([covalent_radius(atom.element) for atom in model.asymmetric_atoms], dtype=np.float64)

    for operation_index, operation in enumerate(model.symmetry_operations):
        operation_matrix = np.asarray(operation.matrix, dtype=np.float64)
        transformed_group = base_fractional @ operation_matrix.T + np.asarray(operation.offset, dtype=np.float64)
        for tx in range(int(lower[0]), int(upper[0]) + 1):
            for ty in range(int(lower[1]), int(upper[1]) + 1):
                for tz in range(int(lower[2]), int(upper[2]) + 1):
                    shifted = transformed_group + np.array([tx, ty, tz], dtype=np.float64)
                    candidate_indices = [
                        index
                        for index, base in enumerate(model.asymmetric_atoms)
                        if _atom_key(base.element, shifted[index]) not in existing
                    ]
                    if not candidate_indices:
                        continue
                    group_fractional = shifted[candidate_indices]
                    group_cartesian = group_fractional @ vectors
                    group_radii = base_radii[candidate_indices]
                    distances = np.linalg.norm(
                        group_cartesian[:, None, :] - current_cartesian[None, :, :], axis=2
                    )
                    radii_sums = group_radii[:, None] + current_radii[None, :]
                    lower_bond = np.maximum(0.35, radii_sums * 0.45)
                    connected = np.any(
                        (distances >= lower_bond)
                        & (distances <= radii_sums + model.bond_tolerance),
                        axis=1,
                    )
                    group: list[Atom] = []
                    for local_index, base_index in enumerate(candidate_indices):
                        base = model.asymmetric_atoms[base_index]
                        candidate = Atom(
                            label=f"{base.label}#{operation_index + 1}",
                            element=base.element,
                            fractional=tuple(float(value) for value in group_fractional[local_index]),
                            cartesian=tuple(float(value) for value in group_cartesian[local_index]),
                        )
                        group.append(candidate)
                        if not connected[local_index]:
                            continue
                        key = _atom_key(candidate.element, candidate.fractional)
                        if key not in existing:
                            existing.add(key)
                            additions.append(candidate)
                    nearest = float(np.min(distances))
                    if nearest > 0.1:
                        center_distance = float(np.linalg.norm(np.mean(group_cartesian, axis=0) - current_center))
                        score = nearest + center_distance * 1e-3
                        if best_packing is None or score < best_packing[0]:
                            best_packing = (score, group)

    # If the asymmetric unit is already a complete disconnected molecule,
    # Olex-style grow should still visibly extend the packing. Add the nearest
    # whole symmetry/translation mate instead of reporting a no-op.
    if not additions and best_packing is not None:
        _score, nearest_group = best_packing
        for candidate in nearest_group:
            key = _atom_key(candidate.element, candidate.fractional)
            if key not in existing:
                existing.add(key)
                additions.append(candidate)

    atoms = tuple(current + additions)
    return StructureModel(
        source=model.source,
        cell=model.cell,
        atoms=atoms,
        bonds=infer_bonds(atoms, tolerance=model.bond_tolerance),
        asymmetric_atoms=model.asymmetric_atoms,
        symmetry_operations=model.symmetry_operations,
        bond_tolerance=model.bond_tolerance,
    )


def grow_structure(model: StructureModel, steps: int = 1) -> StructureModel:
    """Grow several symmetry/packing shells, returning a new display model."""
    result = model
    for _index in range(max(1, int(steps))):
        grown = _grow_structure_once(result)
        if len(grown.atoms) == len(result.atoms):
            break
        result = grown
    return result


def _logical_lines(text: str) -> list[str]:
    """Join the small subset of SHELX ``=`` continuation lines we need."""
    result: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if pending:
                result.append(pending)
                pending = ""
            continue
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


def parse_shelx(path: Path | str, bond_tolerance: float = 0.45) -> StructureModel:
    """Parse CELL, SFAC and explicit atom records from a SHELX RES/INS file."""
    source = Path(path)
    if source.suffix.lower() not in {".res", ".ins"}:
        raise ValueError("Structure file must have a .res or .ins extension")
    text = source.read_text(encoding="utf-8", errors="replace")
    lines = _logical_lines(text)

    cell: UnitCell | None = None
    sfac: list[str] = []
    symm_records: list[str] = []
    latt = -1
    atom_rows: list[tuple[str, str, tuple[float, float, float]]] = []
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        keyword = parts[0].upper()
        if keyword == "CELL" and len(parts) >= 8:
            try:
                values = [float(value) for value in parts[2:8]]
            except ValueError as exc:
                raise ValueError(f"Invalid CELL record in {source.name}") from exc
            cell = UnitCell(*values)
            continue
        if keyword == "SFAC":
            # SHELXT often writes one compact `SFAC C N O` line, while SHELXL
            # can write one scattering-factor record per line.
            symbols = [canonical_element(value) for value in parts[1:] if _SFAC_SYMBOL_RE.fullmatch(value)]
            for symbol in symbols:
                if symbol not in sfac:
                    sfac.append(symbol)
            continue
        if keyword == "LATT" and len(parts) >= 2:
            try:
                latt = int(float(parts[1]))
            except ValueError:
                pass
            continue
        if keyword == "SYMM" and len(parts) >= 2:
            symm_records.append(line.split(None, 1)[1])
            continue
        if keyword in _CONTROL_WORDS or keyword.startswith("Q"):
            continue
        if len(parts) < 5 or cell is None:
            continue
        try:
            sfac_index = int(float(parts[1]))
            fractional = (float(parts[2]), float(parts[3]), float(parts[4]))
        except ValueError:
            continue
        label_match = _ELEMENT_RE.match(parts[0])
        if not label_match:
            continue
        if 1 <= sfac_index <= len(sfac):
            element = sfac[sfac_index - 1]
        else:
            element = canonical_element(label_match.group(1))
        atom_rows.append((parts[0], element, fractional))

    if cell is None:
        raise ValueError(f"No CELL record found in {source.name}")
    if not atom_rows:
        raise ValueError(f"No atom coordinates found in {source.name}")

    vectors = cell.vectors()
    atoms: list[Atom] = []
    for label, element, fractional in atom_rows:
        cartesian = np.asarray(fractional, dtype=np.float64) @ vectors
        atoms.append(
            Atom(
                label=label,
                element=element,
                fractional=fractional,
                cartesian=tuple(float(value) for value in cartesian),
            )
        )
    atom_tuple = tuple(atoms)
    symmetry_operations = build_symmetry_operations(symm_records, latt)
    return StructureModel(
        source=source,
        cell=cell,
        atoms=atom_tuple,
        bonds=infer_bonds(atom_tuple, tolerance=bond_tolerance),
        asymmetric_atoms=atom_tuple,
        symmetry_operations=symmetry_operations,
        bond_tolerance=float(bond_tolerance),
    )


def unit_cell_edges(cell: UnitCell) -> tuple[np.ndarray, tuple[tuple[int, int], ...]]:
    """Return the eight Cartesian corners and twelve edge index pairs."""
    vectors = cell.vectors()
    corners = np.array(
        [
            x * vectors[0] + y * vectors[1] + z * vectors[2]
            for x, y, z in (
                (0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0),
                (0, 0, 1), (1, 0, 1), (0, 1, 1), (1, 1, 1),
            )
        ],
        dtype=np.float64,
    )
    edges = (
        (0, 1), (0, 2), (1, 3), (2, 3),
        (4, 5), (4, 6), (5, 7), (6, 7),
        (0, 4), (1, 5), (2, 6), (3, 7),
    )
    return corners, edges
