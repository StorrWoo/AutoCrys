#!/usr/bin/env python3
"""Script-first AutoXDS helper.

This is the deterministic layer below AutoXDS/SKILL.md. The skill decides
which datasets and scientific assumptions are valid; this script performs
repeatable file edits, command execution, parsing, summary writing, and merge
file preparation.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.autocrys_common import (  # noqa: E402
    NA,
    SUMMARY_COLUMNS,
    backup_file,
    choose_resolution_cutoff,
    disable_keyword,
    find_dataset,
    find_xds_datasets,
    format_cell,
    is_nonempty,
    move_to_archive,
    parse_correct_lp_cell,
    parse_correct_lp_sg,
    parse_correct_lp_stats,
    parse_keyword_values,
    parse_six_floats,
    parse_xds_inp_cell,
    parse_xds_inp_sg,
    read_summary,
    read_text,
    run_command,
    set_keyword,
    set_repeated_keyword_block,
    timestamp,
    write_summary,
    write_text,
)


ARCHIVE_PATTERNS = [
    "*.HKL",
    "*.LP",
    "*.cbf",
    "XPARM.XDS",
    "GXPARM.XDS",
    "SPOT.XDS",
    "XDS.LP",
    "XDSCONV.INP",
    "XDSCONV.LP",
    "XSCALE.INP",
    "XSCALE.LP",
    "temp.hkl",
]
STANDARD_JOB_LINES = [
    "!JOB= XYCORR INIT COLSPOT IDXREF",
    "!JOB= DEFPIX INTEGRATE CORRECT",
    "!JOB= CORRECT",
]


def resolve_root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else ROOT


def project_root_for(search_root: Path) -> Path:
    for candidate in (search_root, *search_root.parents):
        if (candidate / "AutoXDS" / "templates").is_dir():
            return candidate
    return ROOT


def default_summary_path(root: Path) -> Path:
    if (root / "Data").exists():
        return root / "Data" / "summary.txt"
    return root / "summary.txt"


def dataset_workdir(dataset: Path) -> Path:
    return dataset / "diff" / "p"


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def archive_old_outputs(workdir: Path, dry_run: bool = False) -> int:
    files = []
    for pattern in ARCHIVE_PATTERNS:
        files.extend(workdir.glob(pattern))
    archive = workdir / f"autoXDS_old_outputs_{timestamp()}"
    return move_to_archive(files, archive, dry_run=dry_run)


def reset_jobs(inp: Path, dry_run: bool = False) -> None:
    text = read_text(inp)
    kept = [line for line in text.splitlines() if not re.match(r"^\s*!?\s*JOB\s*=", line)]
    insert_at = 0
    for idx, line in enumerate(kept):
        if "Job control" in line:
            insert_at = idx + 2
            break
    for line in reversed(STANDARD_JOB_LINES):
        kept.insert(insert_at, line)
    write_text(inp, "\n".join(kept) + "\n", dry_run=dry_run)


def activate_job(inp: Path, job_value: str, dry_run: bool = False) -> None:
    text = read_text(inp)
    kept = [line for line in text.splitlines() if not re.match(r"^\s*!?\s*JOB\s*=", line)]
    insert_at = 0
    for idx, line in enumerate(kept):
        if "Job control" in line:
            insert_at = idx + 2
            break
    kept.insert(insert_at, f"JOB= {job_value}")
    write_text(inp, "\n".join(kept) + "\n", dry_run=dry_run)


def update_xds_inp(
    inp: Path,
    *,
    name_template: str | None = None,
    declared_cell: str | None = None,
    declared_sg: str | None = None,
    clear_declared_cell_sg: bool = False,
    resolution: str | None = None,
    dry_run: bool = False,
) -> None:
    text = read_text(inp)
    if name_template:
        text = set_keyword(text, "NAME_TEMPLATE_OF_DATA_FRAMES", name_template)
    if clear_declared_cell_sg and (declared_cell or declared_sg):
        raise ValueError("cannot set and clear the declared cell/SG at the same time")
    if clear_declared_cell_sg:
        text = disable_keyword(text, "UNIT_CELL_CONSTANTS")
        text = disable_keyword(text, "SPACE_GROUP_NUMBER")
    elif declared_cell:
        parsed = parse_six_floats(declared_cell)
        if not parsed:
            raise ValueError("invalid declared cell")
        text = set_keyword(text, "UNIT_CELL_CONSTANTS", format_cell(parsed))
    if declared_sg:
        if not str(declared_sg).isdigit():
            raise ValueError("declared SG must be an XDS space-group number")
        text = set_keyword(text, "SPACE_GROUP_NUMBER", str(declared_sg))
    if resolution:
        values = resolution.split()
        if len(values) == 1:
            values = ["20", values[0]]
        if len(values) != 2:
            raise ValueError("resolution must be HIGH or 'LOW HIGH'")
        text = set_keyword(text, "INCLUDE_RESOLUTION_RANGE", f"{values[0]} {values[1]}")
    write_text(inp, text, dry_run=dry_run)


def requested_resolution_high(resolution: str | None) -> str | None:
    if not resolution:
        return None
    values = resolution.split()
    return values[-1] if values else None


def format_resolution_value(value: str | float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number)) if number >= 10 else f"{number:.1f}"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def xscale_resolution_range(resolution: str | None, default_high: str) -> tuple[str, str]:
    values = resolution.split() if resolution else []
    if not values:
        values = ["20", default_high]
    elif len(values) == 1:
        values = ["20", values[0]]
    if len(values) != 2:
        raise ValueError("resolution must be HIGH or 'LOW HIGH'")
    low = format_resolution_value(values[0])
    high = format_resolution_value(values[1])
    if float(low) <= 0 or float(high) <= 0:
        raise ValueError("resolution limits must be positive")
    return low, high


def xscale_resolution_shells(low: str, high: str) -> str:
    low_value = float(low)
    high_value = float(high)
    shells = [low_value]
    if high_value < 2.0 < low_value:
        shells.append(2.0)
    shells.append(high_value)
    return " ".join(format_resolution_value(value) for value in shells)


def stats_for_workdir(workdir: Path) -> dict[str, str]:
    correct_lp = workdir / "CORRECT.LP"
    cell = parse_correct_lp_cell(correct_lp) or parse_xds_inp_cell(workdir / "XDS.INP")
    sg = parse_correct_lp_sg(correct_lp) or parse_xds_inp_sg(workdir / "XDS.INP")
    stats = parse_correct_lp_stats(correct_lp)
    return {
        "Cell": format_cell(cell) if cell else NA,
        "SG": sg or NA,
        "ISa": stats.get("ISa", NA),
        "Rfactor": stats.get("Rfactor", NA),
        "Completeness": stats.get("Completeness", NA),
    }


def prepare_xdsconv(
    workdir: Path,
    root: Path,
    input_name: str,
    output_name: str,
    *,
    dry_run: bool = False,
) -> Path:
    template = root / "AutoXDS" / "templates" / "XDSCONV.INP"
    if not template.exists():
        raise FileNotFoundError(template)
    target = workdir / "XDSCONV.INP"
    backup_file(target, "xdsconv_autoXDS", dry_run=dry_run)
    if dry_run:
        print(f"DRY-RUN copy {template} -> {target}", file=sys.stderr)
        text = read_text(template)
    else:
        shutil.copy2(template, target)
        text = read_text(target)
    text = set_keyword(text, "INPUT_FILE", input_name)
    text = set_keyword(text, "OUTPUT_FILE", f"{output_name}  SHELX")
    # SHELX/Olex2 should receive individual scaled observations, not unique
    # pre-averaged reflections.  Otherwise the downstream Rint is trivially 0.
    # This only works when XSCALE also emitted MERGE=FALSE data.
    text = set_keyword(text, "MERGE", "FALSE")
    write_text(target, text, dry_run=dry_run)
    return target


def xds_ascii_merge_state(path: Path) -> bool | None:
    """Return True/False from an XDS_ASCII MERGE header, or None if absent."""
    if not path.is_file():
        return None
    with path.open("r", encoding="ascii", errors="replace") as handle:
        for _index, line in zip(range(40), handle):
            match = re.search(r"\bMERGE\s*=\s*(TRUE|FALSE)\b", line, re.IGNORECASE)
            if match:
                return match.group(1).upper() == "TRUE"
            if not line.startswith("!"):
                break
    return None


def process_one(dataset: Path, args: argparse.Namespace, search_root: Path, project_root: Path) -> dict[str, str]:
    name = dataset.name
    workdir = dataset_workdir(dataset)
    inp = workdir / "XDS.INP"
    xds_ascii = workdir / "XDS_ASCII.HKL"
    temp_hkl = workdir / "temp.hkl"
    dataset_hkl = workdir / f"{name}.hkl"
    correct_lp = workdir / "CORRECT.LP"

    if not inp.exists():
        return failure_row(name, "missing_xds_inp")

    if args.dry_run:
        return {
            **base_row(name),
            "FirstRun": "dry_run",
            "Final": "planned",
            "Output": relative_to_root(temp_hkl, search_root),
            "Notes": f"workdir={relative_to_root(workdir, search_root)}",
        }

    prior_autoxds_run = any(workdir.glob(f"{inp.name}.bak_preprocess_autoXDS*"))
    preserve_original_cell_sg = bool(
        args.preserve_original_cell_sg_on_first_run
        and args.clear_declared_cell_sg
        and not prior_autoxds_run
    )

    if not args.no_archive:
        archive_old_outputs(workdir, dry_run=False)

    backup_file(inp, "preprocess_autoXDS")
    reset_jobs(inp)
    update_xds_inp(
        inp,
        name_template=args.name_template,
        declared_cell=args.declared_cell,
        declared_sg=args.declared_sg,
        clear_declared_cell_sg=args.clear_declared_cell_sg and not preserve_original_cell_sg,
        resolution=args.resolution,
    )

    completed = run_command([args.xds_bin], workdir, timeout=args.timeout)
    out = "" if completed is None else (completed.stdout or "") + (completed.stderr or "")
    first_run = "success_first_run" if is_nonempty(xds_ascii) else "failed_other_reason"
    recovery = NA

    if "INSUFFICIENT PERCENTAGE (< 50%)" in out:
        recovery = "attempted_percentage_below_50"
        backup_file(inp, "recovery_autoXDS")
        activate_job(inp, "DEFPIX INTEGRATE CORRECT")
        completed = run_command([args.xds_bin], workdir, timeout=args.timeout)
        first_run = "success_after_recovery" if is_nonempty(xds_ascii) else "failed_percentage_below_50"

    resolution_status = requested_resolution_high(args.resolution) or "unchanged"
    if is_nonempty(xds_ascii) and not args.skip_resolution:
        cutoff = choose_resolution_cutoff(correct_lp)
        if cutoff is not None:
            backup_file(inp, "resolution_autoXDS")
            update_xds_inp(inp, resolution=f"20 {cutoff:.2f}")
            activate_job(inp, "DEFPIX INTEGRATE CORRECT")
            run_command([args.xds_bin], workdir, timeout=args.timeout)
            resolution_status = f"{cutoff:.2f}"

    xdsconv_status = NA
    if is_nonempty(xds_ascii) and not args.skip_xdsconv:
        prepare_xdsconv(workdir, project_root, "XDS_ASCII.HKL", "temp.hkl")
        run_command([args.xdsconv_bin], workdir, timeout=args.timeout)
        xdsconv_status = "done" if is_nonempty(temp_hkl) else "xdsconv_failed"
        if is_nonempty(temp_hkl):
            backup_file(dataset_hkl, "xdsconv_autoXDS")
            shutil.copy2(temp_hkl, dataset_hkl)

    reset_jobs(inp)
    stats = stats_for_workdir(workdir)
    final = "single_dataset_finished" if is_nonempty(temp_hkl) else "xds_finished" if is_nonempty(xds_ascii) else "failed"
    output = temp_hkl if is_nonempty(temp_hkl) else xds_ascii if is_nonempty(xds_ascii) else workdir
    return {
        **base_row(name),
        "FirstRun": first_run,
        "Recovery": recovery,
        "Resolution": resolution_status,
        "Xdsconv": xdsconv_status,
        "Final": final,
        "Output": relative_to_root(output, search_root),
        "DatasetHkl": relative_to_root(dataset_hkl, search_root) if is_nonempty(dataset_hkl) else NA,
        "Notes": "preserved_original_cell_sg_first_run" if preserve_original_cell_sg else NA,
        **stats,
    }


def base_row(name: str) -> dict[str, str]:
    return {key: NA for key in SUMMARY_COLUMNS} | {"Dataset": name}


def failure_row(name: str, reason: str) -> dict[str, str]:
    return {**base_row(name), "FirstRun": "failed", "Final": "failed", "Notes": reason}


def selected_datasets(root: Path, args: argparse.Namespace) -> list[Path]:
    if args.dataset:
        return [find_dataset(root, name) for name in args.dataset]
    datasets = find_xds_datasets(root)
    if not args.all:
        raise ValueError("provide --dataset NAME or --all")
    return datasets


def process_command(args: argparse.Namespace) -> dict[str, object]:
    search_root = resolve_root(args.root)
    project_root = project_root_for(search_root)
    datasets = selected_datasets(search_root, args)
    rows = [process_one(dataset, args, search_root, project_root) for dataset in datasets]
    summary = Path(args.summary).expanduser().resolve() if args.summary else default_summary_path(search_root)
    existing = [] if args.replace_summary else read_summary(summary)
    merged = existing + rows
    if not args.dry_run:
        backup_file(summary, "autoXDS")
        write_summary(summary, merged)
    return {"status": "processed", "summary": str(summary), "rows": rows}


def list_command(args: argparse.Namespace) -> dict[str, object]:
    root = resolve_root(args.root)
    datasets = find_xds_datasets(root)
    return {
        "status": "ok",
        "root": str(root),
        "datasets": [
            {
                "name": d.name,
                "path": str(d),
                "relative_path": relative_to_root(d, root),
                "workdir": str(dataset_workdir(d)),
            }
            for d in datasets
        ],
    }


def dataset_short_id(name: str) -> str:
    nums = re.findall(r"\d+", name)
    if nums:
        return nums[-1]
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def row_float(row: dict[str, str], key: str) -> float:
    value = row.get(key, NA)
    if value in ("", NA):
        raise ValueError(f"{row.get('Dataset', '?')} missing {key}")
    return float(value)


def load_summary_rows(root: Path, summary_arg: str | None, names: list[str]) -> tuple[Path, list[dict[str, str]]]:
    summary_path = Path(summary_arg).expanduser().resolve() if summary_arg else default_summary_path(root)
    by_name = {row.get("Dataset"): row for row in read_summary(summary_path)}
    rows = []
    for name in names:
        if name not in by_name:
            raise ValueError(f"{name} not found in {summary_path}")
        rows.append(by_name[name])
    return summary_path, rows


def unit_cell_vector(row: dict[str, str]) -> list[float]:
    cell = parse_six_floats(row.get("Cell", ""))
    if not cell:
        raise ValueError(f"{row.get('Dataset', '?')} missing valid Cell")
    a, b, c, alpha, beta, gamma = cell
    return [
        a,
        b,
        c,
        math.sin(math.radians(alpha)),
        math.sin(math.radians(beta)),
        math.sin(math.radians(gamma)),
    ]


def euclidean(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def read_xds_ascii_intensities(path: Path, max_reflections: int | None = None) -> dict[tuple[int, int, int], float]:
    if not is_nonempty(path):
        raise FileNotFoundError(path)
    values: dict[tuple[int, int, int], float] = {}
    for line in read_text(path).splitlines():
        if not line or line.startswith("!"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            h, k, l = int(parts[0]), int(parts[1]), int(parts[2])
            intensity = float(parts[3])
        except ValueError:
            continue
        values[(h, k, l)] = intensity
        if max_reflections and len(values) >= max_reflections:
            break
    if not values:
        raise ValueError(f"no reflection intensities parsed from {path}")
    return values


def infer_dataset_label_from_xds_path(value: str) -> str:
    path = Path(value)
    parts = list(path.parts)
    if len(parts) >= 4 and parts[-1].upper() == "XDS_ASCII.HKL" and parts[-2] == "p" and parts[-3] == "diff":
        return parts[-4]
    return path.stem


def parse_xscale_lp_correlations(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(path)
    lines = read_text(path).splitlines()
    index_to_label: dict[int, str] = {}
    pairwise = []
    in_files = False
    in_corr = False

    for line in lines:
        if "READING INPUT REFLECTION DATA FILES" in line:
            in_files = True
            in_corr = False
            continue
        if "CORRELATIONS BETWEEN INPUT DATA SETS AFTER CORRECTIONS" in line:
            in_corr = True
            in_files = False
            continue
        if line.startswith(" *****") or line.startswith(" *****".strip()):
            if in_files and index_to_label:
                in_files = False

        if in_files:
            parts = line.split()
            if len(parts) >= 5 and parts[0].isdigit():
                idx = int(parts[0])
                filename = parts[-1]
                index_to_label[idx] = infer_dataset_label_from_xds_path(filename)
            continue

        if in_corr:
            parts = line.split()
            if not parts:
                if pairwise:
                    break
                continue
            if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
                i_idx = int(parts[0])
                j_idx = int(parts[1])
                common = int(parts[2])
                cc1_raw = float(parts[3])
                left = index_to_label.get(i_idx, str(i_idx))
                right = index_to_label.get(j_idx, str(j_idx))
                cc1_for_distance = max(0.0, max(-1.0, min(1.0, cc1_raw)))
                pairwise.append(
                    {
                        "i": left,
                        "j": right,
                        "cc1": round(cc1_raw, 6),
                        "cc1_for_distance": round(cc1_for_distance, 6),
                        "common_reflections": common,
                        "distance": round(math.sqrt(max(0.0, 1.0 - cc1_for_distance**2)), 6),
                    }
                )

    if not index_to_label or not pairwise:
        raise ValueError(f"could not parse XSCALE correlations from {path}")
    labels = [index_to_label[i] for i in sorted(index_to_label)]
    return {"labels": labels, "pairwise": pairwise}


def parse_xscale_summary(path: Path, max_lines: int = 90) -> str:
    if not path.exists():
        return ""
    lines = [line.rstrip() for line in read_text(path).splitlines()]
    markers = [
        "STATISTICS OF SCALED OUTPUT DATA SET",
        "SUBSET OF INTENSITY DATA",
        "CORRELATIONS BETWEEN INPUT DATA SETS AFTER CORRECTIONS",
        "READING INPUT REFLECTION DATA FILES",
    ]
    spans: list[str] = []
    for marker in markers:
        for idx, line in enumerate(lines):
            if marker in line:
                chunk = [value for value in lines[idx : idx + 24] if value.strip()]
                if chunk:
                    spans.extend(chunk)
                break
    if not spans:
        spans = [line for line in lines[-max_lines:] if line.strip()]
    compact: list[str] = []
    seen: set[str] = set()
    for line in spans:
        if line not in seen:
            compact.append(line)
            seen.add(line)
        if len(compact) >= max_lines:
            break
    return "\n".join(compact)


def pearson(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("Pearson correlation requires at least two paired values")
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    num = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    den_left = math.sqrt(sum((a - mean_left) ** 2 for a in left))
    den_right = math.sqrt(sum((b - mean_right) ** 2 for b in right))
    if den_left == 0 or den_right == 0:
        raise ValueError("Pearson correlation undefined for constant intensity vector")
    return num / (den_left * den_right)


def hca(
    labels: list[str],
    distances: dict[tuple[str, str], float],
    linkage: str = "average",
    threshold: float | None = None,
) -> dict[str, object]:
    clusters: dict[str, list[str]] = {label: [label] for label in labels}
    active = list(labels)
    merges = []
    step = 1

    def pair_key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a < b else (b, a)

    def cluster_distance(a: str, b: str) -> float:
        pair_values = [
            distances[pair_key(left, right)]
            for left in clusters[a]
            for right in clusters[b]
            if left != right
        ]
        if not pair_values:
            return 0.0
        if linkage == "single":
            return min(pair_values)
        if linkage == "complete":
            return max(pair_values)
        return sum(pair_values) / len(pair_values)

    while len(active) > 1:
        best = None
        for i, left in enumerate(active):
            for right in active[i + 1 :]:
                dist = cluster_distance(left, right)
                if best is None or dist < best[0]:
                    best = (dist, left, right)
        if best is None:
            break
        dist, left, right = best
        new_name = f"cluster_{step}"
        members = clusters[left] + clusters[right]
        merges.append(
            {
                "step": step,
                "left": left,
                "right": right,
                "distance": round(dist, 6),
                "members": members,
            }
        )
        active = [name for name in active if name not in {left, right}]
        active.append(new_name)
        clusters[new_name] = members
        step += 1

    flat = None
    if threshold is not None:
        flat_clusters: dict[str, list[str]] = {label: [label] for label in labels}
        for merge in merges:
            if float(merge["distance"]) > threshold:
                break
            left = str(merge["left"])
            right = str(merge["right"])
            members = list(merge["members"])
            for key in [left, right]:
                flat_clusters.pop(key, None)
            flat_clusters[f"cluster_{merge['step']}"] = members
        flat = list(flat_clusters.values())

    return {"linkage": linkage, "merges": merges, "clusters_at_threshold": flat}


def escape_xml(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_dendrogram_svg(
    labels: list[str],
    hca_result: dict[str, object],
    output: Path,
    *,
    title: str,
    y_label: str,
    threshold: float | None = None,
    width: int = 1000,
    height: int = 620,
) -> Path:
    merges = list(hca_result.get("merges", []))
    output.parent.mkdir(parents=True, exist_ok=True)
    left_margin = 80
    right_margin = 40
    top_margin = 55
    bottom_margin = 140
    plot_w = max(1, width - left_margin - right_margin)
    plot_h = max(1, height - top_margin - bottom_margin)
    max_distance = max([float(m["distance"]) for m in merges] + ([threshold] if threshold is not None else []) + [1.0])
    scale_y = plot_h / max_distance
    step_x = plot_w / max(1, len(labels) - 1)

    node_x = {label: left_margin + idx * step_x for idx, label in enumerate(labels)}
    node_y = {label: 0.0 for label in labels}
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2:.1f}" y="28" text-anchor="middle" font-family="SimSun, Songti SC, serif" font-size="22" font-weight="700">{escape_xml(title)}</text>',
    ]

    def sy(distance: float) -> float:
        return top_margin + (max_distance - distance) * scale_y

    baseline = sy(0.0)
    axis_x = left_margin - 12
    elements.append(f'<line x1="{axis_x}" y1="{top_margin}" x2="{axis_x}" y2="{baseline}" stroke="#333" stroke-width="1"/>')
    elements.append(f'<line x1="{left_margin}" y1="{baseline}" x2="{left_margin + plot_w}" y2="{baseline}" stroke="#333" stroke-width="1"/>')
    for tick in range(5):
        value = max_distance * tick / 4
        y = sy(value)
        elements.append(f'<line x1="{axis_x - 5}" y1="{y:.2f}" x2="{axis_x}" y2="{y:.2f}" stroke="#333" stroke-width="1"/>')
        elements.append(f'<text x="{axis_x - 8}" y="{y + 4:.2f}" text-anchor="end" font-family="SimSun, Songti SC, serif" font-size="11">{value:.3g}</text>')
    elements.append(
        f'<text x="18" y="{top_margin + plot_h / 2:.1f}" transform="rotate(-90 18 {top_margin + plot_h / 2:.1f})" '
        f'text-anchor="middle" font-family="SimSun, Songti SC, serif" font-size="13">{escape_xml(y_label)}</text>'
    )

    if threshold is not None:
        y = sy(threshold)
        elements.append(f'<line x1="{left_margin}" y1="{y:.2f}" x2="{left_margin + plot_w}" y2="{y:.2f}" stroke="#b01818" stroke-width="1.5" stroke-dasharray="6 5"/>')
        elements.append(f'<text x="{left_margin + plot_w - 5}" y="{y - 7:.2f}" text-anchor="end" font-family="SimSun, Songti SC, serif" font-size="12" fill="#b01818">t={threshold:.3g}</text>')

    for merge in merges:
        left = str(merge["left"])
        right = str(merge["right"])
        distance = float(merge["distance"])
        cluster_name = f"cluster_{merge['step']}"
        x1 = node_x[left]
        x2 = node_x[right]
        y1 = sy(node_y[left])
        y2 = sy(node_y[right])
        y = sy(distance)
        color = "#2b5c8a"
        elements.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x1:.2f}" y2="{y:.2f}" stroke="{color}" stroke-width="2"/>')
        elements.append(f'<line x1="{x2:.2f}" y1="{y2:.2f}" x2="{x2:.2f}" y2="{y:.2f}" stroke="{color}" stroke-width="2"/>')
        elements.append(f'<line x1="{x1:.2f}" y1="{y:.2f}" x2="{x2:.2f}" y2="{y:.2f}" stroke="{color}" stroke-width="2"/>')
        node_x[cluster_name] = (x1 + x2) / 2
        node_y[cluster_name] = distance

    for label in labels:
        x = node_x[label]
        elements.append(
            f'<text x="{x:.2f}" y="{baseline + 16:.2f}" transform="rotate(55 {x:.2f} {baseline + 16:.2f})" '
            f'text-anchor="start" font-family="SimSun, Songti SC, serif" font-size="12">{escape_xml(label)}</text>'
        )

    elements.append("</svg>")
    output.write_text("\n".join(elements) + "\n", encoding="utf-8")
    return output


def dendrogram_output_path(base: str | None, method: str, method_count: int, root: Path) -> Path | None:
    if not base:
        return None
    path = Path(base).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if method_count <= 1:
        return path
    suffix = path.suffix or ".svg"
    return path.with_name(f"{path.stem}_{method}{suffix}")


def pairwise_unit_cell_distances(rows: list[dict[str, str]]) -> dict[str, object]:
    labels = [row["Dataset"] for row in rows]
    vectors = {row["Dataset"]: unit_cell_vector(row) for row in rows}
    pairwise = []
    distances: dict[tuple[str, str], float] = {}
    for i, left in enumerate(labels):
        for right in labels[i + 1 :]:
            distance = euclidean(vectors[left], vectors[right])
            distances[(left, right) if left < right else (right, left)] = distance
            pairwise.append({"i": left, "j": right, "distance": round(distance, 6)})
    return {"labels": labels, "pairwise": pairwise, "distances": distances}


def pairwise_cc1_distances(
    root: Path,
    rows: list[dict[str, str]],
    min_common: int,
    max_reflections: int | None,
) -> dict[str, object]:
    # ``min_common`` is retained only for CLI/API compatibility.  The old
    # fixed threshold of 100 rejected small but otherwise usable datasets.
    # Pearson itself still needs two paired, non-constant values.
    del min_common
    labels = [row["Dataset"] for row in rows]
    intensity_maps = {}
    for label in labels:
        dataset = find_dataset(root, label)
        intensity_maps[label] = read_xds_ascii_intensities(
            dataset_workdir(dataset) / "XDS_ASCII.HKL",
            max_reflections=max_reflections,
        )
    pairwise = []
    distances: dict[tuple[str, str], float] = {}
    for i, left in enumerate(labels):
        for right in labels[i + 1 :]:
            common = sorted(set(intensity_maps[left]) & set(intensity_maps[right]))
            if len(common) < 2:
                raise ValueError(
                    f"{left} vs {right}: only {len(common)} common reflections; "
                    "raw Pearson correlation requires at least 2"
                )
            xs = [intensity_maps[left][hkl] for hkl in common]
            ys = [intensity_maps[right][hkl] for hkl in common]
            cc1 = max(-1.0, min(1.0, pearson(xs, ys)))
            cc1_for_distance = max(0.0, cc1)
            distance = math.sqrt(max(0.0, 1.0 - cc1_for_distance**2))
            distances[(left, right) if left < right else (right, left)] = distance
            pairwise.append(
                {
                    "i": left,
                    "j": right,
                    "cc1": round(cc1, 6),
                    "cc1_for_distance": round(cc1_for_distance, 6),
                    "common_reflections": len(common),
                    "distance": round(distance, 6),
                }
            )
    return {"labels": labels, "pairwise": pairwise, "distances": distances, "cc_source": "raw"}


def pairwise_cc1_distances_from_xscale_lp(
    rows: list[dict[str, str]],
    xscale_lp: Path,
    min_common: int,
) -> dict[str, object]:
    # XSCALE has already computed the correlation.  Do not second-guess that
    # result with an arbitrary local common-reflection threshold.
    del min_common
    wanted = [row["Dataset"] for row in rows]
    parsed = parse_xscale_lp_correlations(xscale_lp)
    parsed_labels = list(parsed["labels"])
    pairwise_all = list(parsed["pairwise"])

    if all(label in parsed_labels for label in wanted):
        labels = wanted
    elif len(parsed_labels) == len(wanted):
        # Fallback for cluster dirs where copied filenames no longer contain dataset names.
        remap = dict(zip(parsed_labels, wanted))
        labels = wanted
        for row in pairwise_all:
            row["i"] = remap.get(row["i"], row["i"])
            row["j"] = remap.get(row["j"], row["j"])
    else:
        missing = [label for label in wanted if label not in parsed_labels]
        raise ValueError(f"datasets not found in {xscale_lp}: {missing}")

    label_set = set(labels)
    pairwise = []
    distances: dict[tuple[str, str], float] = {}
    for row in pairwise_all:
        left = str(row["i"])
        right = str(row["j"])
        if left not in label_set or right not in label_set:
            continue
        distance = float(row["distance"])
        distances[(left, right) if left < right else (right, left)] = distance
        pairwise.append(row)

    expected_pairs = len(labels) * (len(labels) - 1) // 2
    if len(pairwise) != expected_pairs:
        raise ValueError(f"{xscale_lp} has {len(pairwise)} selected pairs, expected {expected_pairs}")
    return {"labels": labels, "pairwise": pairwise, "distances": distances, "cc_source": f"xscale-lp:{xscale_lp}"}


def cluster_from_rows(root: Path, rows: list[dict[str, str]], args: argparse.Namespace) -> dict[str, object]:
    methods = ["unit-cell", "cc1"] if args.cluster_method == "both" else [args.cluster_method]
    result: dict[str, object] = {
        "status": "clustered",
        "datasets": [row["Dataset"] for row in rows],
        "methods": {},
    }
    for method in methods:
        if method == "unit-cell":
            calc = pairwise_unit_cell_distances(rows)
        elif method == "cc1":
            if args.cc_source == "xscale-lp":
                if not args.xscale_lp:
                    raise ValueError("--cc-source xscale-lp requires --xscale-lp")
                calc = pairwise_cc1_distances_from_xscale_lp(rows, Path(args.xscale_lp).expanduser().resolve(), args.min_common_reflections)
            else:
                calc = pairwise_cc1_distances(root, rows, args.min_common_reflections, args.max_reflections)
        else:
            raise ValueError(f"unknown cluster method: {method}")
        hca_result = hca(calc["labels"], calc["distances"], args.cluster_linkage, args.cluster_threshold)
        dendrogram_path = dendrogram_output_path(args.dendrogram, method, len(methods), root)
        if dendrogram_path:
            render_dendrogram_svg(
                calc["labels"],
                hca_result,
                dendrogram_path,
                title=f"AutoXDS HCA dendrogram ({method})",
                y_label="Distance",
                threshold=args.cluster_threshold,
                width=args.dendrogram_width,
                height=args.dendrogram_height,
            )
        result["methods"][method] = {
            "formula": "sqrt(da^2+db^2+dc^2+d_sin_alpha^2+d_sin_beta^2+d_sin_gamma^2)"
            if method == "unit-cell"
            else "sqrt(1-CC1_used^2), CC1_used=max(0, CC1), CC1=Pearson(I_i,I_j)",
            "cc_source": calc.get("cc_source") if method == "cc1" else None,
            "pairwise": calc["pairwise"],
            "hca": hca_result,
            "dendrogram": str(dendrogram_path) if dendrogram_path else None,
        }
    return result


def cluster_command(args: argparse.Namespace) -> dict[str, object]:
    root = resolve_root(args.root)
    if not args.dataset or len(args.dataset) < 2:
        raise ValueError("cluster requires at least two --dataset values")
    summary_path, rows = load_summary_rows(root, args.summary, args.dataset)
    result = cluster_from_rows(root, rows, args)
    result["summary"] = str(summary_path)
    return result


def merge_command(args: argparse.Namespace) -> dict[str, object]:
    root = resolve_root(args.root)
    project_root = project_root_for(root)
    if not args.dataset or len(args.dataset) < 2:
        raise ValueError("merge requires at least two --dataset values")

    summary_path, selected = load_summary_rows(root, args.summary, args.dataset)
    cluster_result = None
    if args.cluster != "none":
        cluster_args = argparse.Namespace(
            cluster_method=args.cluster,
            cluster_linkage=args.cluster_linkage,
            cluster_threshold=args.cluster_threshold,
            min_common_reflections=args.min_common_reflections,
            max_reflections=args.max_reflections,
            cc_source=args.cc_source,
            xscale_lp=args.xscale_lp,
            dendrogram=args.dendrogram,
            dendrogram_width=args.dendrogram_width,
            dendrogram_height=args.dendrogram_height,
        )
        cluster_result = cluster_from_rows(root, selected, cluster_args)

    sgs = {row.get("SG", NA) for row in selected}
    if len(sgs) > 1 and not args.allow_sg_mismatch:
        raise ValueError(f"space-group mismatch: {sorted(sgs)}")

    selected.sort(key=lambda row: row_float(row, "Rfactor"))
    best_name = selected[0]["Dataset"]
    best_dataset = find_dataset(root, best_name)
    workdir = dataset_workdir(best_dataset)
    ids = [dataset_short_id(row["Dataset"]) for row in selected]
    merge_name = args.merge_name or "_".join(ids)

    cells = []
    for row in selected:
        parsed = parse_six_floats(row.get("Cell", ""))
        if not parsed:
            raise ValueError(f"{row['Dataset']} missing valid Cell")
        cells.append(parsed)
    average = [sum(values) / len(values) for values in zip(*cells)]
    cell_text = " ".join(f"{value:.2f}" for value in average)
    sg = selected[0].get("SG", NA)

    copied = []
    copied_inputs = []
    for row, short_id in zip(selected, ids):
        dataset = find_dataset(root, row["Dataset"])
        source = dataset_workdir(dataset) / "XDS_ASCII.HKL"
        if not is_nonempty(source):
            raise FileNotFoundError(f"missing XDS_ASCII.HKL for {row['Dataset']}: {source}")
        target = workdir / f"{short_id}.HKL"
        backup_file(target, "merge_autoXDS", args.dry_run)
        if args.dry_run:
            print(f"DRY-RUN copy {source} -> {target}", file=sys.stderr)
        else:
            shutil.copy2(source, target)
        copied.append(target.name)
        copied_inputs.append(
            {
                "dataset": row["Dataset"],
                "source": str(source),
                "target": str(target),
            }
        )

    xscale_template = project_root / "AutoXDS" / "templates" / "XSCALE.INP"
    if not xscale_template.exists():
        raise FileNotFoundError(xscale_template)
    xscale_inp = workdir / "XSCALE.INP"
    backup_file(xscale_inp, "merge_autoXDS", args.dry_run)
    text = read_text(xscale_template)
    default_resolution = (parse_keyword_values(text, "INCLUDE_RESOLUTION_RANGE", 2) or ["20", "1.00"])[1]
    resolution_low, resolution_high = xscale_resolution_range(args.resolution, default_resolution)
    text = set_keyword(text, "OUTPUT_FILE", f"{merge_name}.ahkl")
    # Scale all selected data sets together but retain every observation.  The
    # final SHELX file must remain unmerged so refinement software can compute
    # a meaningful Rint instead of seeing one value per unique reflection.
    text = set_keyword(text, "MERGE", "FALSE")
    text = set_keyword(text, "UNIT_CELL_CONSTANTS", cell_text)
    text = set_keyword(text, "SPACE_GROUP_NUMBER", sg)
    text = set_keyword(text, "INCLUDE_RESOLUTION_RANGE", f"{resolution_low} {resolution_high}")
    text = set_keyword(text, "RESOLUTION_SHELLS", xscale_resolution_shells(resolution_low, resolution_high))
    text = set_repeated_keyword_block(text, "INPUT_FILE", copied)
    write_text(xscale_inp, text, args.dry_run)

    ahkl = workdir / f"{merge_name}.ahkl"
    hkl = workdir / f"{merge_name}.hkl"
    xscale_lp = workdir / "XSCALE.LP"
    status = "merge_prepared"
    if args.run:
        run_command([args.xscale_bin], workdir, timeout=args.timeout, dry_run=args.dry_run)
        if not args.dry_run and not is_nonempty(ahkl):
            status = "merge_xscale_failed"
        elif not args.dry_run and xds_ascii_merge_state(ahkl) is not False:
            # Do not silently convert pre-merged data: once redundancy is lost
            # XDSCONV cannot recreate it and Rint would be meaningless.
            status = "merge_xscale_premerged"
        else:
            prepare_xdsconv(workdir, project_root, ahkl.name, hkl.name, dry_run=args.dry_run)
            run_command([args.xdsconv_bin], workdir, timeout=args.timeout, dry_run=args.dry_run)
            status = "merge_finished" if args.dry_run or is_nonempty(hkl) else "merge_xdsconv_failed"

    return {
        "status": status,
        "merged_datasets": [row["Dataset"] for row in selected],
        "merge_order": merge_name,
        "best_dataset": best_name,
        "workdir": str(workdir),
        "average_cell": cell_text,
        "space_group": sg,
        "resolution": resolution_high,
        "observation_mode": "unmerged",
        "xscale_input": str(xscale_inp),
        "xscale_lp": str(xscale_lp),
        "xscale_summary": parse_xscale_summary(xscale_lp) if args.run and not args.dry_run else "",
        "ahkl": str(ahkl),
        "hkl": str(hkl),
        "copied_inputs": copied_inputs,
        "skipped_singletons": [],
        "cluster": cluster_result,
    }


def set_cell_sg_command(args: argparse.Namespace) -> dict[str, object]:
    root = resolve_root(args.root)
    has_cell = bool(args.declared_cell and args.declared_cell.strip())
    has_sg = bool(args.declared_sg and str(args.declared_sg).strip())
    has_resolution = bool(args.resolution and args.resolution.strip())
    clear_declared = bool(args.clear_declared_cell_sg)
    if has_cell != has_sg:
        raise ValueError("declared cell and SG must be provided together")
    if clear_declared and has_cell:
        raise ValueError("cannot set and clear the declared cell/SG at the same time")
    if not has_cell and not has_resolution and not clear_declared:
        raise ValueError("provide declared cell+SG and/or resolution")
    datasets = selected_datasets(root, args)
    touched = []
    for dataset in datasets:
        inp = dataset_workdir(dataset) / "XDS.INP"
        backup_file(inp, "declared_cell_sg_autoXDS", args.dry_run)
        update_xds_inp(
            inp,
            declared_cell=args.declared_cell,
            declared_sg=args.declared_sg,
            clear_declared_cell_sg=clear_declared,
            resolution=args.resolution,
            dry_run=args.dry_run,
        )
        touched.append(str(inp))
    return {"status": "updated", "files": touched}


def emit(result: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    for key, value in result.items():
        print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoXDS helper")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", help="AutoCrys root or data search root")
        p.add_argument("--json", action="store_true")
        p.add_argument("--dry-run", action="store_true")

    p_list = sub.add_parser("list")
    add_common(p_list)

    p_process = sub.add_parser("process")
    add_common(p_process)
    p_process.add_argument("--dataset", action="append", help="Dataset name; repeatable")
    p_process.add_argument("--all", action="store_true")
    p_process.add_argument("--summary")
    p_process.add_argument("--replace-summary", action="store_true")
    p_process.add_argument("--no-archive", action="store_true")
    p_process.add_argument("--name-template", default="../frame_????.tif   tiff")
    p_process.add_argument("--declared-cell")
    p_process.add_argument("--declared-sg")
    p_process.add_argument("--clear-declared-cell-sg", action="store_true")
    p_process.add_argument(
        "--preserve-original-cell-sg-on-first-run",
        action="store_true",
        help="When clearing constraints, keep the imported XDS.INP cell/SG on its first AutoXDS run.",
    )
    p_process.add_argument("--resolution")
    p_process.add_argument("--skip-resolution", action="store_true")
    p_process.add_argument("--skip-xdsconv", action="store_true")
    p_process.add_argument("--xds-bin", default="xds")
    p_process.add_argument("--xdsconv-bin", default="xdsconv")
    p_process.add_argument("--timeout", type=int, default=1200)

    p_set = sub.add_parser("set-cell-sg")
    add_common(p_set)
    p_set.add_argument("--dataset", action="append")
    p_set.add_argument("--declared-cell")
    p_set.add_argument("--declared-sg")
    p_set.add_argument("--clear-declared-cell-sg", action="store_true")
    p_set.add_argument("--resolution")
    p_set.add_argument("--all", action="store_true")

    p_cluster = sub.add_parser("cluster")
    add_common(p_cluster)
    p_cluster.add_argument("--dataset", action="append", required=True, help="Dataset name; repeatable")
    p_cluster.add_argument("--summary")
    p_cluster.add_argument("--cluster-method", choices=["unit-cell", "cc1", "both"], default="both")
    p_cluster.add_argument("--cluster-linkage", choices=["average", "single", "complete"], default="average")
    p_cluster.add_argument("--cluster-threshold", type=float)
    p_cluster.add_argument(
        "--min-common-reflections",
        type=int,
        default=0,
        help="Deprecated compatibility option; no fixed minimum is enforced",
    )
    p_cluster.add_argument("--max-reflections", type=int)
    p_cluster.add_argument("--cc-source", choices=["raw", "xscale-lp"], default="raw")
    p_cluster.add_argument("--xscale-lp", help="Existing XSCALE.LP to use with --cc-source xscale-lp")
    p_cluster.add_argument("--dendrogram", help="Write dendrogram SVG. With --cluster-method both, method suffixes are added.")
    p_cluster.add_argument("--dendrogram-width", type=int, default=1000)
    p_cluster.add_argument("--dendrogram-height", type=int, default=620)

    p_merge = sub.add_parser("merge")
    add_common(p_merge)
    p_merge.add_argument("--dataset", action="append", required=True)
    p_merge.add_argument("--summary")
    p_merge.add_argument("--merge-name")
    p_merge.add_argument("--resolution")
    p_merge.add_argument("--allow-sg-mismatch", action="store_true")
    p_merge.add_argument("--cluster", choices=["none", "unit-cell", "cc1", "both"], default="none")
    p_merge.add_argument("--cluster-linkage", choices=["average", "single", "complete"], default="average")
    p_merge.add_argument("--cluster-threshold", type=float)
    p_merge.add_argument(
        "--min-common-reflections",
        type=int,
        default=0,
        help="Deprecated compatibility option; no fixed minimum is enforced",
    )
    p_merge.add_argument("--max-reflections", type=int)
    p_merge.add_argument("--cc-source", choices=["raw", "xscale-lp"], default="raw")
    p_merge.add_argument("--xscale-lp", help="Existing XSCALE.LP to use with --cc-source xscale-lp")
    p_merge.add_argument("--dendrogram", help="Write pre-merge clustering dendrogram SVG")
    p_merge.add_argument("--dendrogram-width", type=int, default=1000)
    p_merge.add_argument("--dendrogram-height", type=int, default=620)
    p_merge.add_argument("--run", action="store_true", help="Run xscale and xdsconv after preparing inputs")
    p_merge.add_argument("--xscale-bin", default="xscale")
    p_merge.add_argument("--xdsconv-bin", default="xdsconv")
    p_merge.add_argument("--timeout", type=int, default=1200)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            result = list_command(args)
        elif args.command == "process":
            result = process_command(args)
        elif args.command == "set-cell-sg":
            result = set_cell_sg_command(args)
        elif args.command == "cluster":
            result = cluster_command(args)
        elif args.command == "merge":
            result = merge_command(args)
        else:
            raise ValueError(args.command)
        emit(result, args.json)
        return 0
    except Exception as exc:
        emit({"status": "failed", "reason": str(exc)}, getattr(args, "json", False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
