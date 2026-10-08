"""Skill mapper - translates natural language intents to AutoCrys script commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
import shlex
import subprocess
import sys


@dataclass
class SkillAction:
    skill_id: str
    description: str
    command_template: str
    params: list[str] = field(default_factory=list)
    confirm_required: bool = True

    def build_command(self, **kwargs) -> str:
        import re as _re
        cmd = self.command_template
        for key, value in kwargs.items():
            if isinstance(value, list):
                if key == "datasets":
                    # auto_xds.py uses argparse action="append", so every
                    # dataset needs its own --dataset option.
                    escaped = [str(v).replace('"', '\\"') for v in value]
                    flat = '" --dataset "'.join(escaped)
                    flat = f'"{flat}"' if flat else ""
                else:
                    flat = " ".join(str(v) for v in value)
                cmd = cmd.replace(f"{{{key}}}", flat)
            else:
                cmd = cmd.replace(f"{{{key}}}", str(value or ""))
        cmd = _re.sub(r"\{([^}]+)\}", "", cmd)
        return cmd.strip()

    def parse_args(self, command: str) -> dict[str, object]:
        return {}

    def build_argv(self, **kwargs) -> list[str]:
        """Build an argv without the lossy ``str.split()`` used by the old UI."""
        command = self.build_command(**kwargs)
        if os.name == "nt":
            tokens = shlex.split(command, posix=False)
            return [token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token for token in tokens]
        return shlex.split(command, posix=True)

    def missing_params(self, values: dict | None = None) -> list[str]:
        values = values or {}
        missing = [name for name in self.params if values.get(name) in (None, "", [])]
        datasets = values.get("datasets")
        if "datasets" in self.params and isinstance(datasets, (list, tuple)) and len(datasets) < 2:
            missing.append("datasets (at least 2)")
        return missing


class SkillMapper:
    _ACTION_PATTERN = re.compile(
        r"<autocrys_action>\s*(\{.*?\})\s*</autocrys_action>",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(self, project_root: Path):
        self._root = project_root
        self._skills: list[SkillAction] = self._register_skills()

    def _register_skills(self) -> list[SkillAction]:
        xds_script = str(self._root / "AutoXDS" / "scripts" / "auto_xds.py")
        solve_script = str(self._root / "AutoSolve" / "scripts" / "auto_shelxt.py")
        refine_script = str(self._root / "AutoSolve" / "scripts" / "auto_shelxl.py")
        main_script = str(self._root / "main.py")
        python_exe = sys.executable

        return [
            SkillAction(
                skill_id="inspect_r3d",
                description="Inspect a cRED2 dataset and verify frame/placeholder geometry",
                command_template=f'"{python_exe}" "{main_script}" r3d inspect "{{dataset}}" --frames "{{frames}}"',
                params=["dataset", "frames"],
                confirm_required=False,
            ),
            SkillAction(
                skill_id="run_r3d",
                description="Run AutoR3D geometry reconstruction and QC",
                command_template=f'"{python_exe}" "{main_script}" r3d run "{{dataset}}" --frames "{{frames}}" --out "{{output}}" --rotation-axis-deg "{{axis}}"',
                params=["dataset", "frames", "output", "axis"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="run_r3d_refine_axis",
                description="Run AutoR3D with explicit rotation-axis refinement",
                command_template=f'"{python_exe}" "{main_script}" r3d run "{{dataset}}" --frames "{{frames}}" --out "{{output}}" --rotation-axis-deg "{{axis}}" --refine-axis',
                params=["dataset", "frames", "output", "axis"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="list_datasets",
                description="List all valid datasets under a root directory",
                command_template=f'"{python_exe}" "{xds_script}" list --root "{{root}}" --json',
                params=["root"],
            ),
            SkillAction(
                skill_id="process_xds",
                description="Run XDS processing on a dataset",
                command_template=f'"{python_exe}" "{xds_script}" process --root "{{root}}" --dataset "{{dataset}}" --clear-declared-cell-sg --preserve-original-cell-sg-on-first-run',
                params=["root", "dataset"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="process_xds_declared",
                description="Run XDS with declared cell and space group",
                command_template=f'"{python_exe}" "{xds_script}" process --root "{{root}}" --dataset "{{dataset}}" --declared-sg "{{sg}}" --declared-cell "{{cell}}"',
                params=["root", "dataset", "sg", "cell"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="process_xds_all",
                description="Run XDS processing on all datasets under a root",
                command_template=f'"{python_exe}" "{xds_script}" process --root "{{root}}" --all --replace-summary --clear-declared-cell-sg --preserve-original-cell-sg-on-first-run',
                params=["root"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="set_cell_sg",
                description="Set declared cell and space group without running XDS",
                command_template=f'"{python_exe}" "{xds_script}" set-cell-sg --root "{{root}}" --dataset "{{dataset}}" --declared-sg "{{sg}}" --declared-cell "{{cell}}"',
                params=["root", "dataset", "sg", "cell"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="cluster_hca",
                description="Run hierarchical cluster analysis on datasets",
                command_template=f'"{python_exe}" "{xds_script}" cluster --root "{{root}}" --dataset {{datasets}} --cluster-method "{{method}}" --json',
                params=["root", "datasets", "method"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="cluster_with_dendrogram",
                description="Run HCA clustering and output dendrogram SVG",
                command_template=f'"{python_exe}" "{xds_script}" cluster --root "{{root}}" --dataset {{datasets}} --cluster-method "{{method}}" --dendrogram "{{svg_path}}" --json',
                params=["root", "datasets", "method", "svg_path"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="merge_datasets",
                description="Prepare merge inputs without running XSCALE",
                command_template=f'"{python_exe}" "{xds_script}" merge --root "{{root}}" --dataset {{datasets}}',
                params=["root", "datasets"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="merge_and_run",
                description="Merge and run XSCALE + XDSCONV",
                command_template=f'"{python_exe}" "{xds_script}" merge --root "{{root}}" --dataset {{datasets}} --run',
                params=["root", "datasets"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="solve_shelxt",
                description="Run SHELXT structure solution",
                command_template=f'"{python_exe}" "{solve_script}" solve --dataset "{{dataset}}" --composition "{{composition}}" --sg "{{sg}}"',
                params=["dataset", "composition", "sg"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="solve_shelxt_with_olex2",
                description="Run SHELXT and open successful result in Olex2",
                command_template=f'"{python_exe}" "{solve_script}" solve --dataset "{{dataset}}" --composition "{{composition}}" --sg "{{sg}}" --open-olex2',
                params=["dataset", "composition", "sg"],
                confirm_required=True,
            ),
            SkillAction(
                skill_id="solve_prepare",
                description="Prepare SHELXT input without solving",
                command_template=f'"{python_exe}" "{solve_script}" prepare --dataset "{{dataset}}" --composition "{{composition}}" --sg "{{sg}}"',
                params=["dataset", "composition", "sg"],
            ),
            SkillAction(
                skill_id="classify_result",
                description="Classify a SHELXT result",
                command_template=f'"{python_exe}" "{solve_script}" classify --workdir "{{workdir}}" --basename "{{basename}}"',
                params=["workdir", "basename"],
            ),
            SkillAction(
                skill_id="refine_shelxl",
                description="Run an isolated SHELXL refinement and open the result in Olex2",
                command_template=f'"{python_exe}" "{refine_script}" --res "{{res}}" --hkl "{{hkl}}" --open-olex2 --json',
                params=["res", "hkl"],
                confirm_required=True,
            ),
        ]

    def match(self, user_input: str, context: dict | None = None) -> list[tuple[SkillAction, dict]]:
        scored: list[tuple[float, SkillAction, dict]] = []
        text = user_input.lower().strip()
        for skill in self._skills:
            score, params = self._try_match(skill, text, context)
            if score > 0:
                scored.append((score, skill, params))
        scored.sort(key=lambda item: (item[0], self._match_score(item[1], item[2])), reverse=True)
        return [(skill, params) for _score, skill, params in scored]

    def _try_match(self, skill: SkillAction, text: str, context: dict | None = None) -> tuple[float, dict]:
        ctx = context or {}
        params: dict[str, object] = {}
        score = 0.0

        keywords_map = {
            "list_datasets": ["list", "列表", "列数据集", "show datasets", "datasets", "available"],
            "process_xds": ["process", "run xds", "处理", "积分"],
            "process_xds_all": ["process all", "all datasets", "全部", "所有"],
            "process_xds_declared": ["declared", "指定", "设定"],
            "set_cell_sg": ["set cell", "设置晶胞", "set sg"],
            "cluster_hca": ["cluster", "hca", "聚类", "dendrogram"],
            "merge_datasets": ["prepare merge", "prepare xscale", "merge inputs", "准备合并"],
            "merge_and_run": ["merge and run", "merge", "xscale", "合并并运行", "合并"],
            "solve_shelxt": ["solve", "shelxt", "求解", "解结构"],
            "solve_shelxt_with_olex2": ["olex2", "open", "打开"],
            "solve_prepare": ["prepare", "准备", "ins"],
            "classify_result": ["classify", "分类", "result"],
            "refine_shelxl": ["refine", "shelxl", "精修"],
        }

        # Override legacy mojibake entries with real UTF-8 phrases used by the
        # Chinese UI.  Keeping this update separate also makes old config files
        # harmless when the source tree was previously opened with a GBK codec.
        keywords_map.update({
            "inspect_r3d": ["inspect r3d", "检查 r3d", "检查帧", "检查数据集", "占位帧"],
            "run_r3d": ["run r3d", "运行 r3d", "重建", "reciprocal reconstruction"],
            "run_r3d_refine_axis": ["refine axis", "轴精修", "优化旋转轴", "refine-axis"],
            "list_datasets": ["list", "列出", "列数据集", "有哪些数据", "show datasets", "datasets", "available"],
            "process_xds": ["process", "run xds", "运行 xds", "处理", "积分"],
            "process_xds_all": ["process all", "all datasets", "全部", "所有数据集"],
            "process_xds_declared": ["declared", "指定", "设定"],
            "set_cell_sg": ["set cell", "设置晶胞", "设置空间群", "set sg"],
            "cluster_hca": ["cluster", "hca", "聚类", "树状图", "dendrogram"],
            "merge_datasets": ["prepare merge", "prepare xscale", "merge inputs", "准备合并"],
            "merge_and_run": ["merge and run", "merge", "xscale", "合并并运行", "合并并执行", "合并"],
            "solve_shelxt": ["solve", "shelxt", "求解", "解结构"],
            "solve_shelxt_with_olex2": ["olex2", "open", "打开"],
            "solve_prepare": ["prepare", "准备", "生成 ins", "ins"],
            "classify_result": ["classify", "分类", "判断结果"],
            "refine_shelxl": ["refine", "shelxl", "精修", "结构精修"],
        })

        sid = skill.skill_id
        for kw in keywords_map.get(sid, []):
            if kw in text:
                score += 1.0 + min(len(kw), 20) / 100.0

        # UI context may fill parameters, but context alone is not user intent.
        # Without this guard a question such as "what is the current result?"
        # could accidentally become an executable action.
        if score == 0:
            return (0.0, {})

        if "dataset" in skill.params:
            # An explicitly typed dataset must override the currently selected
            # UI dataset; context is only a fallback.
            ds_text = self._extract_param(
                text,
                ["dataset", "实验", "数据"],
                r"([A-Za-z0-9][A-Za-z0-9.-]*_[A-Za-z0-9][A-Za-z0-9._-]*)",
            )
            ds = ds_text or ctx.get("dataset_name") or ctx.get("dataset")
            if ds:
                params["dataset"] = ds
                score += 0.5

        if "datasets" in skill.params:
            explicit = self._extract_datasets(text)
            context_datasets = ctx.get("datasets") or []
            if isinstance(context_datasets, str):
                context_datasets = context_datasets.replace(",", " ").split()
            datasets = explicit or list(context_datasets)
            if not datasets:
                current = ctx.get("dataset_name") or ctx.get("dataset")
                datasets = [current] if current else []
            if datasets:
                params["datasets"] = self._deduplicate(datasets)
                score += min(0.25 * len(params["datasets"]), 0.75)

        if "root" in skill.params:
            r = ctx.get("root") or ctx.get("xds_root")
            if r:
                params["root"] = str(r)
                score += 0.5

        if "sg" in skill.params:
            sg = ctx.get("sg") or self._extract_param(text, ["sg", "space group", "空间群"], r"(\d{1,3}|P[-\d\w\(\)/]+)")
            if sg:
                params["sg"] = sg
                score += 0.5

        if "cell" in skill.params:
            cell = ctx.get("cell")
            if cell:
                params["cell"] = str(cell)
                score += 0.5

        if "composition" in skill.params:
            comp = ctx.get("composition") or self._extract_comp(text)
            if comp:
                params["composition"] = comp
                score += 0.5

        if "res" in skill.params:
            res = ctx.get("res") or ctx.get("initial_res")
            if res:
                params["res"] = str(res)
                score += 0.5

        if "hkl" in skill.params:
            hkl = ctx.get("hkl")
            if hkl:
                params["hkl"] = str(hkl)
                score += 0.5

        if "frames" in skill.params:
            params["frames"] = ctx.get("frames") or "diff"
            score += 0.25

        if "output" in skill.params:
            params["output"] = ctx.get("output") or str(self._root / "log" / "AutoR3D")
            score += 0.25

        if "axis" in skill.params:
            axis = ctx.get("axis")
            if axis not in (None, ""):
                params["axis"] = axis
                score += 0.5

        if "method" in skill.params:
            method = "both"
            if "unit-cell" in text or "unit cell" in text or "晶胞" in text:
                method = "unit-cell"
            elif "cc" in text or "cc1" in text:
                method = "cc1"
            if "晶胞" in text:
                method = "unit-cell"
            params["method"] = method

        return (score, params)

    def _extract_param(self, text: str, keywords: list[str], pattern: str) -> str | None:
        lines = text.splitlines()
        for line in lines:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    def _extract_datasets(self, text: str) -> list[str]:
        """Extract every explicitly named dataset, preserving user order."""
        names = re.findall(
            r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9.-]*_[A-Za-z0-9][A-Za-z0-9._-]*)(?![\w.-])",
            text,
        )
        return self._deduplicate(names)

    @staticmethod
    def _deduplicate(values) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            name = str(value).strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
        return result

    def _extract_comp(self, text: str) -> str | None:
        m = re.search(r'composition\s+"([^"]+)"', text, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"composition\s+([A-Za-z0-9\s]+)(?:\s|$)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r'"([A-Z][a-z]?\d+\s+)+"', text)
        if m:
            return m.group(0).strip('"')
        return None

    def _match_score(self, skill: SkillAction, params: dict) -> float:
        total = len(skill.params or [])
        filled = sum(1 for p in (skill.params or []) if p in (params or {}))
        if total == 0:
            return 0.5
        return filled / total

    def action_catalog(self) -> str:
        """Return the execution contract shown to the language model."""

        return json.dumps(
            [
                {
                    "skill_id": skill.skill_id,
                    "description": skill.description,
                    "required_params": list(skill.params),
                    "confirmation_required": bool(skill.confirm_required),
                }
                for skill in self._skills
            ],
            ensure_ascii=False,
            indent=2,
        )

    def parse_model_response(
        self,
        response: str,
        context: dict | None = None,
    ) -> tuple[str, SkillAction | None, dict[str, object], str | None]:
        """Parse and validate a model-selected skill action.

        The model chooses the skill after reading the skill documentation. The
        executor still accepts only registered skill IDs and declared
        parameters, keeping model autonomy separate from command safety.
        """

        matches = list(self._ACTION_PATTERN.finditer(response or ""))
        if not matches:
            return response.strip(), None, {}, None
        match = matches[-1]
        visible = (response[: match.start()] + response[match.end() :]).strip()
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            return visible, None, {}, f"AI action JSON is invalid: {exc.msg}"
        if not isinstance(payload, dict):
            return visible, None, {}, "AI action must be a JSON object."
        skill_id = str(payload.get("skill_id") or "").strip()
        skill = next((item for item in self._skills if item.skill_id == skill_id), None)
        if skill is None:
            return visible, None, {}, f"AI selected an unknown skill: {skill_id or '(empty)'}"
        raw_params = payload.get("params") or {}
        if not isinstance(raw_params, dict):
            return visible, None, {}, "AI action params must be a JSON object."
        params = self._model_params(skill, raw_params, context or {})
        return visible, skill, params, None

    def _model_params(
        self,
        skill: SkillAction,
        proposed: dict,
        context: dict,
    ) -> dict[str, object]:
        aliases = {
            "dataset": ("dataset_name", "dataset"),
            "datasets": ("datasets",),
            "root": ("root", "xds_root"),
            "sg": ("sg",),
            "cell": ("cell",),
            "composition": ("composition",),
            "res": ("res", "initial_res"),
            "hkl": ("hkl",),
            "frames": ("frames",),
            "output": ("output",),
            "axis": ("axis",),
            "method": ("method", "hca_method"),
            "svg_path": ("svg_path",),
            "workdir": ("workdir",),
            "basename": ("basename",),
        }
        defaults: dict[str, object] = {
            "frames": "diff",
            "output": str(self._root / "log" / "AutoR3D"),
            "method": "both",
        }
        params: dict[str, object] = {}
        for name in skill.params:
            value = proposed.get(name)
            if value in (None, "", []):
                for key in aliases.get(name, (name,)):
                    candidate = context.get(key)
                    if candidate not in (None, "", []):
                        value = candidate
                        break
            if value in (None, "", []):
                value = defaults.get(name)
            if value in (None, "", []):
                continue
            if name == "datasets":
                if isinstance(value, str):
                    value = value.replace(",", " ").split()
                if isinstance(value, (list, tuple)):
                    params[name] = self._deduplicate(value)
            elif isinstance(value, (str, int, float, Path)):
                params[name] = str(value)
        return params

    def run_command(self, command: str, workdir: str | None = None) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=workdir or str(self._root),
                timeout=300,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out after 300 seconds"
        except Exception as e:
            return -1, "", str(e)

    @property
    def available_skills(self) -> list[SkillAction]:
        return list(self._skills)
