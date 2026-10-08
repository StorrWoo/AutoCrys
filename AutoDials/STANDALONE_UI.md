# 独立 DIALS 测试窗口

这是独立小窗口，不影响 AutoCrys 主界面或其启动命令。它既可处理单数据集，也可选择多个已成功处理的 run，执行 `combine_experiments → cosym → scale → merge` 并导出 MTZ、SHELX HKL/INS。

在 Windows 中双击：

```bat
AutoDials\start_ui.bat
```

默认使用 `C:\dials\python.exe`（已包含 Tk）。也可先设置 `AUTODIALS_PYTHON`，或直接带入实验目录：

```bat
set AUTODIALS_PYTHON=C:\dials\python.exe
AutoDials\start_ui.bat "C:\path\to\dataset"
```

在 WSL bash 中直接启动（自动使用 AutoCrys 环境的 Python）：

```bash
autodials
```

安装项目后可直接使用 `autodials` 命令；也可运行 `AutoDials/start_ui.sh`。可传入实验文件夹完整路径：

```bash
autodials /path/to/AutoCrys/Data/sample3_1
```

从源码树启动：

```bash
conda activate AutoCrys
cd /path/to/AutoCrys
bash AutoDials/start_ui.sh
```

直接带入一个实验：

```bash
bash AutoDials/start_ui.sh Data/sample3_1
```

1. 选择**实验文件夹**，其中应包含 `Continuous 3D ED (cRED2) parameters.txt` 与 `diff/frame_*.tif`。不要只选 `diff`。
2. DIALS Python 默认 `/mnt/c/dials/python.exe`，使用已安装的 Windows DIALS。程序自动转换 WSL 路径。
3. **目标晶胞**可留空，或输入 `a b c α β γ`。它是索引先验，随后允许精修，不会把六个数锁死。最后显示实际精修晶胞。
4. **空间群**可输入国际编号 `1–230`，或名称，例如 `P1`、`P212121`、`Pnma`。留空按 P1。晶胞与空间群不兼容时会停止并提示。
5. **使用 XDS 几何**默认关闭。打开后自动使用该实验 `diff/p/GXPARM.XDS`（优先）或 `XPARM.XDS`；也可手选。这里只采用实验几何，晶胞/空间群仍来自界面设置，不采用 XDS 的晶体取向解。
6. 点击 **运行 DIALS**。窗口保持响应，下方实时打印日志和最终结果。**停止**按钮会停止正在运行的处理进程并保留日志。
7. 合并时，在“多数据集合并”区选择包含多个数据集的根目录，点击 **扫描成功 run**，用 Ctrl/Shift 选择至少两个 run，填写结果名后点击 **合并所选 run**。所选 run 必须具有 `work/integrated.expt`、`work/integrated.refl` 和成功的 `summary.json`，并且空间群一致。

每次运行单独保存，不覆盖上一轮：

```text
实验文件夹/
  AutoDials/
    run_日期_时间_唯一后缀/
      实验文件夹名.mtz
      实验文件夹名.hkl
      实验文件夹名.ins
      summary.json
      request.json
      run.log
      各阶段日志
      work/                    # CBF、PHIL、expt、refl 等中间文件
数据根目录/
  AutoDials/
    merge_日期_时间_唯一后缀/
      merged.mtz
      merged.hkl
      merged.ins
      summary.json
      request.json
      dials.merge.html
```

单数据集 MTZ 与 HKL 是**未标度的积分强度**；HKL 是 SHELX HKLF 4，采用轮廓积分。合并流程先经 cosym 和 scale，输出 merged MTZ 与 scaled-unmerged SHELX HKLF4。

同名 `.ins` 自动使用最终 `integrated.expt` 中的精修结果：`CELL` 写入结果晶胞，`LATT/SYMM` 写入结果空间群的对称操作。导出后回读并验证晶胞、全部对称操作与最终实验模型一致。INS 的元素组成目前仍是占位的 `SFAC C H / UNIT 0 0`，需要按样品填写，不代表可直接开展完整结构精修。

全零占位帧自动排除，但保持帧号和角度间隔。输入晶胞可能在精修中变化；“运行成功”表示处理和格式校验通过，不表示空间群已被证实或强度质量已通过结构解析验证。

## 已验证

- 在 WSL 的 AutoCrys Python 中打开实际 Tk 窗口，并调用 Windows DIALS，验证跨系统路径和日志回传。
- `sample3_1`，输入晶胞 `19.70 19.88 13.17 90 90 90`、空间群 `62`、XDS 几何开启，完整按钮流程成功，结果空间群 Pnma。
- 实际导出 `sample3_1.mtz`（8,623 条）及 `sample3_1.hkl`（11,076 条），使用 iotbx 回读两种文件验证。
- 试跑中精修晶胞为 `20.2278 20.3070 13.7088 90 90 90`，并非锁定的输入晶胞；需要继续评估几何/精修设置，不能将该试跑结果视为最终标定结果。
- 停止操作、无效晶胞、空间群编号/晶胞兼容性、XDS 开关与缺失几何文件、独立输出目录、空格/中文实验名的请求生成均有测试。

维护入口全部位于顶层 `AutoDials/`：`mini_ui.py`（独立界面）、`ui_support.py`（路径、输入和请求）、`worker.py`/`ui_worker.py`（处理与导出）、`multi.py`（HCA/合并）、`prepare_cred2.py` 和 `dials_compat.py`。
