#!/usr/bin/env python3
"""Shared mechanical helpers for AutoCrys scripts.

These helpers intentionally encode boring, repeatable rules so SKILL.md can
stay focused on judgment: what to run, when to stop, and what to ask.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable


NA = "NA"
SUMMARY_COLUMNS = [
    "Dataset",
    "FirstRun",
    "Recovery",
    "Resolution",
    "Xdsconv",
    "Final",
    "Cell",
    "SG",
    "ISa",
    "Rfactor",
    "Completeness",
    "Output",
    "Notes",
]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"DRY-RUN write {path}", file=sys.stderr)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def is_nonempty(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def compact_cwd_label(cwd: Path) -> str:
    parts = [part for part in cwd.parts if part not in {cwd.anchor, "", "/", "\\"}]
    if len(parts) >= 3 and tuple(parts[-2:]) == ("diff", "p"):
        return "/" + "/".join(parts[-3:])
    if len(parts) >= 2:
        return "/" + "/".join(parts[-2:])
    return cwd.as_posix()


def next_backup_path(path: Path, tag: str) -> Path:
    base = path.with_name(f"{path.name}.bak_{tag}")
    if not base.exists():
        return base
    idx = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak_{tag}_{idx:03d}")
        if not candidate.exists():
            return candidate
        idx += 1


def backup_file(path: Path, tag: str, dry_run: bool = False) -> Path | None:
    if not path.exists():
        return None
    backup = next_backup_path(path, tag)
    if dry_run:
        print(f"DRY-RUN backup {path} -> {backup}", file=sys.stderr)
        return backup
    shutil.copy2(path, backup)
    return backup


def move_to_archive(files: Iterable[Path], archive_dir: Path, dry_run: bool = False) -> int:
    count = 0
    seen: set[Path] = set()
    for path in files:
        if not path.exists() or path.is_dir() or path in seen:
            continue
        seen.add(path)
        dest = archive_dir / path.name
        if dest.exists():
            dest = next_backup_path(dest, "archive_collision")
        if dry_run:
            print(f"DRY-RUN archive {path} -> {dest}", file=sys.stderr)
        else:
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
        count += 1
    return count


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout: int = 900,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    print(f"$ {' '.join(cmd)}  # {compact_cwd_label(cwd)}", file=sys.stderr)
    if dry_run:
        return None
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def external_target_path(executable: Path, target: Path) -> str:
    """Render a local target path for a native executable.

    AutoCrys normally runs inside WSL.  A Windows ``.exe`` cannot open a Linux
    ``/home/...`` argument directly, so convert it to the Windows-accessible
    UNC path with ``wslpath -w``.  Failing loudly is safer than launching the
    application with a path that it will misinterpret.
    """
    resolved = target.expanduser().resolve()
    if executable.suffix.lower() != ".exe" or sys.platform == "win32":
        return str(resolved)
    converter = shutil.which("wslpath")
    if not converter:
        raise RuntimeError("wslpath is required to open a WSL file in a Windows application")
    completed = subprocess.run(
        [converter, "-w", str(resolved)],
        capture_output=True,
        text=True,
        check=False,
    )
    converted = completed.stdout.strip()
    if completed.returncode != 0 or not converted:
        reason = (completed.stderr or completed.stdout or "path conversion failed").strip()
        raise RuntimeError(f"Could not convert WSL path for Windows: {reason}")
    return converted


def read_summary(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_summary(path: Path, rows: list[dict[str, str]], dry_run: bool = False) -> None:
    if dry_run:
        print(f"DRY-RUN write summary {path} ({len(rows)} rows)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, NA) for key in SUMMARY_COLUMNS})


def parse_six_floats(text: str) -> list[float] | None:
    nums = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
    if len(nums) < 6:
        return None
    values = [float(x) for x in nums[:6]]
    if values[0] <= 0 or values[1] <= 0 or values[2] <= 0:
        return None
    return values


def format_cell(values: Iterable[float]) -> str:
    return " ".join(f"{float(v):.4g}" if float(v).is_integer() else f"{float(v):.4f}".rstrip("0").rstrip(".") for v in values)


def parse_keyword_values(text: str, keyword: str, count: int | None = None) -> list[str] | None:
    pattern = re.compile(rf"^\s*!?\s*{re.escape(keyword)}\s*=\s*(.+?)\s*(?:!.*)?$", re.MULTILINE)
    matches = pattern.findall(text)
    if not matches:
        return None
    parts = matches[-1].split()
    if count is not None and len(parts) < count:
        return None
    return parts[:count] if count else parts


def set_keyword(text: str, keyword: str, value: str) -> str:
    """Set a singleton keyword in place and remove stale duplicates."""

    line = f"{keyword}= {value}"
    pattern = re.compile(rf"^\s*!?\s*{re.escape(keyword)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        replaced = False

        def replace(match: re.Match[str]) -> str:
            nonlocal replaced
            if replaced:
                return ""
            replaced = True
            return line

        return pattern.sub(replace, text)
    if not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def disable_keyword(text: str, keyword: str) -> str:
    """Comment every occurrence of an XDS keyword while preserving its value.

    Commenting rather than deleting restores XDS's unconstrained/default state
    and leaves the last value visible for inspection or later reactivation.
    All occurrences are disabled so a stale duplicate cannot remain active.
    """

    pattern = re.compile(
        rf"^\s*!?\s*{re.escape(keyword)}\s*=\s*(.*?)\s*$",
        re.MULTILINE,
    )
    return pattern.sub(lambda match: f"!{keyword}= {match.group(1)}", text)


def set_repeated_keyword_block(text: str, keyword: str, values: list[str]) -> str:
    """Replace the keyword block IN PLACE instead of appending at end.

    XSCALE, XDS, and SHELX programs are order-sensitive:
    INPUT_FILE lines must appear BEFORE general parameters like
    UNIT_CELL_CONSTANTS, not at the end of the file.
    """
    # Find the first matching line position
    pattern = re.compile(rf"^\s*!?\s*{re.escape(keyword)}\s*=.*\n?", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        # No existing keyword block — append at end
        block = "".join(f"{keyword}={value}\n" for value in values)
        if not text.endswith("\n"):
            text += "\n"
        return text + block

    # Find the start and end of the consecutive keyword block
    start = match.start()
    lines = text.splitlines(keepends=True)
    block_start = len("".join(lines[:text[:start].count("\n") + 1])) - 1
    # Simpler: find the contiguous block of keyword= and !keyword= lines
    all_lines = text.splitlines(keepends=True)
    cum = 0
    block_idx_start = 0
    block_idx_end = 0
    found = False
    for i, line in enumerate(all_lines):
        stripped = line.strip()
        if re.match(rf"^!?{re.escape(keyword)}\s*=", stripped):
            if not found:
                block_idx_start = i
                found = True
            block_idx_end = i + 1
        elif found:
            break
        cum += len(line)

    if not found:
        block = "".join(f"{keyword}={value}\n" for value in values)
        if not text.endswith("\n"):
            text += "\n"
        return text + block

    # Remove old block and insert new one at same position
    new_block = "".join(f"{keyword}={value}\n" for value in values)
    before = "".join(all_lines[:block_idx_start])
    after = "".join(all_lines[block_idx_end:])
    return before + new_block + after


def strip_commented_lines(text: str) -> str:
    """Drop comment-only lines (whitespace then '!').

    XDS.INP templates ship placeholder keywords as comments, e.g.
    ``!UNIT_CELL_CONSTANTS= 10 20 30 90 90 90``. Reading those would report a
    fake cell/SG, so keyword readers must ignore them.
    """
    return "\n".join(
        line for line in text.splitlines() if not re.match(r"^\s*!", line)
    )


def parse_xds_inp_cell(path: Path) -> list[float] | None:
    if not path.exists():
        return None
    values = parse_keyword_values(strip_commented_lines(read_text(path)), "UNIT_CELL_CONSTANTS", 6)
    if not values:
        return None
    return parse_six_floats(" ".join(values))


def parse_xds_inp_sg(path: Path) -> str | None:
    if not path.exists():
        return None
    values = parse_keyword_values(strip_commented_lines(read_text(path)), "SPACE_GROUP_NUMBER", 1)
    if not values:
        return None
    return values[0] if values[0].isdigit() and values[0] != "0" else None


def parse_correct_lp_cell(path: Path) -> list[float] | None:
    if not path.exists():
        return None
    text = read_text(path)
    cell: list[float] | None = None
    for match in re.finditer(r"UNIT_CELL_CONSTANTS\s*=\s*(.+)", text):
        parsed = parse_six_floats(match.group(1))
        if parsed:
            cell = parsed
    return cell


def parse_correct_lp_sg(path: Path) -> str | None:
    if not path.exists():
        return None
    text = read_text(path)
    matches = re.findall(r"SPACE_GROUP_NUMBER\s*=\s*(\d+)", text)
    for value in reversed(matches):
        if value != "0":
            return value
    return None


def parse_correct_lp_isa(path: Path) -> str:
    if not path.exists():
        return NA
    lines = read_text(path).splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"^\s*a\s+b\s+ISa\s*$", line):
            for candidate in lines[idx + 1 : idx + 4]:
                nums = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", candidate)
                if nums:
                    return nums[-1]
    return NA


def final_xds_subset_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    text = read_text(path)
    marker = 'STATISTICS OF SAVED DATA SET "XDS_ASCII.HKL"'
    idx = text.rfind(marker)
    section = text[idx:] if idx >= 0 else text
    table_idx = section.rfind("SUBSET OF INTENSITY DATA")
    if table_idx < 0:
        return []
    rows: list[list[str]] = []
    for line in section[table_idx:].splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "total" or re.match(r"^\d+(?:\.\d+)?$", parts[0]):
            rows.append(parts)
    return rows


def parse_correct_lp_stats(path: Path) -> dict[str, str]:
    rows = final_xds_subset_rows(path)
    total = next((row for row in rows if row and row[0] == "total"), None)
    stats = {"Rfactor": NA, "Completeness": NA}
    if total and len(total) >= 6:
        stats["Completeness"] = total[4].rstrip("%")
        stats["Rfactor"] = total[5].rstrip("%")
    stats["ISa"] = parse_correct_lp_isa(path)
    return stats


def choose_resolution_cutoff(path: Path, cap: float = 1.0) -> float | None:
    rows = [row for row in final_xds_subset_rows(path) if row and row[0] != "total"]
    previous_r = None
    for row in rows:
        if len(row) < 8:
            continue
        try:
            resolution = float(row[0])
            rfactor = float(row[5].rstrip("%"))
            isig = float(row[7])
        except ValueError:
            continue
        if rfactor > 100 or (previous_r and rfactor > previous_r * 2 and isig < 2) or (rfactor > 50 and isig < 2):
            return min(resolution, cap)
        previous_r = rfactor
    return None


def parse_composition(text: str) -> list[tuple[str, int]]:
    cleaned = text.replace(",", " ").strip()
    if not cleaned:
        raise ValueError("empty composition")
    tokens = re.findall(r"([A-Z][a-z]?)(\d*)", cleaned)
    rebuilt = " ".join(f"{el}{n}" for el, n in tokens).replace(" ", "")
    compact = re.sub(r"\s+", "", cleaned)
    if not tokens or rebuilt != compact:
        raise ValueError(f"invalid composition: {text}")
    parsed: list[tuple[str, int]] = []
    for element, count_text in tokens:
        count = int(count_text) if count_text else 1
        if count <= 0:
            raise ValueError(f"invalid count for {element}: {count}")
        parsed.append((element, count))
    return parsed


def multiply_composition(items: list[tuple[str, int]], multiplier: int) -> list[tuple[str, int]]:
    if multiplier <= 0:
        raise ValueError("Z must be a positive integer")
    return [(element, count * multiplier) for element, count in items]


def load_sfac_symbols(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(path)
    symbols: set[str] = set()
    for line in read_text(path).splitlines():
        match = re.match(r"^\s*SFAC\s+([A-Z][a-z]?)(?:[-+]?\d*[+-]?)?\b", line)
        if match:
            symbols.add(match.group(1))
            symbols.add(match.group(1).upper())
    return symbols


def validate_elements(items: list[tuple[str, int]], sfac_path: Path) -> None:
    symbols = load_sfac_symbols(sfac_path)
    invalid = [element for element, _ in items if element not in symbols and element.upper() not in symbols]
    if invalid:
        raise ValueError(f"invalid element or SFAC symbol: {', '.join(invalid)}")


def shelx_lines_from_composition(items: list[tuple[str, int]]) -> tuple[str, str]:
    elements: list[str] = []
    counts: list[int] = []
    for element, count in items:
        if element in elements:
            pos = elements.index(element)
            counts[pos] += count
        else:
            elements.append(element)
            counts.append(count)
    return f"SFAC {' '.join(elements)}", f"UNIT {' '.join(str(x) for x in counts)}"


def load_space_group_map(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    data = json.loads(read_text(path))
    return {
        "by_number": {str(k): str(v) for k, v in data.get("by_number", {}).items()},
        "aliases": {str(k): str(v) for k, v in data.get("aliases", {}).items()},
    }


def lookup_shelxt_space_group(value: str, mapping_path: Path) -> str:
    value = str(value).strip()
    mapping = load_space_group_map(mapping_path)
    if value.isdigit():
        found = mapping["by_number"].get(value)
        if found:
            return found
    aliases = mapping["aliases"]
    for candidate in (value, value.replace(" ", ""), value.upper(), value.replace("/", "_")):
        if candidate in aliases:
            return aliases[candidate]
    raise ValueError(f"unmapped space group: {value}")


def find_dataset(root: Path, dataset_name: str) -> Path:
    candidates: list[Path] = []
    for base in [root, root / "Data"]:
        candidate = base / dataset_name
        if candidate.is_dir():
            candidates.append(candidate)
    data_root = root / "Data"
    if data_root.exists():
        for candidate in data_root.rglob(dataset_name):
            if candidate.is_dir() and "autoXDS_old_outputs" not in str(candidate):
                candidates.append(candidate)
    unique = sorted(set(candidates))
    if not unique:
        raise FileNotFoundError(f"dataset not found: {dataset_name}")
    if len(unique) > 1:
        joined = ", ".join(str(x) for x in unique)
        raise RuntimeError(f"multiple datasets named {dataset_name}: {joined}")
    return unique[0]


def find_xds_datasets(root: Path) -> list[Path]:
    datasets: list[Path] = []
    for inp in root.rglob("XDS.INP"):
        if "autoXDS_old_outputs" in str(inp):
            continue
        workdir = inp.parent
        if workdir.name == "p" and workdir.parent.name == "diff":
            datasets.append(workdir.parent.parent)
    return sorted(set(datasets))


def timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")
