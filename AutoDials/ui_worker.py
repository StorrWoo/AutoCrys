"""DIALS worker for AutoDials. All heavy imports run in DIALS' own Python."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback

try:
    from .ui_support import EVENT_PREFIX, parse_cell
except ImportError:
    from ui_support import EVENT_PREFIX, parse_cell


def configure_runtime():
    root = Path(sys.executable).parent
    if os.name == "nt":
        dirs = [root, root / "Library" / "bin", root / "Scripts", root / "bin"]
        os.environ["PATH"] = os.pathsep.join(map(str, dirs)) + os.pathsep + os.environ.get("PATH", "")
        # Keep handles alive for extension modules imported later in this process.
        global DLL_HANDLES
        DLL_HANDLES = [os.add_dll_directory(str(p)) for p in dirs if p.is_dir()]
    os.environ["PYTHONUTF8"] = "1"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def event(kind, **values):
    print(EVENT_PREFIX + json.dumps(dict(kind=kind, **values), ensure_ascii=False), flush=True)


def runtime_metadata():
    """Record the exact DIALS runtime so a run stays reproducible later."""
    import importlib.metadata as metadata
    import platform
    versions = {}
    for name in ("dials", "dxtbx", "cctbx-base", "scitbx", "gemmi"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return {"dials_version": versions.get("dials"), "dials_modules": versions,
            "platform": platform.platform(), "python_version": platform.python_version()}


class Cancelled(Exception):
    pass


def canonical_symmetry(cell, sg_text):
    try:
        from .dials_compat import apply
    except ImportError:
        from dials_compat import apply
    apply()
    from cctbx import sgtbx, uctbx
    text = str(sg_text).strip() or "P1"
    sg = sgtbx.space_group_info(number=int(text)) if text.isdigit() else sgtbx.space_group_info(symbol=text)
    if cell is not None:
        cell = parse_cell(" ".join(map(str, cell)))
        uc = uctbx.unit_cell(cell)
        if not sg.group().is_compatible_unit_cell(uc):
            raise ValueError("晶胞与指定空间群不兼容，请检查轴顺序、角度或空间群设置")
    return cell, str(sg)


def stop_child(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_child(command, cwd, logfile, cancel_file):
    if cancel_file.exists():
        raise Cancelled("用户已停止运行")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
    lines = queue.Queue()
    def read_output():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            process.stdout.close()
            lines.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    finished = False
    try:
        with logfile.open("w", encoding="utf-8") as log:
            log.write("COMMAND: " + json.dumps(command, ensure_ascii=False) + "\n")
            while not finished or process.poll() is None:
                if cancel_file.exists():
                    raise Cancelled("用户已停止运行")
                try:
                    line = lines.get(timeout=0.1)
                    if line is None:
                        finished = True
                    else:
                        log.write(line)
                        log.flush()
                        print(line, end="", flush=True)
                except queue.Empty:
                    continue
            code = process.wait()
        if code != 0:
            raise RuntimeError(f"处理失败（退出码 {code}），详见 {logfile}")
    finally:
        stop_child(process)
        reader.join(timeout=2)


def process_job(config):
    scripts = Path(__file__).resolve().parent
    run = Path(config["run_directory"])
    source = Path(config["dataset"])
    name = config["name"]
    if source.name != name or Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("实验文件夹名与输出名不匹配")
    if any((run / f"{name}{suffix}").exists() for suffix in (".mtz", ".hkl", ".ins")):
        raise ValueError("结果已存在，拒绝覆盖。请重新启动一次任务")
    work = run / "work"
    cancel = run / "cancel.request"
    event("stage", stage="检查参数", current=0, total=8)
    cell, sg = canonical_symmetry(config.get("unit_cell"), config.get("space_group", "P1"))
    print(f"实验：{name}\n空间群：{sg}\n目标晶胞：{cell or '自动索引'}", flush=True)
    print("XDS 几何：" + (config.get("xparm") or "关闭，使用 cRED2 参数"), flush=True)
    print("晶胞作为索引目标，随后精修；最终数值以结果为准。", flush=True)
    prepare = [sys.executable, "-u", str(scripts / "prepare_cred2.py"), str(source), str(work), "--d-min", str(config["d_min"])]
    if config.get("xparm"):
        prepare += ["--xparm", config["xparm"]]
    event("stage", stage="转换 TIFF / 检查空白帧", current=1, total=8)
    run_child(prepare, run, run / "prepare.log", cancel)
    # Only write validated, canonical crystallographic values to PHIL.
    index_text = ("refinement.parameterisation.detector.fix=distance\n"
                  "refinement.parameterisation.goniometer.fix=None\n"
                  "refinement.reflections.outlier.nproc=1\n"
                  f"indexing.known_symmetry.space_group={json.dumps(sg)}\n")
    if cell:
        index_text += "indexing.known_symmetry.unit_cell=" + ",".join(map(str, cell)) + "\n"
    (work / "index.phil").write_text(index_text, encoding="utf-8")
    def quoted_path(path):
        return json.dumps(str(path), ensure_ascii=False)
    mtz_path, hkl_path = run / f"{name}.mtz", run / f"{name}.hkl"
    ins_path = run / f"{name}.ins"
    commands = [
        ("导入", "import", ["import.phil"], "imported.expt"),
        ("寻峰", "find_spots", ["imported.expt", "spots.phil"], "strong.refl"),
        ("索引", "index", ["imported.expt", "strong.refl", "index.phil"], "indexed.expt"),
        ("精修", "refine", ["indexed.expt", "indexed.refl", "detector.fix=distance", "goniometer.fix=None", "refinement.reflections.outlier.nproc=1"], "refined.expt"),
        ("积分", "integrate", ["refined.expt", "refined.refl", "integrate.phil"], "integrated.refl"),
        ("导出 MTZ", "export", ["integrated.expt", "integrated.refl", "format=mtz", f"mtz.hklout={quoted_path(mtz_path)}"], mtz_path),
        ("导出 SHELX HKL / INS", "export", ["integrated.expt", "integrated.refl", "format=shelx", "intensity=profile", f"shelx.hklout={quoted_path(hkl_path)}", f"shelx.ins={quoted_path(ins_path)}"], hkl_path),
    ]
    for i, (label, module, params, expected) in enumerate(commands, 2):
        event("stage", stage=label, current=i, total=8)
        command = [sys.executable, "-u", str(scripts / "dials_compat.py"), module, *params]
        run_child(command, work, run / f"{i:02d}_{module}.log", cancel)
        artifact = work / expected
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise RuntimeError(f"{label}未生成预期文件：{artifact}")
    if cancel.exists():
        raise Cancelled("用户已停止运行")
    report = summarize(work, mtz_path, hkl_path, ins_path)
    report.update(status="success", experiment=name, requested_cell=cell, requested_space_group=sg,
                  xds_geometry=bool(config.get("xparm")), mtz=str(mtz_path), hkl=str(hkl_path), ins=str(ins_path),
                  **runtime_metadata())
    (run / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    event("result", **report)
    return report


def validate_ins(work, ins_path):
    """Check CELL and all LATT/SYMM operations against the final DIALS model."""
    from dxtbx.model.experiment_list import ExperimentListFactory
    from iotbx import builders
    from iotbx.shelx import command_stream, crystal_symmetry_parser
    experiments = ExperimentListFactory.from_json_file(str(work / "integrated.expt"), check_format=False)
    crystal = experiments[0].crystal
    expected_cell = crystal.get_recalculated_unit_cell() or crystal.get_unit_cell()
    builder = builders.crystal_symmetry_builder()
    crystal_symmetry_parser(command_stream(filename=str(ins_path)), builder).parse()
    actual = builder.crystal_symmetry
    if any(abs(a - b) > 1e-4 for a, b in zip(actual.unit_cell().parameters(), expected_cell.parameters())):
        raise ValueError("INS 晶胞与最终精修结果不一致")
    if actual.space_group() != crystal.get_space_group():
        raise ValueError("INS 的 LATT/SYMM 与最终空间群不一致")
    return {"ins_cell": actual.unit_cell().parameters(),
            "ins_space_group": str(actual.space_group_info()), "ins_geometry_verified": True}


def summarize(work, mtz_path, hkl_path, ins_path=None):
    from dxtbx.model.experiment_list import ExperimentListFactory
    from dials.array_family import flex
    from iotbx import mtz
    from iotbx.shelx import hklf
    exp = ExperimentListFactory.from_json_file(str(work / "integrated.expt"), check_format=False)[0]
    if str(exp.beam.get_probe()) != "electron":
        raise ValueError("导出实验不是 electron 模式")
    strong = flex.reflection_table.from_file(str(work / "strong.refl"))
    indexed = flex.reflection_table.from_file(str(work / "indexed.refl"))
    reflections = flex.reflection_table.from_file(str(work / "integrated.refl"))
    n_indexed = indexed.get_flags(indexed.flags.indexed).count(True)
    data = mtz.object(str(mtz_path))
    with hkl_path.open() as stream:
        shelx_data = hklf.reader(file_object=stream).as_miller_arrays(merge_equivalents=False)[0]
    if not data.n_reflections() or not shelx_data.size():
        raise ValueError("导出的反射文件为空")
    if data.space_group().type().number() != exp.crystal.get_space_group().type().number():
        raise ValueError("MTZ 空间群与精修结果不一致")
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    used = indexed.get_flags(indexed.flags.used_in_refinement)
    selected = indexed.select(used)
    rmsd = None
    if len(selected):
        difference = selected["xyzcal.px"] - selected["xyzobs.px.value"]
        rmsd = [(flex.mean(v * v)) ** 0.5 for v in difference.parts()]
    report = {"frames": manifest["frames"], "excluded_zero_frames": len(manifest["zero_frames"]),
            "strong_spots": len(strong), "indexed_spots": n_indexed,
            "indexed_percent": 100 * n_indexed / max(1, len(strong)),
            "index_rmsd_px": rmsd, "cell": exp.crystal.get_unit_cell().parameters(),
            "space_group": str(exp.crystal.get_space_group().info()),
            "integrated_reflections": len(reflections), "mtz_reflections": data.n_reflections(),
            "hkl_reflections": shelx_data.size(), "intensity_status": "unscaled",
            "hkl_format": "SHELX HKLF 4", "hkl_intensity": "profile"}
    if ins_path is not None:
        report.update(validate_ins(work, ins_path))
    return report


def main():
    configure_runtime()
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    run = Path(config["run_directory"])
    try:
        process_job(config)
        return 0
    except Exception as error:
        status = "cancelled" if isinstance(error, Cancelled) else "failed"
        report = {"status": status, "error": str(error)}
        (run / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        event("error", **report)
        if status == "failed":
            traceback.print_exc()
        return 2 if status == "cancelled" else 1


if __name__ == "__main__":
    raise SystemExit(main())
