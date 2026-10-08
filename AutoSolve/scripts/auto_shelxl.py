#!/usr/bin/env python3
"""Run an isolated SHELXL refinement from an existing SHELXT/Olex2 model.

The source .res and .hkl are never modified.  A new timestamped directory is
created for each run, so a poor early-cycle refinement remains recoverable and
can be inspected in Olex2 alongside its input model.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SHELXL = ROOT / "AutoSolve" / "tools" / "shelxl"
DEFAULT_OLEX2 = Path("/mnt/c/Program Files/Olex2-1.5/olex2.exe")


def _existing_file(value: str, suffix: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"missing_{suffix.lstrip('.')}: {path}")
    if path.suffix.lower() != suffix:
        raise ValueError(f"expected {suffix} file: {path}")
    return path


def _is_q_peak(line: str) -> bool:
    first = line.strip().split(maxsplit=1)
    return bool(first and re.fullmatch(r"Q\d+", first[0], re.IGNORECASE))


def _render_refinement_ins(source: Path, cycles: int, peaks: int) -> tuple[str, int]:
    """Keep the atom model, discard stale Q peaks, and set safe early cycles."""
    kept: list[str] = []
    removed_q = 0
    seen_ls = False
    seen_plan = False
    seen_acta = False

    after_hklf = False
    for raw in source.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if after_hklf:
            if _is_q_peak(line):
                removed_q += 1
            continue
        keyword = line.strip().split(maxsplit=1)[0].upper() if line.strip() else ""
        if keyword == "HKLF":
            after_hklf = True
            continue
        if _is_q_peak(line):
            removed_q += 1
            continue
        if keyword == "L.S.":
            kept.append(f"L.S. {cycles}")
            seen_ls = True
            continue
        if keyword == "PLAN":
            kept.append(f"PLAN {peaks}")
            seen_plan = True
            continue
        if keyword == "ACTA":
            seen_acta = True
        kept.append(line)

    if not seen_ls:
        kept.append(f"L.S. {cycles}")
    if not seen_plan:
        kept.append(f"PLAN {peaks}")
    if not seen_acta:
        # Produce CIF/FCF artefacts for inspection and later validation.
        kept.append("ACTA")
    kept.extend(["HKLF 4", "END", ""])
    return "\n".join(kept), removed_q


def _parse_summary(res_path: Path, lst_path: Path) -> dict[str, object]:
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (res_path, lst_path)
        if path.exists()
    )
    result: dict[str, object] = {}
    r1_matches = list(
        re.finditer(
            r"R1\s*=\s*([0-9.]+)\s+for\s+\d+\s+Fo\s*>\s*4sig\(Fo\)\s+and\s+([0-9.]+)\s+for\s+all\s+\d+\s+data",
            text,
            re.IGNORECASE,
        )
    )
    if r1_matches:
        result["r1_gt"] = float(r1_matches[-1].group(1))
        result["r1_all"] = float(r1_matches[-1].group(2))
    for key, pattern in {
        "wr2": r"wR2\s*=\s*([0-9.]+)",
        "gof": r"GooF\s*=\s*(?:S\s*=\s*)?([0-9.]+)",
        "max_coordinate_shift_angstrom": r"Max\.\s*shift\s*=\s*([+-]?[0-9.]+)\s*A",
    }.items():
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            result[key] = float(matches[-1])
    shift_esd = re.findall(
        r"Mean shift/esd\s*=\s*[+-]?[0-9.]+\s+Maximum\s*=\s*([+-]?[0-9.]+)",
        text,
        re.IGNORECASE,
    )
    if shift_esd:
        result["shift_max"] = abs(float(shift_esd[-1]))
    return result


def _refinement_warnings(stdout: str, lst_path: Path) -> list[str]:
    text = stdout + "\n" + (lst_path.read_text(encoding="utf-8", errors="replace") if lst_path.exists() else "")
    known = {
        "unusual_exti_or_swat": "unusual EXTI or SWAT parameter",
        "unit_atom_count_mismatch": "Cell contents from UNIT instruction and atom list do not agree",
    }
    warnings = [code for code, phrase in known.items() if phrase.lower() in text.lower()]
    npd_counts = re.findall(r"(\d+)\s+atoms NPD", text, re.IGNORECASE)
    if npd_counts and int(npd_counts[-1]) > 0:
        warnings.append("atoms_npd")
    return warnings


def _open_olex2(olex2_bin: Path, res_path: Path) -> str:
    if not olex2_bin.exists():
        return "olex2_not_found"
    target = str(res_path)
    if olex2_bin.suffix.lower() == ".exe":
        converted = subprocess.run(
            ["wslpath", "-w", str(res_path)], capture_output=True, text=True, check=False
        )
        if converted.returncode == 0 and converted.stdout.strip():
            target = converted.stdout.strip()
    subprocess.Popen([str(olex2_bin), target], start_new_session=True)
    return "launched"


def refine(args: argparse.Namespace) -> dict[str, object]:
    source_res = _existing_file(args.res, ".res")
    source_hkl = _existing_file(args.hkl, ".hkl") if args.hkl else source_res.with_suffix(".hkl")
    if not source_hkl.is_file():
        raise ValueError(f"missing_hkl: {source_hkl}")

    basename = args.basename or source_res.stem
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (
        Path(args.outdir).expanduser().resolve()
        if args.outdir
        else ROOT / "log" / "AutoSolve" / "shelxl_refinement" / f"{basename}_{stamp}"
    )
    if output_dir.exists():
        raise ValueError(f"output_exists: {output_dir}")

    shelxl_bin = Path(args.shelxl_bin).expanduser().resolve()
    if not shelxl_bin.is_file():
        raise ValueError(f"shelxl_not_found: {shelxl_bin}")
    if not shelxl_bin.stat().st_mode & 0o111:
        shelxl_bin.chmod(shelxl_bin.stat().st_mode | 0o100)

    if args.dry_run:
        return {
            "status": "dry_run",
            "source_res": str(source_res),
            "source_hkl": str(source_hkl),
            "output_dir": str(output_dir),
            "basename": basename,
            "command": [str(shelxl_bin), basename, f"-t{args.threads}"],
        }

    output_dir.mkdir(parents=True)
    target_hkl = output_dir / f"{basename}.hkl"
    target_ins = output_dir / f"{basename}.ins"
    shutil.copy2(source_hkl, target_hkl)
    rendered, removed_q = _render_refinement_ins(source_res, args.cycles, args.peaks)
    target_ins.write_text(rendered, encoding="utf-8")

    command = [str(shelxl_bin), basename, f"-t{args.threads}"]
    completed = subprocess.run(
        command,
        cwd=output_dir,
        capture_output=True,
        text=True,
        timeout=args.timeout,
        check=False,
    )
    (output_dir / "shelxl.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output_dir / "shelxl.stderr.log").write_text(completed.stderr, encoding="utf-8")

    target_res = output_dir / f"{basename}.res"
    target_lst = output_dir / f"{basename}.lst"
    status = "refined" if completed.returncode == 0 and target_res.exists() else "refinement_failed"
    result: dict[str, object] = {
        "status": status,
        "source_res": str(source_res),
        "source_hkl": str(source_hkl),
        "output_dir": str(output_dir),
        "ins": str(target_ins),
        "hkl": str(target_hkl),
        "res": str(target_res),
        "lst": str(target_lst),
        "returncode": completed.returncode,
        "cycles": args.cycles,
        "peaks": args.peaks,
        "removed_stale_q_peaks": removed_q,
        "command": command,
    }
    result.update(_parse_summary(target_res, target_lst))
    warnings = _refinement_warnings(completed.stdout, target_lst)
    if warnings:
        result["warnings"] = warnings
    shift_max = result.get("shift_max")
    if status == "refined":
        if warnings or (isinstance(shift_max, float) and shift_max > 0.05):
            result["quality"] = "needs_model_work"
            result["next_step"] = "Open the isolated .res in Olex2; inspect Q peaks, atom types, restraints, and rerun short cycles."
        else:
            result["quality"] = "converged_candidate"
    if args.open_olex2 and status == "refined":
        result["olex2"] = _open_olex2(Path(args.olex2_bin), target_res)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated SHELXL refinement for AutoCrys initial structures")
    parser.add_argument("--res", required=True, help="Existing SHELXT/Olex2 .res model")
    parser.add_argument("--hkl", help="Reflection .hkl; defaults to the .res sibling")
    parser.add_argument("--outdir", help="New output directory; must not already exist")
    parser.add_argument("--basename", help="Output file basename; default comes from .res")
    parser.add_argument("--cycles", type=int, default=4, help="Early refinement cycles (default: 4)")
    parser.add_argument("--peaks", type=int, default=20, help="Difference peaks to retain (default: 20)")
    parser.add_argument("--threads", type=int, default=4, help="SHELXL threads (default: 4)")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--shelxl-bin", default=str(DEFAULT_SHELXL))
    parser.add_argument("--olex2-bin", default=str(DEFAULT_OLEX2))
    parser.add_argument("--open-olex2", action="store_true", help="Open the successful .res in Olex2")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.cycles < 1 or args.peaks < 0 or args.threads < 1:
        parser.error("--cycles and --threads must be positive; --peaks cannot be negative")
    try:
        result = refine(args)
    except subprocess.TimeoutExpired:
        result = {"status": "refinement_failed", "reason": f"timeout_after_{args.timeout}_seconds"}
    except Exception as exc:
        result = {"status": "input_preparation_failed", "reason": str(exc)}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0 if result.get("status") in {"refined", "dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
