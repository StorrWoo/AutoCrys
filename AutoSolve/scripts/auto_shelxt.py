#!/usr/bin/env python3
"""Script-first AutoSolve runner for deterministic SHELXT preparation.

The skill should decide intent and missing scientific inputs. This script does
the repeatable file work: locate HKL, derive CELL/SG, write INS, run SHELXT,
and classify outputs.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.autocrys_common import (  # noqa: E402
    NA,
    backup_file,
    external_target_path,
    find_dataset,
    format_cell,
    is_nonempty,
    load_space_group_map,
    lookup_shelxt_space_group,
    multiply_composition,
    parse_composition,
    parse_correct_lp_cell,
    parse_correct_lp_sg,
    parse_six_floats,
    parse_xds_inp_cell,
    parse_xds_inp_sg,
    read_summary,
    run_command,
    shelx_lines_from_composition,
    validate_elements,
    write_text,
)
from AutoDials.results import reflection_metadata


ELECTRON_WAVELENGTH = "0.02508"
FIXED_ZERR = "ZERR 1 0.001 0.001 0.001 0.010 0.010 0.010"
SKIP_ATOM_KEYWORDS = {
    "ACTA",
    "BASF",
    "BIND",
    "BOND",
    "CELL",
    "CONF",
    "END",
    "EXTI",
    "FMAP",
    "FVAR",
    "HKLF",
    "LATT",
    "LIST",
    "L.S.",
    "MERG",
    "MORE",
    "OMIT",
    "PART",
    "PLAN",
    "REM",
    "SFAC",
    "SIZE",
    "SYMM",
    "TEMP",
    "TITL",
    "UNIT",
    "WGHT",
    "ZERR",
}


def resolve_root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else ROOT


def project_root_for(search_root: Path) -> Path:
    for candidate in (search_root, *search_root.parents):
        if (candidate / "AutoSolve" / "templates").is_dir() and (candidate / "AutoSolve" / "lib").is_dir():
            return candidate
    return ROOT


def template_source(root: Path) -> Path:
    preferred = root / "AutoSolve" / "templates" / "template.ins"
    fallback = root / "AutoSolve" / "templates" / "template.res"
    if preferred.exists():
        return preferred
    if fallback.exists():
        return fallback
    raise FileNotFoundError("missing template.ins and template.res in AutoSolve/templates")


def summary_candidates(root: Path, workdir: Path) -> list[Path]:
    candidates = [root / "Data" / "summary.txt", root / "summary.txt"]
    for parent in [workdir, *workdir.parents]:
        candidates.append(parent / "summary.txt")
    unique: list[Path] = []
    for path in candidates:
        if path not in unique and path.exists():
            unique.append(path)
    return unique


def summary_row(root: Path, workdir: Path, dataset: str, basename: str) -> dict[str, str] | None:
    wanted = {dataset, basename}
    for summary in summary_candidates(root, workdir):
        for row in read_summary(summary):
            if row.get("Dataset") in wanted:
                return row
    return None


def cell_from_sources(args: argparse.Namespace, root: Path, workdir: Path, dataset: str, basename: str) -> tuple[list[float], str]:
    if args.cell:
        parsed = parse_six_floats(args.cell)
        if not parsed:
            raise ValueError("invalid --cell, expected six numeric values")
        return parsed, "user"

    result = reflection_metadata(Path(args.hkl) if getattr(args, "hkl", None) else workdir / f"{basename}.hkl")
    if result:
        return result["cell"], "DIALS summary.json"
    row = summary_row(root, workdir, dataset, basename)
    if row and row.get("Cell") and row["Cell"] != NA:
        parsed = parse_six_floats(row["Cell"])
        if parsed:
            return parsed, "summary.txt"

    parsed = parse_correct_lp_cell(workdir / "CORRECT.LP")
    if parsed:
        return parsed, "CORRECT.LP"

    parsed = parse_xds_inp_cell(workdir / "XDS.INP")
    if parsed:
        return parsed, "XDS.INP"

    raise ValueError("missing_cell")


def sg_from_sources(args: argparse.Namespace, root: Path, workdir: Path, dataset: str, basename: str) -> tuple[str | None, str]:
    if args.no_force_sg:
        return None, "no_force_sg"
    raw = args.sg
    source = "user"
    if not raw:
        result = reflection_metadata(Path(args.hkl) if getattr(args, "hkl", None) else workdir / f"{basename}.hkl")
        if result:
            raw = str(result["space_group_number"])
            source = "DIALS summary.json"
    if not raw:
        row = summary_row(root, workdir, dataset, basename)
        if row and row.get("SG") and row["SG"] != NA:
            raw = row["SG"]
            source = "summary.txt"
    if not raw:
        raw = parse_correct_lp_sg(workdir / "CORRECT.LP")
        source = "CORRECT.LP"
    if not raw:
        raw = parse_xds_inp_sg(workdir / "XDS.INP")
        source = "XDS.INP"
    if not raw:
        raise ValueError("missing_or_unmapped_xds_space_group")
    return lookup_shelxt_space_group(raw, root / "AutoSolve" / "lib" / "shelxt_space_groups.json"), source


def composition_from_args(args: argparse.Namespace, root: Path) -> tuple[list[tuple[str, int]], str]:
    if args.composition:
        items = parse_composition(args.composition)
        source = "unit_cell_contents"
    elif args.formula and args.z:
        items = multiply_composition(parse_composition(args.formula), int(args.z))
        source = "formula_times_z"
    else:
        raise ValueError("missing_composition")
    validate_elements(items, root / "AutoSolve" / "lib" / "SFAC_UCLA_2022.txt")
    return items, source


def resolve_target(args: argparse.Namespace, root: Path) -> dict[str, Path | str]:
    if args.hkl:
        hkl = Path(args.hkl).expanduser().resolve()
        workdir = Path(args.workdir).expanduser().resolve() if args.workdir else hkl.parent
        dataset = args.dataset or hkl.stem
        basename = args.basename or hkl.stem
        return {"hkl": hkl, "workdir": workdir, "dataset": dataset, "basename": basename}

    if args.dataset:
        dataset_path = find_dataset(root, args.dataset)
        workdir = Path(args.workdir).expanduser().resolve() if args.workdir else dataset_path / "diff" / "p"
        hkl = workdir / "temp.hkl"
        basename = args.basename or dataset_path.name
        return {"hkl": hkl, "workdir": workdir, "dataset": dataset_path.name, "basename": basename}

    if args.workdir:
        workdir = Path(args.workdir).expanduser().resolve()
        hkl = workdir / (args.basename + ".hkl" if args.basename else "temp.hkl")
        basename = args.basename or hkl.stem
        return {"hkl": hkl, "workdir": workdir, "dataset": basename, "basename": basename}

    raise ValueError("provide --dataset, --hkl, or --workdir")


def render_ins(
    basename: str,
    cell: list[float],
    sfac_line: str,
    unit_line: str,
    template: Path,
    title_space_group: str | None = None,
    title_space_group_number: str | None = None,
    latt: int | None = None,
    symm: list[str] | None = None,
    wavelength: str = ELECTRON_WAVELENGTH,
) -> str:
    cell_line = f"CELL {wavelength} {format_cell(cell)}"
    if title_space_group and title_space_group_number:
        title_line = f"TITL {basename} in {title_space_group} #{title_space_group_number}"
    else:
        title_line = f"TITL {basename} AutoSolve SHELXT input"
    symmetry_lines: list[str] = []
    if latt is not None:
        # SHELXT on freshly xdsconv-converted ED HKL fails when the space group
        # is forced with -s unless LATT/SYMM give it the Laue-group context.
        symmetry_lines.append(f"LATT {latt}")
        symmetry_lines.extend(f"SYMM {line}" for line in (symm or []))
    return "\n".join(
        [
            title_line,
            "REM Generated by AutoSolve/scripts/auto_shelxt.py",
            f"REM Template source: {template.name}",
            "REM LATT/SYMM (when present) are derived from the target space group",
            "REM so SHELXT -s works reliably on freshly converted ED data.",
            cell_line,
            FIXED_ZERR,
            *symmetry_lines,
            sfac_line,
            unit_line,
            "HKLF 4",
            "END",
            "",
        ]
    )


def symops_for_sg_number(root: Path, number: str | None) -> tuple[int | None, list[str]]:
    """SHELX LATT/SYMM for an International Tables space-group number.

    Reads the gemmi-generated table in AutoSolve/lib/shelx_symops.json (no runtime
    dependency on gemmi). Returns (latt, symm_lines); (None, []) when the
    number is unknown or the table is missing.
    """
    if not number:
        return None, []
    try:
        path = root / "AutoSolve" / "lib" / "shelx_symops.json"
        if not path.exists():
            return None, []
        entry = json.loads(path.read_text(encoding="utf-8")).get("by_number", {}).get(str(number))
        if not entry:
            return None, []
        return int(entry["latt"]), list(entry.get("symm", []))
    except Exception:
        return None, []


def _res_space_group_number(res_path: Path) -> str | None:
    """Parse the International-Tables number from a SHELXT .res header.

    SHELXT writes a line like ``REM Old TITL <name> in R3 #146``.
    """
    if not res_path.exists():
        return None
    text = res_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"#\s*(\d+)", text)
    return match.group(1) if match else None


def normalize_res_lattsymm(res_path: Path, root: Path, dry_run: bool = False) -> str:
    """Make the LATT/SYMM block in a SHELXT .res SHELXL-consistent.

    SHELXT can emit a self-inconsistent symmetry block (e.g. ``LATT -3`` paired
    with rhombohedral-axis ``SYMM Z, X, Y`` / ``SYMM Y, Z, X``). SHELXL then
    refuses the file with ``** INCONSISTENT LATT/SYMM **`` and Olex2 cannot open
    it. We keep SHELXT's LATT (its setting / enantiomorph choice) but replace
    the SYMM operators with the reference operators for that space-group number
    from ``AutoSolve/lib/shelx_symops.json`` (hexagonal-axis settings), so the block is
    consistent.
    """
    if not res_path.exists():
        return "no_res"
    number = _res_space_group_number(res_path)
    latt, symm = symops_for_sg_number(root, number)
    if latt is None or not symm:
        return "no_symops"
    text = res_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    current = None
    for line in lines:
        match = re.match(r"^\s*LATT\s+(-?\d+)", line)
        if match:
            current = int(match.group(1))
            break
    # Preserve SHELXT's setting sign (obverse vs reverse); use reference magnitude.
    sign = -1 if (current is not None and current < 0) else 1
    final_latt = sign * abs(int(latt))
    out: list[str] = []
    inserted = False
    for line in lines:
        if re.match(r"^\s*(LATT|SYMM)\b", line):
            if not inserted:
                out.append(f"LATT {final_latt}")
                out.extend(f"SYMM {op}" for op in symm)
                inserted = True
            continue
        out.append(line)
    if not inserted:
        return "no_latt_block"
    new = "\n".join(out) + "\n"
    if new == text:
        return "unchanged"
    if not dry_run:
        backup_file(res_path, "lattsymm_autoSolve")
        res_path.write_text(new, encoding="utf-8")
    return "patched"


def title_sg_and_number(shelxt_sg: str | None, mapping_path: Path) -> tuple[str | None, str | None]:
    if not shelxt_sg:
        return None, None
    normalized = shelxt_sg.replace(" ", "").replace("_", "/").upper()
    mapping = load_space_group_map(mapping_path)
    found_number = None
    for number, symbol in mapping.get("by_number", {}).items():
        symbol_norm = str(symbol).replace(" ", "").replace("_", "/").upper()
        if symbol_norm == normalized:
            found_number = str(number)
            break
    return shelxt_sg, found_number


def prepare(args: argparse.Namespace) -> dict[str, object]:
    search_root = resolve_root(args.root)
    root = project_root_for(search_root)
    target = resolve_target(args, search_root)
    workdir = Path(target["workdir"])
    hkl_source = Path(target["hkl"])
    dataset = str(target["dataset"])
    basename = str(target["basename"])
    template = template_source(root)

    if not is_nonempty(hkl_source):
        raise FileNotFoundError(f"missing_hkl: {hkl_source}")

    cell, cell_source = cell_from_sources(args, root, workdir, dataset, basename)
    shelxt_sg, sg_source = sg_from_sources(args, root, workdir, dataset, basename)
    title_sg, title_sg_number = title_sg_and_number(shelxt_sg, root / "AutoSolve" / "lib" / "shelxt_space_groups.json")
    latt, symm = symops_for_sg_number(root, title_sg_number)
    dials_metadata = reflection_metadata(hkl_source)
    if dials_metadata and str(title_sg_number) == str(dials_metadata["space_group_number"]) and "latt" in dials_metadata:
        # Retain the actual DIALS setting/origin, including nonstandard settings.
        latt, symm = dials_metadata["latt"], dials_metadata["symm"]
    composition, composition_source = composition_from_args(args, root)
    sfac_line, unit_line = shelx_lines_from_composition(composition)

    target_hkl = workdir / f"{basename}.hkl"
    target_ins = workdir / f"{basename}.ins"

    if hkl_source.resolve() != target_hkl.resolve():
        backup_file(target_hkl, "autoSolve", args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN copy {hkl_source} -> {target_hkl}", file=sys.stderr)
        else:
            shutil.copy2(hkl_source, target_hkl)

    backup_file(target_ins, "autoSolve", args.dry_run)
    write_text(
        target_ins,
        render_ins(
            basename,
            cell,
            sfac_line,
            unit_line,
            template,
            title_space_group=title_sg,
            title_space_group_number=title_sg_number,
            latt=latt,
            symm=symm,
            wavelength=str(dials_metadata.get("wavelength", ELECTRON_WAVELENGTH)) if dials_metadata else ELECTRON_WAVELENGTH,
        ),
        args.dry_run,
    )

    command = ["shelxt", basename]
    if shelxt_sg:
        command.append(f"-s{shelxt_sg}")

    return {
        "status": "prepared",
        "root": str(root),
        "search_root": str(search_root),
        "workdir": str(workdir),
        "dataset": dataset,
        "basename": basename,
        "hkl_source": str(hkl_source),
        "hkl": str(target_hkl),
        "ins": str(target_ins),
        "cell": format_cell(cell),
        "cell_source": cell_source,
        "composition_source": composition_source,
        "sfac": sfac_line.replace("SFAC ", ""),
        "unit": unit_line.replace("UNIT ", ""),
        "space_group_source": sg_source,
        "shelxt_sg": shelxt_sg or NA,
        "latt": latt if latt is not None else NA,
        "symm": symm or [],
        "shelxt_command": printable_shelxt_command(command),
    }


def printable_shelxt_command(command: list[str]) -> str:
    rendered = []
    for part in command:
        if part.startswith("-s"):
            rendered.append(f'-s"{part[2:]}"')
        else:
            rendered.append(part)
    return " ".join(rendered)


def has_atom_model(res_path: Path) -> bool:
    text = res_path.read_text(encoding="utf-8", errors="replace")
    before_hklf = text.split("HKLF", 1)[0]
    for line in before_hklf.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        keyword = parts[0].upper()
        if keyword in SKIP_ATOM_KEYWORDS or keyword.startswith("REM"):
            continue
        if len(parts) >= 6 and parts[1].lstrip("-").isdigit():
            try:
                float(parts[2])
                float(parts[3])
                float(parts[4])
            except ValueError:
                continue
            return True
    return False


def classify(workdir: Path, basename: str) -> dict[str, object]:
    # Primary name
    res = workdir / f"{basename}.res"
    # SHELXT appends _a (or _b, _c, ...) when the original .res exists at launch
    # Try the numbered suffixes in order
    if not is_nonempty(res):
        for suffix in "abcdefghij":
            candidate = workdir / f"{basename}_{suffix}.res"
            if is_nonempty(candidate):
                res = candidate
                break
    lxt = workdir / f"{basename}.lxt"
    lst = workdir / f"{basename}.lst"
    log = lxt if is_nonempty(lxt) else lst if is_nonempty(lst) else None

    if not is_nonempty(res):
        return {"status": "shelxt_failed", "reason": "missing_or_empty_res", "res": str(res), "log": str(log) if log else NA}
    if not log:
        return {"status": "shelxt_failed", "reason": "missing_or_empty_log", "res": str(res), "log": NA}
    if has_atom_model(res):
        return {"status": "solved_success", "reason": "atom_model_before_hklf", "res": str(res), "log": str(log)}
    return {"status": "shelxt_no_solution", "reason": "no_atom_model_before_hklf", "res": str(res), "log": str(log)}


def _shelxt_run(binary: str, basename: str, workdir: Path, sg: str | None, timeout: int, dry_run: bool) -> tuple[list[str], subprocess.CompletedProcess | None]:
    """Run shelxt once; returns (command, completed)."""
    command = [binary, basename]
    if sg:
        command.append(f"-s{sg}")
    completed = run_command(command, workdir, timeout=timeout, dry_run=dry_run)
    return command, completed


def _shelxt_attempt_record(command: list[str], completed: subprocess.CompletedProcess | None, classification: dict[str, object]) -> dict[str, object]:
    return {
        "shelxt_command": printable_shelxt_command(command),
        "returncode": None if completed is None else completed.returncode,
        **classification,
    }


def run_shelxt(args: argparse.Namespace, prepared: dict[str, object] | None = None) -> dict[str, object]:
    search_root = resolve_root(args.root)
    root = project_root_for(search_root)
    target = resolve_target(args, search_root)
    workdir = Path(prepared["workdir"]) if prepared else Path(target["workdir"])
    basename = str(prepared["basename"]) if prepared else str(target["basename"])
    sg = None if args.no_force_sg else args.sg

    if prepared and prepared.get("shelxt_sg") != NA:
        sg = str(prepared["shelxt_sg"])
    elif sg:
        sg = lookup_shelxt_space_group(str(sg), root / "AutoSolve" / "lib" / "shelxt_space_groups.json")

    for ext in [".res", ".lxt", ".lst"]:
        src = workdir / f"{basename}{ext}"
        backup_file(src, "autoSolve", args.dry_run)
        # Remove original after backup so SHELXT doesn't create _a.res
        if src.exists() and not args.dry_run:
            src.unlink()

    command, completed = _shelxt_run(args.shelxt_bin, basename, workdir, sg, args.timeout, args.dry_run)
    attempts: list[dict[str, object]] = []
    result: dict[str, object] = {
        "shelxt_command": printable_shelxt_command(command),
        "returncode": None if completed is None else completed.returncode,
    }

    if args.dry_run:
        result.update({"status": "dry_run", "reason": "not_run"})
        return result

    classification = classify(workdir, basename)
    attempts.append(_shelxt_attempt_record(command, completed, classification))

    # Known SHELXT quirk on freshly xdsconv-converted ED HKL: when the space
    # group is forced with -s, SHELXT bails out with "No satisfactory space
    # group found" and writes no .res at all. Workaround (observed in the
    # wild and reproduced): run SHELXT once WITHOUT -s so it produces a first
    # solution, then retry the forced run - the second attempt succeeds and
    # writes the solution in the requested space group.
    if sg and classification.get("status") != "solved_success":
        stale_res = workdir / f"{basename}.res"
        if stale_res.exists() and not args.dry_run:
            stale_res.unlink()  # priming must write a clean basename.res

        prime_command, prime_completed = _shelxt_run(args.shelxt_bin, basename, workdir, None, args.timeout, args.dry_run)
        prime_classification = classify(workdir, basename)
        attempts.append(_shelxt_attempt_record(prime_command, prime_completed, prime_classification))
        result["priming_run"] = {
            "shelxt_command": printable_shelxt_command(prime_command),
            "returncode": None if prime_completed is None else prime_completed.returncode,
            "status": prime_classification.get("status"),
        }

        if prime_classification.get("status") != "solved_success":
            classification = {
                "status": "shelxt_failed",
                "reason": "no_solution_after_forced_and_priming_runs",
                "res": str(workdir / f"{basename}.res"),
                "log": str(workdir / f"{basename}.lxt"),
            }
        else:
            # basename.res now exists (priming solution); the forced retry will
            # write basename_a.res (SHELXT appends a suffix to avoid clobbering).
            retry_command, retry_completed = _shelxt_run(args.shelxt_bin, basename, workdir, sg, args.timeout, args.dry_run)
            retry_res = workdir / f"{basename}_a.res"
            if is_nonempty(retry_res) and has_atom_model(retry_res):
                classification = {
                    "status": "solved_success",
                    "reason": "solved_after_priming",
                    "res": str(retry_res),
                    "log": str(workdir / f"{basename}.lxt"),
                }
                result["sg_force_retried"] = True
                result["sg_force_retry_command"] = printable_shelxt_command(retry_command)
            else:
                classification = {
                    "status": "shelxt_failed",
                    "reason": "forced_retry_after_priming_failed",
                    "res": str(retry_res),
                    "log": str(workdir / f"{basename}.lxt"),
                }
            attempts.append(_shelxt_attempt_record(retry_command, retry_completed, classification))

    result["attempts"] = attempts
    result.update(classification)

    # Make every produced .res SHELXL/Olex2-openable: SHELXT sometimes writes a
    # self-inconsistent LATT/SYMM block that SHELXL rejects.
    if result.get("status") == "solved_success":
        lattsymm: dict[str, str] = {}
        for res_file in sorted(workdir.glob(f"{basename}*.res")):
            lattsymm[res_file.name] = normalize_res_lattsymm(res_file, root, args.dry_run)
        if lattsymm:
            result["lattsymm_normalized"] = lattsymm

    if args.open_olex2 and result.get("status") == "solved_success":
        result["olex2"] = open_olex2(Path(result["res"]), args.olex2_bin, args.dry_run)
    return result


def open_olex2(res_path: Path, olex2_bin: str, dry_run: bool = False) -> str:
    if dry_run:
        print(f"DRY-RUN open {olex2_bin} {res_path}", file=sys.stderr)
        return "dry_run"
    exe = Path(olex2_bin)
    if not exe.exists():
        return "olex2_not_found"
    target = external_target_path(exe, res_path)
    subprocess.Popen([str(exe), target], start_new_session=True)
    return "launched"


def emit(result: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    for key, value in result.items():
        print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoSolve SHELXT helper")
    parser.add_argument("command", choices=["prepare", "run", "solve", "classify"])
    parser.add_argument("--root", help="AutoCrys root; default is inferred from script location")
    parser.add_argument("--dataset", help="Dataset folder name, e.g. experiment_003")
    parser.add_argument("--hkl", help="Explicit HKL source path")
    parser.add_argument("--workdir", help="Explicit working directory")
    parser.add_argument("--basename", help="Output basename")
    parser.add_argument("--composition", help="Unit-cell contents, e.g. 'Au4 C20 H16 N2'")
    parser.add_argument("--formula", help="Molecular formula; requires --z")
    parser.add_argument("--z", type=int, help="Z multiplier for --formula")
    parser.add_argument("--cell", help="Manual cell: a b c alpha beta gamma")
    parser.add_argument("--sg", help="Space-group number or symbol")
    parser.add_argument("--no-force-sg", action="store_true", help="Do not pass SHELXT -s")
    parser.add_argument("--shelxt-bin", default="shelxt")
    parser.add_argument("--olex2-bin", default="/mnt/c/Program Files/Olex2-1.5/olex2.exe")
    parser.add_argument("--open-olex2", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            emit(prepare(args), args.json)
        elif args.command == "run":
            root = resolve_root(args.root)
            target = resolve_target(args, root)
            emit(run_shelxt(args, {"workdir": str(target["workdir"]), "basename": str(target["basename"])}), args.json)
        elif args.command == "solve":
            prepared = prepare(args)
            result = dict(prepared)
            result.update(run_shelxt(args, prepared))
            emit(result, args.json)
        elif args.command == "classify":
            search_root = resolve_root(args.root)
            target = resolve_target(args, search_root)
            emit(classify(Path(target["workdir"]), str(target["basename"])), args.json)
        return 0
    except Exception as exc:
        result = {"status": "input_preparation_failed", "reason": str(exc)}
        emit(result, args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
