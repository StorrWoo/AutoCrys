"""Standard-library-only helpers shared by the standalone UI and its worker."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from datetime import datetime

EVENT_PREFIX = "@@DIALS_UI@@"


def parse_cell(text):
    if not text.strip():
        return None
    try:
        values = [float(v) for v in re.split(r"[\s,;，；]+", text.strip())]
    except ValueError as error:
        raise ValueError("晶胞需要 6 个数字：a b c α β γ") from error
    if len(values) != 6 or not all(math.isfinite(v) for v in values):
        raise ValueError("晶胞需要 6 个有限数字：a b c α β γ")
    if min(values[:3]) <= 0 or not all(0 < a < 180 for a in values[3:]):
        raise ValueError("晶胞边长须大于 0，角度须介于 0 和 180 度之间")
    ca, cb, cg = (math.cos(math.radians(a)) for a in values[3:])
    if 1 - ca * ca - cb * cb - cg * cg + 2 * ca * cb * cg <= 1e-10:
        raise ValueError("这些角度不能构成有效晶胞")
    return values


def find_xparm(dataset):
    dataset = Path(dataset)
    for folder in (dataset / "diff" / "p", dataset / "p", dataset):
        for name in ("GXPARM.XDS", "XPARM.XDS"):
            path = folder / name
            if path.is_file():
                return path
    return None


def local_path(text):
    text = str(text).strip().strip('"')
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", text):
        converted = subprocess.run(["wslpath", "-u", text], capture_output=True, text=True, check=True)
        text = converted.stdout.strip()
    return Path(text).expanduser().resolve()


def native_path(path, executable):
    """Pass Windows paths to Windows Python even when the UI runs in WSL."""
    path = Path(path).resolve()
    if os.name == "nt" or Path(executable).suffix.lower() != ".exe":
        return str(path)
    result = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, check=True)
    if not result.stdout.strip():
        raise ValueError(f"无法转换 Windows 路径：{path}")
    return result.stdout.strip()


def default_python():
    for candidate in (os.environ.get("DIALS_PYTHON", ""), *dials_python_candidates()):
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def dials_python_candidates():
    """Known DIALS interpreters, covering Windows installs and Linux/conda."""
    if os.name == "nt":
        return ["C:/dials/python.exe", "C:/DIALS/python.exe"]
    home = Path.home()
    return [
        "/mnt/c/dials/python.exe", "/mnt/c/DIALS/python.exe",
        str(home / "miniconda3/envs/dials/bin/python"),
        str(home / "miniforge3/envs/dials/bin/python"),
        str(home / "mambaforge/envs/dials/bin/python"),
        str(home / "anaconda3/envs/dials/bin/python"),
        str(home / "dials/bin/python"),
    ]


def probe_dials(python, timeout=60):
    """Verify a DIALS interpreter and required packages before submitting work."""
    python = Path(python)
    if not python.is_file():
        raise ValueError("找不到 DIALS Python，请检查程序路径")
    script = (
        "import importlib.metadata as m, json, platform, sys\n"
        "mods = {}\n"
        "for name in ('dials', 'dxtbx', 'cctbx-base', 'scitbx', 'gemmi'):\n"
        "    try:\n"
        "        mods[name] = m.version(name)\n"
        "    except Exception:\n"
        "        pass\n"
        "import numpy, PIL, scipy\n"
        "print('@@DIALS_PROBE@@' + json.dumps({'python': sys.version.split()[0], 'modules': mods, "
        "'numpy': numpy.__version__, 'pillow': PIL.__version__, 'scipy': scipy.__version__, "
        "'platform': platform.platform(), 'executable': sys.executable}))\n"
    )
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run([str(python), "-c", script], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, **flags)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"无法运行 DIALS Python：{error}") from error
    for line in result.stdout.splitlines():
        if line.startswith("@@DIALS_PROBE@@"):
            return json.loads(line[len("@@DIALS_PROBE@@"):])
    detail = (result.stderr or result.stdout).strip().splitlines()
    tail = detail[-1] if detail else "未知错误"
    raise ValueError("DIALS 运行时不可用：" + tail)


def validate_inputs(dataset, python, cell_text="", space_group="", d_min="1.0", use_xds=False, xparm=""):
    dataset, python = local_path(dataset), local_path(python)
    if not dataset.is_dir() or len(list(dataset.glob("*cRED2*parameters*.txt"))) != 1:
        raise ValueError("请选择包含 cRED2 参数文件和 diff 子目录的实验文件夹")
    if not any((dataset / "diff").glob("frame_*.tif")):
        raise ValueError("没有找到 diff/frame_*.tif")
    if not python.is_file():
        raise ValueError("找不到 DIALS Python，请检查程序路径")
    # Preserve normal names, including spaces and Chinese; reject names that
    # Windows cannot represent rather than silently changing experiment names.
    if python.suffix.lower() == ".exe" and re.search(r'[<>:"/\\|?*\x00-\x1f]', dataset.name):
        raise ValueError("实验名包含 Windows 输出文件名不支持的字符")
    cell = parse_cell(cell_text)
    space_group = space_group.strip() or "P1"
    if "\n" in space_group or "\r" in space_group:
        raise ValueError("空间群必须写在一行内")
    if space_group.isdigit() and not 1 <= int(space_group) <= 230:
        raise ValueError("空间群编号须为 1–230")
    try:
        resolution = float(d_min)
    except ValueError as error:
        raise ValueError("分辨率必须是正数") from error
    if not math.isfinite(resolution) or resolution <= 0:
        raise ValueError("分辨率必须是正数")
    geometry = None
    if use_xds:
        geometry = local_path(xparm) if xparm.strip() else find_xparm(dataset)
        if geometry is None or not geometry.is_file():
            raise ValueError("已启用 XDS 几何，但没有找到 XPARM.XDS / GXPARM.XDS")
    return dict(dataset=dataset, python=python, cell=cell, space_group=space_group,
                d_min=resolution, xparm=geometry)


def create_job(inputs, output_parent=None):
    """Each click gets a separate directory; never overwrite a previous run."""
    dataset, python = inputs["dataset"], inputs["python"]
    parent = Path(output_parent) if output_parent else dataset / "AutoDials"
    parent.mkdir(parents=True, exist_ok=True)
    run = parent / (datetime.now().strftime("run_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
    run.mkdir()
    request = {"action": "process", "dataset": native_path(dataset, python), "run_directory": native_path(run, python),
               "name": dataset.name, "unit_cell": inputs["cell"], "space_group": inputs["space_group"],
               "d_min": inputs["d_min"], "xparm": native_path(inputs["xparm"], python) if inputs["xparm"] else None}
    config = run / "request.json"
    config.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    worker = Path(__file__).with_name("worker.py")
    command = [str(python), "-u", native_path(worker, python), native_path(config, python)]
    return run, command


def discover_merge_runs(root):
    root = local_path(root)
    runs = []
    for summary in root.rglob("summary.json"):
        run = summary.parent
        if run.parent.name not in ("AutoDials", "DIALS"):
            continue
        expt = run / "work" / "integrated.expt"
        refl = run / "work" / "integrated.refl"
        if not expt.is_file() or not refl.is_file() or not expt.stat().st_size or not refl.stat().st_size:
            continue
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("status") != "success":
            continue
        dataset = run.parent.parent
        runs.append({"run": run, "dataset": dataset, "run_id": run.name,
                     "cell": data.get("cell"), "space_group": data.get("space_group"),
                     "dials_version": data.get("dials_version")})
    return sorted(runs, key=lambda item: item["run"].stat().st_mtime, reverse=True)


def create_merge_job(root, runs, python, name="merged"):
    root, python = local_path(root), local_path(python)
    runs = [local_path(run) for run in runs]
    name = name.strip()
    if not root.is_dir():
        raise ValueError("合并数据根目录不存在")
    if not python.is_file():
        raise ValueError("找不到 DIALS Python，请检查程序路径")
    if len(runs) < 2:
        raise ValueError("请至少选择两个成功的 DIALS run")
    if Path(name).name != name or not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise ValueError("合并结果名称包含无效字符")
    available = {item["run"]: item for item in discover_merge_runs(root)}
    if any(run not in available for run in runs):
        raise ValueError("选择中包含不完整、失败或不属于当前根目录的 run")
    worker = Path(__file__).with_name("worker.py")
    if not worker.is_file():
        raise ValueError(f"找不到 AutoDials 合并 worker：{worker}")
    parent = root / "AutoDials"
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / (datetime.now().strftime("merge_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
    output.mkdir()
    request = {"action": "merge", "run_directory": native_path(output, python),
               "inputs": [native_path(run, python) for run in runs],
               "run_ids": [run.name for run in runs],
               "labels": [str(available[run]["dataset"].relative_to(root))
                          if available[run]["dataset"].is_relative_to(root) else str(available[run]["dataset"])
                          for run in runs], "name": name}
    config = output / "request.json"
    config.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [str(python), "-u", native_path(worker, python), native_path(config, python)]
    return output, command


def decode_event(line):
    if not line.startswith(EVENT_PREFIX):
        return None
    try:
        return json.loads(line[len(EVENT_PREFIX):])
    except (ValueError, TypeError):
        return None
