"""Knowledge base - parses SKILL.md files and provides structured knowledge."""

from __future__ import annotations

from pathlib import Path
import re


SECTION_PATTERN = re.compile(r"^#{1,3}\s+(.*?)$", re.MULTILINE)


class KnowledgeBase:
    def __init__(self, autocrys_root: Path):
        self._root = autocrys_root
        self._sections: dict[str, dict[str, str]] = {}
        self._load_all()

    def _load_all(self) -> None:
        skill_dirs = [
            ("AutoXDS", self._root / "AutoXDS" / "SKILL.md"),
            ("AutoR3D", self._root / "AutoR3D" / "SKILL.md"),
            ("AutoSolve", self._root / "AutoSolve" / "SKILL.md"),
            ("AutoRefine", self._root / "AutoRefine" / "SKILL.md"),
        ]
        for name, path in skill_dirs:
            if path.exists():
                self._sections[name] = self._parse_skill(path)

    def _parse_skill(self, path: Path) -> dict[str, str]:
        text = path.read_text(encoding="utf-8", errors="replace")
        stripped = text.split("---\n", 2)
        if len(stripped) >= 3:
            text = stripped[2]
        elif len(stripped) == 2 and stripped[0].strip() == "":
            text = stripped[1]
        sections: dict[str, str] = {}
        matches = list(SECTION_PATTERN.finditer(text))
        for i, m in enumerate(matches):
            title = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections[title] = text[start:end].strip()
        sections["_full"] = text
        return sections

    def get_section(self, module: str, title: str) -> str:
        return self._sections.get(module, {}).get(title, "")

    def get_full(self, module: str) -> str:
        return self._sections.get(module, {}).get("_full", "")

    def search(self, query: str) -> list[tuple[str, str, str]]:
        results: list[tuple[str, str, str]] = []
        q = query.lower()
        for module, sections in self._sections.items():
            for title, content in sections.items():
                if title == "_full":
                    continue
                if q in title.lower() or q in content.lower():
                    results.append((module, title, content))
        return results

    def modules(self) -> list[str]:
        return list(self._sections.keys())

    def build_system_prompt(
        self,
        action_catalog: str = "",
        modules: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        parts = ["You are an AI assistant for AutoCrys, a crystallographic data processing application."]
        parts.append("You help users with 3D Electron Diffraction (3D ED / MicroED / cRED) data processing.")
        parts.append("")
        parts.append("=== OPTIONAL KNOWLEDGE MODULES ===")
        parts.append("AutoXDS: XDS processing, conversion, clustering, merging, and dataset QC.")
        parts.append("AutoR3D: reciprocal-space reconstruction, geometry, peaks, slices, and 3D QC.")
        parts.append("AutoSolve: SHELXT input preparation, structure solution, and result classification.")
        parts.append("AutoRefine: isolated SHELXL refinement and refinement-result handling.")
        parts.append(
            "If the user's request needs exact workflow knowledge that is not loaded below, reply only with "
            '<autocrys_context>{"modules":["AutoXDS"]}</autocrys_context>, choosing one or more names '
            "from this list. Request knowledge before proposing any executable operation. Do not request a "
            "module for greetings, simple professional conversation, or questions answerable from the current "
            "UI snapshot and general crystallographic knowledge."
        )
        parts.append("")
        parts.append("=== AVAILABLE KNOWLEDGE ===")
        selected_modules = self._sections if modules is None else modules
        for module in selected_modules:
            full = self.get_full(module)
            if full:
                parts.append(f"--- {module} ---")
                parts.append(full)
        if action_catalog:
            parts.append("")
            parts.append("=== AVAILABLE EXECUTABLE SKILLS ===")
            parts.append(action_catalog)
            parts.append(
                "Choose an executable skill yourself only when the user clearly asks to perform an action. "
                "For advice, diagnosis, explanation, or missing parameters, answer normally without an action."
            )
            parts.append(
                "To propose an action, end the response with exactly one machine-readable block: "
                '<autocrys_action>{"skill_id":"...","params":{...}}</autocrys_action>. '
                "Use only a listed skill_id and its declared parameters. Do not invent values."
            )
        parts.append("")
        parts.append("=== YOUR ROLE ===")
        parts.append("1. Diagnose problems in XDS, XSCALE, SHELXT, and AutoR3D output.")
        parts.append("2. Suggest parameter fixes and workflow improvements.")
        parts.append("3. Answer crystallographic questions related to the AutoCrys workflow.")
        parts.append("4. Generate commands using the available scripts (auto_xds.py, auto_shelxt.py, autor3d CLI).")
        parts.append("5. Always be concise and actionable. Prefer specific commands over general advice.")
        parts.append("6. Treat the [Artifact analysis] block as verified evidence. Never invent metrics that are absent from it.")
        parts.append("7. Distinguish geometry/peak-candidate QC from final reflection integration and structure refinement.")
        parts.append("8. Never guess composition, unit cell, space group, file path, or success status.")
        parts.append("9. Recommend one parameter change at a time and explain which evidence motivates it.")
        parts.append(
            "10. If you actually execute an AutoXDS, AutoSolve, or AutoR3D task with your own tools, "
            "end the final response with exactly one machine-readable completion block: "
            '<autocrys_result>{"module":"AutoXDS","operation":"process_xds","success":true,'
            '"summary":"short result","output":"optional concise output"}</autocrys_result>. '
            "Use module AutoXDS, AutoSolve, or AutoR3D. Do not emit this block for advice, chat, "
            "a proposed action, or work you did not execute."
        )
        parts.append(
            "11. Always answer in Simplified Chinese. Unless the user explicitly asks for a detailed "
            "or step-by-step explanation, aim for about 300 Chinese characters. Prioritize complete "
            "sentences and required information: exceed the target when necessary rather than ending "
            "mid-sentence, while remaining concise."
        )
        return "\n".join(parts)

    def build_agent_bridge_prompt(self) -> str:
        """Build the thin UI contract used when OpenClaw owns all skills."""
        return "\n".join(
            [
                "You are the OpenClaw agent connected to the AutoCrys crystallography UI.",
                "Use only the skills and tools configured in your OpenClaw environment for workflow knowledge and execution.",
                "AutoCrys does not provide a second skill catalog in agent mode. Do not request <autocrys_context> knowledge modules and do not emit <autocrys_action> proposals.",
                "Treat the supplied UI snapshot and [Artifact analysis] block as evidence. Never invent composition, unit cell, space group, file paths, metrics, or success status.",
                "If you actually execute an AutoXDS, AutoSolve, or AutoR3D task with your own tools, end the final response with exactly one machine-readable completion block:",
                '<autocrys_result>{"module":"AutoXDS","operation":"process_xds","success":true,"summary":"short result","output":"optional concise output"}</autocrys_result>',
                "Use module AutoXDS, AutoSolve, or AutoR3D. Do not emit this block for advice, chat, a proposed action, or work you did not execute.",
                "Always answer in Simplified Chinese. Unless the user asks for detail, be concise and actionable, while always completing the sentence.",
            ]
        )
