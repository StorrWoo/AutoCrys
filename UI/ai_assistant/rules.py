"""Rule engine - pattern-based problem detection for XDS/SHELXT/AutoR3D output."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable


@dataclass
class Diagnosis:
    rule_id: str
    severity: str
    title: str
    detail: str
    suggestion: str
    fix_id: str | None = None
    fix_params: dict = field(default_factory=dict)
    matched_line: str = ""
    timestamp: float = 0.0

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        return self.severity == "warning"

    @property
    def severity_icon(self) -> str:
        return {"error": "[ERROR]", "warning": "[WARN]", "info": "[INFO]"}.get(self.severity, "[INFO]")

    @property
    def severity_tag(self) -> str:
        return {"error": "diag_error", "warning": "diag_warn", "info": "diag_info"}.get(self.severity, "diag_info")


@dataclass
class Rule:
    rule_id: str
    patterns: list[str]
    severity: str
    title: str
    suggestion: str
    detail_template: str = ""
    fix_id: str | None = None
    fix_params: dict = field(default_factory=dict)
    context_tabs: list[str] = field(default_factory=list)
    cooldown_count: int = 1

    def matches(self, line: str) -> re.Match | None:
        for pattern in self.patterns:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                return m
        return None

    def build_diagnosis(self, match: re.Match, line: str) -> Diagnosis:
        detail = self.detail_template
        if match and match.groups():
            try:
                detail = detail.format(*match.groups())
            except (IndexError, KeyError):
                pass
        return Diagnosis(
            rule_id=self.rule_id,
            severity=self.severity,
            title=self.title,
            detail=detail,
            suggestion=self.suggestion,
            fix_id=self.fix_id,
            fix_params=dict(self.fix_params),
            matched_line=line.strip(),
        )


class RuleEngine:
    def __init__(self):
        self._rules: list[Rule] = []
        self._cooldowns: dict[str, int] = {}
        self._callback: Callable[[Diagnosis], None] | None = None

    def register(self, rule: Rule) -> None:
        self._rules.append(rule)

    def register_callback(self, callback: Callable[[Diagnosis], None]) -> None:
        self._callback = callback

    def feed_line(self, line: str, active_tab: str = "") -> list[Diagnosis]:
        results: list[Diagnosis] = []
        for rule in self._rules:
            if rule.context_tabs and active_tab not in rule.context_tabs:
                continue
            cd_key = rule.rule_id
            if self._cooldowns.get(cd_key, 0) >= rule.cooldown_count:
                continue
            m = rule.matches(line)
            if not m:
                continue
            diag = rule.build_diagnosis(m, line)
            self._cooldowns[cd_key] = self._cooldowns.get(cd_key, 0) + 1
            results.append(diag)
            if self._callback:
                self._callback(diag)
        return results

    def feed_lines(self, lines: list[str], active_tab: str = "") -> list[Diagnosis]:
        all_diags: list[Diagnosis] = []
        for line in lines:
            all_diags.extend(self.feed_line(line, active_tab))
        return all_diags

    def reset_cooldowns(self) -> None:
        self._cooldowns.clear()

    @property
    def rule_count(self) -> int:
        return len(self._rules)


def build_default_engine() -> RuleEngine:
    engine = RuleEngine()

    engine.register(Rule(
        rule_id="xds_insufficient_percentage",
        patterns=[r"INSUFFICIENT PERCENTAGE\s*\(\s*<\s*50\s*%\s*\)"],
        severity="error",
        title="XDS selfcheck: insufficient reflections",
        detail_template="XDS detected fewer than 50% expected reflections. Dataset may have weak diffraction or wrong detector parameters.",
        suggestion="AutoXDS will enter recovery mode: reduce MINIMUM_NUMBER_OF_REFLECTIONS and rerun. If it persists, check beam center, detector distance, and frame exposure.",
        fix_id="xds_recovery",
        context_tabs=["AutoXDS"],
        cooldown_count=1,
    ))

    engine.register(Rule(
        rule_id="xds_cannot_open_tmp",
        patterns=[r"CANNOT\s+OPEN\s+OR\s+READ\s+FILE\s+bin\d+"],
        severity="error",
        title="XDS WSL processor conflict",
        detail_template="XDS cannot read temporary file. This is a known WSL parallel-processing issue.",
        suggestion="Set MAXIMUM_NUMBER_OF_PROCESSORS=1 in XDS.INP and rerun. This forces single-threaded mode and avoids the race condition.",
        fix_id="set_max_processors",
        context_tabs=["AutoXDS"],
        cooldown_count=1,
    ))

    engine.register(Rule(
        rule_id="xds_general_error",
        patterns=[r"!!+\s*ERROR\s*!!+"],
        severity="error",
        title="XDS encountered an error",
        detail_template="XDS stopped with an error. Check CORRECT.LP and XDS.LP for details.",
        suggestion="Common causes: wrong frame template path, corrupted TIFF frames, insufficient memory, or beam center too far from detector center.",
        context_tabs=["AutoXDS"],
        cooldown_count=3,
    ))

    engine.register(Rule(
        rule_id="xscale_misplaced_param",
        patterns=[r"MISPLACED\s+PARAMETER"],
        severity="error",
        title="XSCALE.INP parameter order error",
        detail_template="INPUT_FILE lines must appear before UNIT_CELL_CONSTANTS and SPACE_GROUP_NUMBER in XSCALE.INP.",
        suggestion="The script can auto-fix the ordering. Click execute to regenerate XSCALE.INP with correct order.",
        fix_id="fix_xscale_order",
        context_tabs=["AutoXDS"],
        cooldown_count=1,
    ))

    engine.register(Rule(
        rule_id="xscale_warning",
        patterns=[r"!!+\s*WARNING\s*!!+"],
        severity="warning",
        title="XSCALE warning",
        detail_template="XSCALE issued a warning. The merge may still complete but check the results carefully.",
        suggestion="Review XSCALE.LP for ISa values and outlier datasets. Datasets with very low ISa may need to be excluded.",
        context_tabs=["AutoXDS"],
        cooldown_count=2,
    ))

    engine.register(Rule(
        rule_id="high_rfactor",
        patterns=[r"R.?factor.*?[=:\s]+0\.([12]\d{2}|[3-9]\d)",
                  r"R_?merge.*?[=:\s]+0\.([12]\d{2}|[3-9]\d)"],
        severity="warning",
        title="High R-factor detected",
        detail_template="The R-factor appears unusually high. This may indicate poor data quality or incorrect unit cell.",
        suggestion="Check the resolution cutoff from CORRECT.LP. Consider: 1) tighter resolution cutoff, 2) verify declared cell parameters, 3) check for crystal decay.",
        context_tabs=["AutoXDS"],
        cooldown_count=2,
    ))

    engine.register(Rule(
        rule_id="low_completeness",
        patterns=[r"[Cc][Oo][Mm][Pp][Ll][Ee][Tt].*?[<:=\s]+([1-6][0-9]|[0-9])\.?[0-9]*\s*%"],
        severity="warning",
        title="Low data completeness",
        detail_template="Completeness is below 70%. The dataset may have missing wedges or weak high-resolution data.",
        suggestion="Consider merging with complementary datasets to fill missing regions. Check if rotation range was sufficient.",
        context_tabs=["AutoXDS"],
        cooldown_count=2,
    ))

    engine.register(Rule(
        rule_id="no_solution",
        patterns=[r"shelxt_no_solution", r"no_solution", r"NO\s+SOLUTION", r"\*\*\s*NO\s+SOLUTION\s*"],
        severity="warning",
        title="SHELXT found no solution",
        detail_template="SHELXT could not find a structure solution for this dataset.",
        suggestion="Try: 1) -s\"P-1\" triclinic fallback, 2) verify chemical composition and Z, 3) check that XSCALE and XDSCONV both used MERGE=FALSE so observations were retained, 4) try a different resolution cutoff.",
        fix_id="try_p1_solve",
        context_tabs=["AutoSolve"],
        cooldown_count=1,
    ))

    engine.register(Rule(
        rule_id="shelxt_failed",
        patterns=[r"shelxt_failed", r"input_preparation_failed"],
        severity="error",
        title="SHELXT execution failed",
        detail_template="SHELXT could not run. Common causes: missing input file, invalid cell, or missing composition.",
        suggestion="Verify: 1) .hkl file exists in working directory, 2) cell parameters are valid, 3) composition is provided and elements are recognized.",
        context_tabs=["AutoSolve"],
        cooldown_count=1,
    ))

    engine.register(Rule(
        rule_id="zero_frames_detected",
        patterns=[r"placeholder\s+zero\s+frames:\s*([1-9]\d*)"],
        severity="info",
        title="Placeholder zero frames detected",
        detail_template="This dataset contains zero-intensity placeholder frames. These are correctly excluded from reconstruction.",
        suggestion="No action needed. AutoR3D will preserve frame numbering and exclude zero frames from observations.",
        context_tabs=["AutoR3D", "Reconstruction"],
        cooldown_count=1,
    ))

    engine.register(Rule(
        rule_id="reconstruction_empty",
        patterns=[r"No\s+observations\s+found", r"0\s+observations", r"empty\s+volume", r"volume.*?all\s+zero"],
        severity="warning",
        title="Empty reconstruction volume",
        detail_template="The 3D reconstruction produced no observations or an empty volume.",
        suggestion="Tune parameters: 1) lower peak-threshold, 2) check center-mask-radius (reduce if the direct beam is not masking real spots), 3) verify rotation axis, 4) check if peak-filter-size is too large.",
        context_tabs=["AutoR3D", "Reconstruction"],
        cooldown_count=1,
    ))

    engine.register(Rule(
        rule_id="xds_resolution_low",
        patterns=[r"RESOLUTION\s+LIMIT.*?[=:\s]+([4-9]\.\d+|[1-9]\d+\.\d+)", r"Resolution.*?cutoff.*?[=:\s]+([4-9]\.\d+)"],
        severity="warning",
        title="Low resolution limit",
        detail_template="The achieved resolution is relatively low. This may limit structure solution quality.",
        suggestion="Check if the declared resolution was too restrictive. Consider re-running without a resolution cutoff, or inspect the frames for radiation damage or poor crystal quality.",
        context_tabs=["AutoXDS"],
        cooldown_count=2,
    ))

    engine.register(Rule(
        rule_id="sg_mismatch",
        patterns=[r"SG\s+mismatch", r"space\s+group\s+mismatch", r"different\s+SG\s+values"],
        severity="warning",
        title="Space group mismatch in merge candidates",
        detail_template="Selected datasets have different space group assignments. Merging may produce unreliable results.",
        suggestion="Confirm all datasets should have the same space group. If so, use --allow-sg-mismatch flag. Otherwise, only merge datasets with matching SG.",
        fix_id="allow_sg_mismatch",
        context_tabs=["AutoXDS"],
        cooldown_count=1,
    ))

    return engine
