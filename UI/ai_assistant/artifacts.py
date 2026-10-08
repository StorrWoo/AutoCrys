"""Evidence-first result inspection for AutoCrys workflows.

The LLM is deliberately not involved here.  These inspectors turn files written
by XDS, AutoR3D and SHELXT into a small, typed report that can be shown in the UI
or supplied to an LLM as grounded context.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any


@dataclass
class Evidence:
    label: str
    value: str
    source: str


@dataclass
class Finding:
    severity: str
    title: str
    detail: str
    suggestion: str = ""
    code: str = ""


@dataclass
class AnalysisReport:
    module: str
    dataset: str
    status: str
    summary: str
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    @property
    def highest_severity(self) -> str:
        rank = {"info": 0, "warning": 1, "error": 2}
        return max((item.severity for item in self.findings), key=lambda x: rank.get(x, 0), default="info")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_display_text(self, compact: bool = False) -> str:
        head = f"{self.module} · {self.dataset} · {self.status}"
        lines = [head, self.summary]
        if self.findings:
            for item in self.findings[: (2 if compact else 8)]:
                icon = {"error": "ERROR", "warning": "WARN", "info": "INFO"}.get(item.severity, "INFO")
                lines.append(f"[{icon}] {item.title}: {item.detail}")
        if self.next_steps:
            lines.append("Next: " + self.next_steps[0] if compact else "Recommended next steps:")
            if not compact:
                lines.extend(f"  {i + 1}. {step}" for i, step in enumerate(self.next_steps[:5]))
        if not compact and self.evidence:
            lines.append("Evidence:")
            lines.extend(f"  {item.label}: {item.value} ({item.source})" for item in self.evidence)
        return "\n".join(lines)

    def to_prompt(self) -> str:
        return "[Artifact analysis — deterministic, treat as evidence]\n" + self.to_display_text(compact=False)


class ArtifactAnalyzer:
    """Inspect the output artifacts for the workflow represented by a UI tab."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def analyze(
        self,
        dataset_path: Path | str | None,
        active_tab: str = "",
        dataset_name: str | None = None,
        summary_rows: dict[str, dict[str, str]] | None = None,
        hints: dict[str, Any] | None = None,
    ) -> AnalysisReport:
        path = self._resolve_path(dataset_path, hints or {})
        tab = active_tab.lower()
        if "autoxds" in tab:
            return self.inspect_xds(path, dataset_name, summary_rows or {})
        if "autosolve" in tab:
            return self.inspect_solve(path, dataset_name, hints or {})
        return self.inspect_r3d(path, dataset_name, hints or {})

    def _resolve_path(self, value: Path | str | None, hints: dict[str, Any]) -> Path | None:
        if value:
            path = Path(value)
            if not path.is_absolute() and hints.get("root"):
                candidate = Path(str(hints["root"])) / path
                if candidate.exists():
                    return candidate
            if not path.is_absolute():
                candidate = self.project_root / path
                if candidate.exists():
                    return candidate
            return path
        for key in ("dataset_path", "workdir", "hkl"):
            if hints.get(key):
                return Path(str(hints[key]))
        return None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _float(value: Any) -> float | None:
        try:
            return float(str(value).strip().rstrip("%"))
        except (TypeError, ValueError):
            return None

    def _find_xds_workdir(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        if path.is_file():
            path = path.parent
        candidates = [path, path / "diff" / "p", path / "p"]
        for candidate in candidates:
            if (candidate / "CORRECT.LP").exists() or (candidate / "XDS.INP").exists():
                return candidate
        return path

    def _summary_row(
        self, name: str, rows: dict[str, dict[str, str]], workdir: Path | None
    ) -> dict[str, str]:
        if name and name in rows:
            return rows[name]
        if workdir:
            for parent in [workdir, *workdir.parents[:5]]:
                summary = parent / "summary.txt"
                if not summary.exists():
                    continue
                try:
                    lines = summary.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                if not lines:
                    continue
                headers = lines[0].split("\t")
                matches: list[dict[str, str]] = []
                for line in lines[1:]:
                    values = line.split("\t")
                    row = dict(zip(headers, values))
                    if row.get("Dataset") == name or (not name and row.get("Dataset") == workdir.parent.parent.name):
                        matches.append(row)
                if matches:
                    return matches[-1]
        return {}

    def inspect_xds(
        self,
        dataset_path: Path | None,
        dataset_name: str | None = None,
        summary_rows: dict[str, dict[str, str]] | None = None,
    ) -> AnalysisReport:
        workdir = self._find_xds_workdir(dataset_path)
        name = dataset_name or (workdir.parent.parent.name if workdir and workdir.name == "p" else (workdir.name if workdir else "unknown"))
        row = self._summary_row(name, summary_rows or {}, workdir)
        report = AnalysisReport("AutoXDS", name, "not-ready", "No usable XDS result was found.")
        lp = workdir / "CORRECT.LP" if workdir else None
        inp = workdir / "XDS.INP" if workdir else None
        hkl = workdir / "temp.hkl" if workdir else None
        xds_hkl = workdir / "XDS_ASCII.HKL" if workdir else None
        text = ""
        if lp and lp.exists():
            text = lp.read_text(encoding="utf-8", errors="replace")
            report.artifacts.append(str(lp))
        if inp and inp.exists():
            report.artifacts.append(str(inp))
        if hkl and hkl.exists():
            report.artifacts.append(str(hkl))
        elif xds_hkl and xds_hkl.exists():
            report.artifacts.append(str(xds_hkl))

        def from_text(pattern: str) -> str | None:
            matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
            if not matches:
                return None
            value = matches[-1]
            return " ".join(value) if isinstance(value, tuple) else str(value).strip()

        cell = row.get("Cell") or from_text(r"UNIT_CELL_CONSTANTS=\s*([0-9.\s]+?)\s+as used")
        sg = row.get("SG") or from_text(r"SPACE_GROUP_NUMBER=\s*(\d+)")
        isa = row.get("ISa") or from_text(r"\bISa\s*=\s*([0-9.]+)")
        rfactor = row.get("Rfactor")
        completeness = row.get("Completeness")
        # A summary.txt is optional.  When it is absent, the final `total`
        # row of CORRECT.LP still contains the operative completeness and
        # observed R-factor for the complete image range.
        totals = re.findall(
            r"^\s*total\s+\d+\s+\d+\s+\d+\s+([0-9.]+)%\s+([0-9.]+)%",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if totals:
            lp_completeness, lp_rfactor = totals[-1]
            completeness = completeness or lp_completeness
            rfactor = rfactor or lp_rfactor
        resolution = row.get("Resolution")
        final = row.get("Final")

        for label, value, source in (
            ("Unit cell", cell, "summary.txt/CORRECT.LP"),
            ("Space group", sg, "summary.txt/CORRECT.LP"),
            ("Resolution", resolution, "summary.txt"),
            ("ISa", isa, "summary.txt/CORRECT.LP"),
            ("R-factor", rfactor, "summary.txt"),
            ("Completeness", completeness, "summary.txt"),
        ):
            if value and str(value).upper() != "NA":
                report.evidence.append(Evidence(label, str(value), source))

        has_result = bool(text) or bool(row)
        has_export = bool((hkl and hkl.exists()) or (xds_hkl and xds_hkl.exists()))
        if has_result:
            report.status = "complete" if has_export or final == "single_dataset_finished" else "partial"
            report.summary = "XDS processing result is available" + (" and a reflection file is ready." if has_export else ", but no exported reflection file was found.")
        if not has_export:
            report.findings.append(Finding("error", "Reflection output missing", "Neither temp.hkl nor XDS_ASCII.HKL is available in the working directory.", "Inspect the XDS/XDSCONV error before continuing to AutoSolve.", "xds_output_missing"))

        comp = self._float(completeness)
        rf = self._float(rfactor)
        isa_f = self._float(isa)
        if comp is not None and comp < 70:
            sev = "error" if comp < 50 else "warning"
            report.findings.append(Finding(sev, "Low completeness", f"Completeness is {comp:.1f}%.", "Use HCA to identify a compatible dataset with complementary reciprocal-space coverage before merging.", "xds_low_completeness"))
        if rf is not None and rf >= 30:
            report.findings.append(Finding("warning", "High R-factor", f"R-factor is {rf:.1f}%.", "Review the resolution cutoff and declared cell before structure solution.", "xds_high_rfactor"))
        if isa_f is not None and isa_f < 3:
            report.findings.append(Finding("warning", "Weak scaled signal", f"ISa is {isa_f:.2f}.", "Inspect frame quality and consider a more conservative resolution cutoff.", "xds_low_isa"))

        if has_export and comp is not None and comp >= 70 and (rf is None or rf < 30):
            report.next_steps.append("The reflection file passes the basic checks; review composition and space group, then prepare AutoSolve input.")
        if comp is not None and comp < 75:
            report.next_steps.append("Run HCA and compare compatible datasets before deciding whether to merge.")
        if rf is not None and rf >= 30:
            report.next_steps.append("Revisit the resolution cutoff or declared unit cell, changing one variable at a time.")
        if not report.next_steps:
            report.next_steps.append("Inspect CORRECT.LP and confirm the reflection export before continuing.")
        return report

    def _find_r3d_output(self, path: Path | None, hints: dict[str, Any]) -> Path | None:
        output_hint = hints.get("output")
        candidates: list[Path] = []
        if output_hint:
            out = Path(str(output_hint))
            if not out.is_absolute() and path:
                out = path / out
            candidates.append(out)
        if path:
            if path.is_file():
                path = path.parent
            candidates.extend([path, path / "results" / "AutoR3D"])
        for candidate in candidates:
            if (candidate / "qc_report.json").exists():
                return candidate
        return candidates[-1] if candidates else None

    def inspect_r3d(
        self,
        dataset_path: Path | None,
        dataset_name: str | None = None,
        hints: dict[str, Any] | None = None,
    ) -> AnalysisReport:
        hints = hints or {}
        output = self._find_r3d_output(dataset_path, hints)
        name = dataset_name or (dataset_path.name if dataset_path else "unknown")
        report = AnalysisReport("AutoR3D", name, "not-ready", "No AutoR3D qc_report.json was found.")
        qc_path = output / "qc_report.json" if output else None
        if not qc_path or not qc_path.exists():
            report.findings.append(Finding("info", "Reconstruction not analyzed", "Run AutoR3D to generate qc_report.json and the 3D panel.", "Use the current dataset and verify that the cRED2 parameter file and TIFF stack exist.", "r3d_qc_missing"))
            report.next_steps.append("Run reconstruction after confirming the dataset and frame directory.")
            return report

        qc = self._read_json(qc_path)
        frames = qc.get("frames", {})
        obs = qc.get("sparse_observations", {})
        volume = qc.get("volume", {})
        peaks = qc.get("peaks", {})
        geometry = qc.get("geometry", {})
        refinement = qc.get("axis_refinement", {})
        panel = qc.get("panel", {}).get("scale_bar", {})
        report.status = "complete"
        report.summary = "AutoR3D geometry reconstruction and QC artifacts are available. This is geometry/peak-candidate QC, not final reflection integration."
        report.artifacts.append(str(qc_path))

        discovered = int(frames.get("discovered", 0) or 0)
        placeholder = int(frames.get("placeholder_frame_count", len(frames.get("placeholder_zero_frames", []))) or 0)
        used = int(frames.get("intensity_frame_count", frames.get("used_frame_count", 0)) or 0)
        observation_count = int(obs.get("count", 0) or 0)
        peak_count = int(peaks.get("count", 0) or 0)
        axis = refinement.get("chosen_rotation_axis_deg", geometry.get("rotation_axis_deg"))
        axis_source = refinement.get("axis_source", "unknown")
        report.evidence.extend([
            Evidence("Frames", f"{discovered} total / {placeholder} placeholders / {used} intensity", "qc_report.json"),
            Evidence("Sparse observations", str(observation_count), "qc_report.json"),
            Evidence("Peak candidates", str(peak_count), "qc_report.json"),
            Evidence("Rotation axis", f"{axis}° ({axis_source})", "qc_report.json"),
            Evidence("Beam center", f"({geometry.get('center_x')}, {geometry.get('center_y')}) px", "qc_report.json"),
            Evidence("Detector distance", f"{geometry.get('detector_distance_mm')} mm", "qc_report.json"),
            Evidence("Volume", f"shape={volume.get('shape')}, voxel={volume.get('voxel_size')} A^-1", "qc_report.json"),
            Evidence("Scale bar", f"{panel.get('reciprocal_pixel_per_angstrom')} A^-1/px; {panel.get('detector_dimensions_px')} px", "qc_report.json"),
            Evidence("Exclusion mask", f"{obs.get('excluded_pixel_count', 0)} px; source={obs.get('external_mask_file') or 'center mask'}", "qc_report.json"),
        ])

        geometry_path = output / "observations" / "frame_geometry.json"
        if geometry_path.exists():
            report.artifacts.append(str(geometry_path))
            geometry_data = self._read_json(geometry_path).get("frames", [])
            placeholders = [item for item in geometry_data if item.get("is_placeholder_zero_frame")]
            bad_placeholders = [item for item in placeholders if item.get("used_for_intensity")]
            if len(geometry_data) != discovered or len(placeholders) != placeholder or bad_placeholders:
                report.findings.append(Finding("error", "Placeholder-frame policy mismatch", f"frame_geometry has {len(geometry_data)} slots and {len(placeholders)} placeholders; QC expects {discovered} and {placeholder}.", "Do not use this geometry downstream until frame numbering and zero-frame flags are corrected.", "r3d_placeholder_mismatch"))
            else:
                report.findings.append(Finding("info", "Placeholder-frame policy verified", f"All {placeholder} zero frames retain their angle slots and are excluded from intensity.", "", "r3d_placeholder_verified"))
        else:
            report.findings.append(Finding("warning", "Frame geometry missing", "frame_geometry.json is unavailable, so placeholder-slot preservation cannot be verified.", "Regenerate the AutoR3D outputs before downstream use.", "r3d_geometry_missing"))

        peaks_path = output / "peaks" / "peak_candidates.json"
        panel_path = output / "panel" / "index.html"
        for artifact in (peaks_path, panel_path, output / "summary.md"):
            if artifact.exists():
                report.artifacts.append(str(artifact))
        if observation_count == 0:
            report.findings.append(Finding("error", "No sparse observations", "The reconstruction contains no intensity-bearing observations.", "Tune peak-threshold first; change one parameter at a time.", "r3d_no_observations"))
            report.next_steps.append("Adjust peak-threshold first, then rerun and compare the observation count.")
        elif peak_count == 0:
            report.findings.append(Finding("warning", "No peak candidates", f"{observation_count} observations were collected but no 3D peak candidates were found.", "Inspect the 3D panel; then consider axis refinement or peak-finding thresholds.", "r3d_no_peaks"))
            report.next_steps.append("Inspect the 3D panel for diffuse structure; use --refine-axis only if the fixed axis appears unfocused.")
        else:
            report.next_steps.append("Open the 3D panel and verify that the candidate peaks form coherent reciprocal-space features.")
        if not refinement.get("enabled", False):
            report.next_steps.append("Keep the fixed axis unless the panel is diffuse; if it is, run an explicit --refine-axis comparison.")
        return report

    def _find_solve_workdir(self, path: Path | None, hints: dict[str, Any]) -> Path | None:
        if hints.get("workdir"):
            return Path(str(hints["workdir"]))
        if path is None:
            return None
        return path.parent if path.is_file() else path

    def inspect_solve(
        self,
        dataset_path: Path | None,
        dataset_name: str | None = None,
        hints: dict[str, Any] | None = None,
    ) -> AnalysisReport:
        hints = hints or {}
        workdir = self._find_solve_workdir(dataset_path, hints)
        basename = str(hints.get("basename") or dataset_name or "").strip()
        if not basename and workdir:
            basename = workdir.name
        report = AnalysisReport("AutoSolve", basename or "unknown", "not-ready", "No SHELXT result was found.")
        if not workdir or not workdir.exists():
            report.findings.append(Finding("error", "Working directory missing", "The AutoSolve working directory does not exist.", "Select a valid HKL file or working directory.", "solve_workdir_missing"))
            return report

        source_res = Path(str(hints["res"])) if hints.get("res") else None
        if source_res:
            refined = []
            for root in (
                self.project_root / "log" / "AutoSolve" / "shelxl_refinement",
                source_res.parent / "shelxl_refinement",
            ):
                refined.extend(root.glob("*/*.res"))
            refined.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            if refined:
                return self._inspect_shelxl_refinement(refined[0], source_res)

        res = workdir / f"{basename}.res"
        lxt = workdir / f"{basename}.lxt"
        lst = workdir / f"{basename}.lst"
        if not res.exists() and not basename:
            files = sorted(workdir.glob("*.res"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                res = files[0]
                basename = res.stem
                lxt = workdir / f"{basename}.lxt"
                lst = workdir / f"{basename}.lst"
                report.dataset = basename
        log = lxt if lxt.exists() else lst
        res_text = res.read_text(encoding="utf-8", errors="replace") if res.exists() else ""
        log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        for artifact in (res, log):
            if artifact.exists():
                report.artifacts.append(str(artifact))

        before_hklf = res_text.split("HKLF", 1)[0]
        # Atom records follow FVAR in normal SHELX output.  Restricting the
        # search avoids counting numeric instructions such as UNIT and ZERR.
        atom_block = before_hklf.split("FVAR", 1)[1] if "FVAR" in before_hklf else before_hklf
        atom_pattern = re.compile(r"^\s*[A-Za-z][A-Za-z0-9']*\s+\d+\s+[-+0-9.]+\s+[-+0-9.]+\s+[-+0-9.]+", re.MULTILINE)
        atom_count = len(atom_pattern.findall(atom_block))
        if res_text and log_text and atom_count > 0:
            report.status = "solved_success"
            report.summary = f"SHELXT produced a non-empty model with {atom_count} atom records before HKLF."
        elif res_text and log_text:
            report.status = "shelxt_no_solution"
            report.summary = "SHELXT produced files, but no clear atom model was found before HKLF."
            report.findings.append(Finding("warning", "No clear structure solution", "The result does not contain atom records before HKLF.", "Verify composition, space group and XDS data quality before trying a broader space-group search.", "solve_no_solution"))
        else:
            report.status = "shelxt_failed"
            missing = []
            if not res_text:
                missing.append("non-empty .res")
            if not log_text:
                missing.append("non-empty .lxt/.lst")
            report.summary = "SHELXT output is incomplete: missing " + ", ".join(missing) + "."
            report.findings.append(Finding("error", "SHELXT output incomplete", report.summary, "Check the HKL, CELL, composition and mapped space group before rerunning.", "solve_output_missing"))

        cell = re.search(r"^CELL\s+(.+)$", res_text, re.MULTILINE)
        sg = re.search(r"^TITL\s+(.+)$", res_text, re.MULTILINE)
        cfom = re.search(r"best CFOM\s*=\s*([0-9.]+)", log_text, re.IGNORECASE)
        r1 = re.search(r"REM R1\s*=\s*([0-9.]+)", res_text)
        for label, match, source in (
            ("Cell", cell, res.name),
            ("Title / space group", sg, res.name),
            ("Best CFOM", cfom, log.name),
            ("R1", r1, res.name),
        ):
            if match:
                report.evidence.append(Evidence(label, match.group(1).strip(), source))
        report.evidence.append(Evidence("Atom records before HKLF", str(atom_count), res.name))
        if report.status == "solved_success":
            report.next_steps.append("Open the .res model in Olex2 and inspect connectivity, atom types and chemically unreasonable geometry.")
            report.next_steps.append("Treat this as an initial solution; refinement and final validation are still required.")
        elif report.status == "shelxt_no_solution":
            report.next_steps.append("Check XDS completeness/R-factor and confirm composition and SG before changing SHELXT strategy.")
        else:
            report.next_steps.append("Repair missing input/output prerequisites before rerunning SHELXT.")
        return report

    def _inspect_shelxl_refinement(self, res: Path, source_res: Path) -> AnalysisReport:
        lst = res.with_suffix(".lst")
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (res, lst)
            if path.exists()
        )
        report = AnalysisReport(
            "AutoSolve",
            res.parent.name,
            "shelxl_refined",
            "An isolated SHELXL refinement result is available.",
            artifacts=[str(source_res), str(res)],
        )
        if lst.exists():
            report.artifacts.append(str(lst))

        def number(pattern: str) -> float | None:
            match = re.search(pattern, text, re.IGNORECASE)
            return self._float(match.group(1)) if match else None

        r1 = number(r"R1\s*=\s*([0-9.]+)\s+for\s+\d+\s+Fo\s*>\s*4sig")
        r1_all = number(r"and\s+([0-9.]+)\s+for\s+all\s+\d+\s+data")
        wr2 = number(r"wR2\s*=\s*([0-9.]+)")
        gof = number(r"GooF\s*=\s*(?:S\s*=\s*)?([0-9.]+)")
        shift = number(r"Max\.\s*shift\s*=\s*([+-]?[0-9.]+)")
        for label, value in (("R1 (I>2σ)", r1), ("R1 (all)", r1_all), ("wR2", wr2), ("GoF", gof), ("Max shift", shift)):
            if value is not None:
                report.evidence.append(Evidence(label, f"{value:.4f}", res.name if label != "Max shift" else lst.name))

        if "Cell contents from UNIT instruction and atom list do not agree" in text:
            report.findings.append(Finding(
                "warning", "Model composition is incomplete or inconsistent",
                "SHELXL reports that UNIT and the current atom list do not agree.",
                "Use Olex2 to inspect the strongest Q peaks, correct element assignments, and only then add appropriate restraints.",
                "shelxl_unit_atom_mismatch",
            ))
        if shift is not None and shift > 0.05:
            report.findings.append(Finding(
                "warning", "Refinement is not converged",
                f"Maximum coordinate shift is {shift:.3f} Å.",
                "Do not start final anisotropic refinement yet; fix the incomplete or unstable model first.",
                "shelxl_not_converged",
            ))
        if r1 is not None and r1 > 0.20:
            report.findings.append(Finding(
                "warning", "High ED residual",
                f"R1(I>2σ) is {r1:.3f}.",
                "For ED, kinematic R factors are higher than X-ray values, but this level still indicates a model requiring work.",
                "shelxl_high_r1",
            ))
        report.next_steps.append("Open this isolated .res in Olex2, inspect the Q peaks and connectivity, then assign only chemically justified atoms.")
        if report.findings:
            report.next_steps.append("Run another short SHELXL cycle after one deliberate model change; avoid adding many atoms or restraints at once.")
        return report
