# AutoCrys 0.1 详细操作手册

本文面向已经安装或已经部署过 AutoCrys 的操作人员，覆盖日常更新、数据准备、GUI、
命令行、输出解释、安全重跑和故障排查。首次安装见
[GETTING_STARTED.md](GETTING_STARTED.md)。

本文按 2026-09-22 的代码实现编写。版本更新后，`python main.py ... --help` 和脚本
自身的 `--help` 是参数名称与默认值的最终依据。

## 1. 一页式标准流程

典型 3DED 工作流如下：

```text
已有系统自检/原位适配
        ↓
准备 dataset/diff/frame_*.tif、dataset/diff/p/XDS.INP
        ↓
AutoXDS：处理、自动分辨率截止、XDSCONV
        ↓
可选 HCA：Unit-cell / CC1 聚类，选择 cutoff
        ↓
可选合并：XSCALE(MERGE=FALSE) → XDSCONV → merged .hkl
        ↓
可选 AutoDials：DIALS 独立后端（cRED2→miniCBF→import/index/integrate→MTZ/HKL/INS）
        ↓
AutoR3D：cRED2 几何、峰提取、倒易空间重构、hkl slices、QC
        ↓
AutoSolve：生成 .ins → SHELXT → .res
        ↓
SHELXL / AutoRefine：隔离目录短周期精修、指标和模型诊断
        ↓
可选 Olex2：人工检查和进一步建模
```

每次打开新终端先执行：

```bash
conda activate AutoCrys
cd /path/to/AutoCrys
```

启动 GUI：

```bash
autocrys
```

## 2. 已部署系统的更新自检

### 2.1 为什么先自检

目标机可能已经有 Miniconda/Miniforge、不同路径的 AutoCrys 环境、`bash` 或
`bash2`、NVM 管理的 OpenClaw、已安装的 XDS/SHELX 和本机数据。更新时不应假设
所有路径都与源机器一致，更不应先删除环境再重建。

只读检查：

```bash
cd /path/to/AutoCrys
python3 scripts/update_self_check.py
```

已安装的等价入口是 `autocrys-self-check`；更新旧部署时优先直接运行脚本，避免旧入口
仍指向上一份工作副本。

JSON 输出：

```bash
python3 scripts/update_self_check.py --json
```

原位适配：

```bash
python3 scripts/update_self_check.py --apply
```

### 2.2 自检结果怎么读

- `OK`：当前目标机已满足该项；
- `WARN`：可选组件缺失，或当前任务使用该组件前需要处理；
- `FAIL`：运行边界不满足，例如不是 Linux/WSL；
- `action`：建议的最小修复，不代表必须重装全部环境。

检查范围包括 WSL/WSLg、CPU、shell、Conda、AutoCrys 环境、Python、依赖、入口、
XDS/SHELX、OpenClaw/Gateway 和数据标记。脚本不会显示任何 API key。

### 2.3 `--apply` 会做什么

- 对已有 AutoCrys Conda 环境执行原位 `conda env update`，不删除、不重建、不
  `--prune`；
- 重新执行 editable install，使命令入口指向当前项目；
- 修复 用户自行安装的 SHELXL 权限；
- 必要时解压 SHELXT 到 `~/.local/bin`；
- 根据实际 shell 选择 profile；`bash2` 与 `bash` 都使用 `~/.bashrc`；
- 仅在 AI 已配置为 `agent` 且普通 `openclaw` 不可执行时，为 NVM 中已有的
  OpenClaw 创建 PATH-safe wrapper，并更新现有 `llm.agent.command`；
- 完成后自动再次检查。

脚本不会安装/升级 WSL、创建或删除 Conda 环境、下载 XDS、启动/停止 Gateway、
覆盖数据集或打印密钥。缺失这些基础项时再按安装文档单独处理。

### 2.4 手工检查 OpenClaw

如果自检显示 `openclaw_gateway: WARN`，且确实使用 `llm.mode = "agent"`：

```bash
openclaw --version
openclaw gateway status
```

NVM 用户应先让当前 shell 加载 NVM；也可以再次运行 `--apply`，让 AutoCrys 使用
自检生成的 wrapper。不要为了普通 `cloud`、`local` 或 `offline` 模式安装
OpenClaw。

## 3. 数据目录规范

### 3.1 推荐的统一布局

```text
AutoCrys/
├── Data/
│   └── experiment_001/
│       ├── Continuous 3D ED (cRED2) parameters.txt
│       └── diff/
│           ├── frame_0001.tif
│           ├── frame_0002.tif
│           ├── ...
│           └── p/
│               ├── XDS.INP
│               ├── CORRECT.LP           # XDS 后生成
│               ├── XDS_ASCII.HKL        # XDS 后生成
│               ├── temp.hkl             # XDSCONV 后生成
│               └── experiment_001.hkl   # AutoXDS 复制的命名输出
└── log/
    ├── AutoR3D/
    ├── AutoSolve/
    └── AutoRefine/
```

### 3.2 AutoXDS 的硬性条件

- 数据集必须有 `<dataset>/diff/p/XDS.INP`；AutoXDS 不负责从零生成它；
- 数据集发现逻辑只把父目录名为 `p`、上一级为 `diff` 的 `XDS.INP` 识别为标准
  数据集；
- 默认图像模板是 `../frame_????.tif tiff`，相对于 `diff/p/`，即 TIFF 应位于
  `diff/`；
- TIFF 编号必须与 `XDS.INP` 的范围和模板一致；
- 数据集名称在搜索根中应唯一。重名时 CLI 会拒绝并列出多个匹配项。

### 3.3 AutoR3D 的硬性条件

- 参数文件位于**数据集根目录**；
- 文件名匹配 `*cRED2*parameters*.txt` 或 `*3D ED*parameters*.txt`；
- 默认帧目录为 `diff/`，默认模式为 `frame_*.tif`；
- 旧数据若使用 `raw/`，启动时指定 `--frames raw`；
- 全零 TIFF 会作为 placeholder 保留几何位置，但不参与强度重构；
- 没有可用的非零帧，或没有提取到显著观测，重构会明确失败。

### 3.4 XDS 参考对 AutoR3D 的影响

AutoR3D 会先查找 `diff/p/`，再查找若干兼容的嵌套 `*/p` 路径，读取：

```text
CORRECT.LP
XDS_ASCII.HKL
XDS.INP
GXPARM.XDS
```

有可用参考时，AutoR3D 从中获得旋转轴、晶胞、倒易基矢、束心、距离、波长和空间
群；`Indexed` 显示和 hkl slices 需要取向矩阵。没有 XDS 参考时仍可用手动旋转轴做
几何重构，但不能把结果视为完整的 hkl 索引结果。

## 4. 启动方式和界面结构

### 4.1 启动器

```bash
autocrys          # AI 是否显示由 config 决定，紧凑 Console
autocrys-ai       # 强制显示 Assistant，紧凑 Console
autocrys-base     # 强制隐藏 Assistant，紧凑 Console
autocrys-main     # AI 由 config 决定，完整 Console
autor3d-ui        # 同一个集成 GUI
python main.py    # 普通 GUI
python main.py ai # 强制 AI GUI
```

直接预载一个数据集：

```bash
autocrys /path/to/dataset
autocrys /path/to/legacy-dataset --frames raw
```

### 4.2 顶层标签

- **AutoXDS**：数据处理、汇总、HCA 和合并；
- **AutoDials**：可选的 DIALS 独立后端（cRED2→miniCBF→DBF 流程、HCA/合并、下游交接）；
- **AutoR3D**：帧查看、峰搜索、重构、3D panel 和 slices；
- **AutoSolve**：SHELXT、SHELXL、结构预览和 Olex2；
- **Assistant**：只有 AI 启用时出现；
- **Console**：右侧持续显示处理状态；`autocrys-main` 保留完整子进程细节。

## 5. AutoXDS GUI 操作

### 5.1 选择根目录和数据集

1. `Root` 默认是项目的 `Data/`；也可以指向其他数据集合根目录。
2. 点击 **Dataset List** 扫描所有标准 `diff/p/XDS.INP`。
3. 点击 **Browse Datasets** 多选，或点击 **Select All**。
4. 确认选择框中是本次真正要处理的数据集；批处理会逐个修改对应 `XDS.INP`。

### 5.2 Cell、Space Group、Resolution

- `Unit Cell` 格式：`a b c alpha beta gamma`，必须正好六个数；
- `Space Group` 在 AutoXDS 中是 XDS 空间群**编号**；
- Cell 和 SG 必须同时填写或同时留空；
- `Resolution` 可以只写高分辨率端，例如 `1.0`，也可以写 `20 1.0`。

**Set Cell/SG/Res** 的实际语义：

- Cell+SG 非空：写入 `UNIT_CELL_CONSTANTS` 和 `SPACE_GROUP_NUMBER`；
- Cell 为空：清除/禁用已有 cell 和 SG 约束；单独填写 SG 会被清空；
- Resolution 非空：写入 `INCLUDE_RESOLUTION_RANGE`；
- 该按钮只改 `XDS.INP`，不运行 XDS。

### 5.3 Process & Convert

点击前检查：

- `xds`、`xdsconv` 在当前 GUI 进程继承的 `PATH` 中；
- `XDS.INP` 的探测器、角度、数据范围等参数正确；
- Cell 和 SG 要么都填，要么都空。

执行内容：

1. 默认把旧的 `*.HKL`、`*.LP`、`XPARM.XDS`、`GXPARM.XDS`、
   `SPOT.XDS`、`XDSCONV.INP`、`XSCALE.*`、`temp.hkl` 等移入时间戳目录；
2. 备份 `XDS.INP`；
3. 重置 JOB 行并写入图像模板、cell/SG、分辨率；
4. 运行 XDS；
5. 若输出包含 `INSUFFICIENT PERCENTAGE (< 50%)`，以
   `DEFPIX INTEGRATE CORRECT` 尝试恢复；
6. 没有显式跳过时，从 `CORRECT.LP` 选择分辨率截止并重跑后段；
7. 用 XDSCONV 输出 `temp.hkl`，再复制为 `<dataset>.hkl`；
8. 更新 `summary.txt`。

当 Cell/SG 留空时，GUI 在数据集的**第一次** AutoXDS 运行中保留原始
`XDS.INP` 的 cell/SG；以后再次无约束运行会清除它们。这是有意的状态行为，重跑前应
确认是否需要保留声明晶胞。

结果区重点检查：

- `Final` 是否为 `single_dataset_finished`；
- `XDSCONV` 是否为 `done`；
- Cell、SG、ISa、R factor、Completeness、Resolution；
- `DatasetHkl` 或 `Output` 是否指向非空文件。

### 5.4 HCA Run

HCA 至少需要两个已成功处理的数据集。失败的数据集会被跳过。可选方法：

- `unit-cell`：基于晶胞向量距离；
- `cc1`：基于共同反射强度相关；
- `both`：同时计算两种树。

运行后：

1. 用 **UnitCell** / **CC** 切换树；
2. 在 dendrogram 上点击一个高度选择 cutoff；
3. 检查下方列出的每个 cluster、平均晶胞和空间群；
4. singleton cluster 不会参与按聚类合并。

聚类只是辅助决策。不要仅因树上距离近就合并空间群、晶胞或数据质量明显不一致的
数据集。

### 5.5 Merge & Conv

- 已运行 HCA 且选了 cutoff：每个含至少两个数据集的 cluster 分别合并；
- 未运行 HCA：把当前所有选中数据集作为一个组；
- 每组输出名称在 GUI 中为 `cluster_1`、`cluster_2` 等；
- 程序按 summary 中 R factor 排序，把工作目录放在最优数据集的 `diff/p/`；
- 空间群不一致时默认拒绝；GUI 不自动打开 `--allow-sg-mismatch`；
- XSCALE 和 XDSCONV 都强制 `MERGE=FALSE`，保留单个观测，避免下游 `Rint`
  因预合并而人为变为 0。

主要输出：

```text
cluster_N.ahkl
cluster_N.hkl
XSCALE.INP
XSCALE.LP
XDSCONV.INP
```

## 6. AutoR3D GUI 操作

### 6.1 导入数据集

1. 在 AutoR3D 的 Reconstruction 区点击 **Browse Dataset**；
2. 选择包含参数文件和 `diff/` 的数据集根目录；
3. 点击 **Import Dataset**；
4. 确认帧数、zero placeholder 数、图像尺寸和旋转轴来源。

导入时会读取第一张图、所有帧统计、cRED2 参数和可用 XDS 参考。检测到 XDS 时
束心、旋转轴和几何优先使用 XDS；否则使用手工值。

### 6.2 峰搜索参数

- `Center X/Y`：直束中心，XDS 存在时自动填入；
- `Mask radius`：直束中心排除半径；
- `Edge guard`：中心 mask 外的额外保护带；空值按总排除约 20 px 计算；
- `Spot size`：圆形局部极大值邻域半径；
- `Threshold`：峰强度阈值；
- **Peak Search**：逐帧预览峰，不等于完整 3D 重构。

建议一次只调整一个参数。峰过多先提高 Threshold 或 Spot size；峰过少先适度降低
Threshold。不要用过大的中心 mask 隐藏整个低角区域。

### 6.3 完整重构

Reconstruction 区：

- `Output`：输出目录；GUI 默认是 `Data/AutoR3D`，建议为不同数据集使用独立目录；
- `Voxel`：q 空间体素大小，默认 `0.01 Å⁻¹`，必须为正；
- `Max peaks`：导出的峰候选上限，默认 2000；
- **Run Reconstruction**：执行峰提取、几何映射、gridding、候选峰、QC、导出和
  slices。

GUI 当前固定使用：圆形 peak footprint、trilinear gridding、最多 8 个左右的并行
帧 worker（由 CPU 决定）、默认 1000 spots/frame、99.5 percentile 候选峰阈值。
需要改变高级参数时使用 CLI。

### 6.4 3D Panel

完成后点 **Load 3D Panel**。内置 panel 支持：

- Threshold、点大小、Brightness、Contrast、Gamma；
- `a*`、`b*`、`c*`、Iso 和 Fit 视图；
- `Cell/grid`；
- 有索引观测时启用 `Indexed`；
- 左键拖动旋转，右键/中键拖动平移，滚轮缩放，双击重置。

3D 点始终按物理倒易空间 `q (Å⁻¹)` 显示。存在 XDS 取向时，`Cell/grid`
使用 XDS 倒易基矩阵绘制真实的 `a*、b*、c*` 长度和夹角；单斜、三斜晶胞
不会被显示成正交单位立方体。`Indexed` 仅使用 hkl 判断点是否接近整数反射，
不会改变点的物理显示坐标。

`a*` 与 `b*` 视角固定 `c*` 向上，`c*` 视角固定 `b*` 向上。自由旋转以当前
屏幕的上/右方向为旋转轴，避免在斜晶轴视角下出现欧拉角翻转。3D panel 的
Brightness 和 Contrast 默认均为最大值。

导出的 `panel/index.html` 使用网络上的 Three.js 资源；离线时浏览器版可能无法完整
加载。GUI 内嵌 panel 不依赖该 CDN。

### 6.5 Slices

- `Family`：`hk0`、`h0l` 或 `0kl`；
- `Layer`：目标层，可为负数、0 或正数；
- `Thickness r.l.u.`：围绕平面的总厚度；
- `Step r.l.u.`：二维 bin 步长；
- **Preview**：重新计算/显示；
- **Export PNG**：保存当前显示；
- `Cell/grid` 和 Threshold 只控制显示。

hkl slices 必须有 XDS 取向参考。空层也会写出有效的空图和数据文件，不能把“文件
存在”误判为“该层有衍射点”。

### 6.6 AutoR3D 输出

```text
<out>/
├── reconstruction/volume.npz
├── observations/
│   ├── spot_observations.npz
│   ├── indexed_observations.npz   # 有取向时
│   ├── frame_geometry.json
│   ├── spots_with_intensity.txt
│   ├── exclusion_mask.png
│   └── exclusion_mask.npy
├── peaks/peak_candidates.json
├── exports/
│   ├── peaks.ply
│   ├── observations_sample.ply
│   ├── volume.vti
│   └── metadata.json
├── panel/index.html
├── qc_report.json
└── summary.md
```

实际文件名以结果 JSON 的 `exports.outputs` 为准。交付结果时至少保留
`summary.md`、`qc_report.json`、`frame_geometry.json`、索引观测和原始参数文件。

## 7. AutoSolve 与 SHELXL GUI 操作

### 7.1 选择反射文件

1. 点击 `Working directory` 的 **Browse**；
2. 可以选具体数据集，也可以选只含若干数据集的父目录再二次选择；
3. GUI 扫描 `<dataset>/diff/p/` 中扩展名严格为小写 `.hkl` 的文件；
4. 在 `HKL file` 下拉框选择单数据集或合并数据；
5. Cell/SG 会优先从同目录 `XDS_ASCII.HKL`，否则从同名 `.ahkl` 头读取。

Linux 文件名区分大小写；只有 `.HKL` 而没有 `.hkl` 时，GUI 会报告找不到文件。

### 7.2 Composition

GUI 的 `Composition` 是**单位晶胞内容**，例如：

```text
Au4 C20 H16 N2
```

它生成：

```text
SFAC Au C H N
UNIT 4 20 16 2
```

不要输入未知组成，也不要把分子式直接当单位晶胞内容。若只有分子式和 Z，使用 CLI
的 `--formula` 与 `--z`，程序会相乘。元素符号由
`AutoSolve/lib/SFAC_UCLA_2022.txt` 验证。

### 7.3 Solve (SHELXT)

1. 确认 `.hkl`、Cell 和 Composition；
2. 若要约束空间群，勾选 **Force space group** 并填写编号或支持的符号；
3. 点击 **Solve (SHELXT)**；
4. GUI 先生成同名 `.ins` 和同名 `.hkl`，再在 `diff/p/` 调用 SHELXT；
5. 检查 Output 和 Structure 标签。

生成 `.ins` 的固定规则：

- 电子波长 `0.02508`；
- `ZERR 1 0.001 0.001 0.001 0.010 0.010 0.010`；
- symbolic `SFAC` 和 `UNIT`；
- `HKLF 4`；
- 强制 SG 时，从空间群库加入相应 `LATT`/`SYMM` 并使用 SHELXT `-s`。

GUI 当前执行的是“prepare + 一次 SHELXT”，完成后寻找 `<basename>.res`，再依次寻找
`_a.res` 到 `_j.res`。需要结构化结果分类、已知 forced-SG priming/retry 路径时，使用
命令行 `python main.py solve solve ... --json`。

### 7.4 结构查看器与 Olex2

内置 Structure 标签读取 `.res/.ins`，显示原子、按共价半径推断的键和晶胞，并支持
旋转、平移、缩放、a/b/c 方向和 Grow symmetry mates。它是检查工具，不编辑模型，
也不替代 Olex2。

**Open Olex2** 打开的是当前 Structure 标签实际显示的 `.res`，不是输入框中任意文本。
默认 Olex2 路径固定为 `/mnt/c/Program Files/Olex2-1.5/olex2.exe`。

### 7.5 Refine (SHELXL)

1. 选择已存在的初始 `.res`；
2. 选择匹配的 `.hkl`；
3. 点击 **Refine (SHELXL)**。

程序不会在源目录直接覆盖模型，而是在以下新目录运行：

```text
log/AutoSolve/shelxl_refinement/<basename>_<timestamp>/
```

它从源 `.res` 生成 `.ins`，去掉旧 Q peaks，写入短周期 `L.S.` 和峰数设置，复制
`.hkl`，运行 用户自行安装的 SHELXL，并保留 stdout/stderr、`.res` 和 `.lst`。输出目录已存在
时会拒绝覆盖。

## 7b. AutoDials GUI 操作

AutoDials 是可选的 DIALS 后端，与 AutoXDS 并行且不修改源 TIFF 或 XDS 输出。

### 7b.1 运行时

- `DIALS Python` 指向 DIALS 解释器；未填写时按 `DIALS_PYTHON`、`C:\dials`、
  Linux/conda dials 顺序查找；
- 点击 **Check DIALS runtime** 会打印 `dials`/`dxtbx` 版本以及 `numpy`/`Pillow`/`scipy`
  是否可用；处理开始前也会自动探测一次。

### 7b.2 处理单个数据集

1. `Working directory` 指向数据集合根目录，点击 **Browse / Scan datasets**；
2. 选择一个数据集，填写可选的 Target cell、Space group、Resolution；
3. 点击 **Process & Convert selected**；结果显示在下方日志和运行列表。

每个 run 输出到 `<dataset>/AutoDials/run_<timestamp>_<id>/`：`summary.json`、
`reference.json`、`<name>.mtz/.hkl/.ins` 和 `work/` 中间文件。单数据集强度为
未标度的 profile intensity。

### 7b.3 运行列表与 HCA / 合并

- 选中数据集后，run 列表显示每个成功 run 的完整性、最终晶胞、空间群、DIALS 版本和时间；
- 勾选（点击每行前方框）要参与 HCA/合并的 run，而不是自动取最新；
- **HCA Run selected** 使用勾选的 run；**Merge & Convert selected** 直接合并，
  **Merge clusters at cutoff** 按 cutoff 聚类后逐组合并；
- 合并结果是 scaled 数据，可直接发送到 AutoSolve。

### 7b.4 交接

- **Send result to AutoR3D**：导出 DIALS 原生几何，切换到 AutoR3D 并导入；
- **Send result to AutoSolve**：发送 HKL，自动带出晶胞和空间群；未标度结果会先弹出确认。

## 8. AutoRefine 高级操作

AutoRefine 当前是独立 CLI，不是 `main.py` 的子命令。它在普通 SHELXL runner 之外
增加模型解析、ADP、指标和特定 SiO2 几何诊断。

### 8.1 定位模型

```bash
python AutoRefine/scripts/autorefine.py locate sample3_1_a --json
```

默认搜索项目 `Data/`。也可指定：

```bash
python AutoRefine/scripts/autorefine.py locate sample_a \
  --data-root /path/to/Data --json
```

重名且评分相同会报 `ambiguous_target`，不会猜测。

### 8.2 只检查，不运行 SHELXL

```bash
python AutoRefine/scripts/autorefine.py inspect \
  --res /path/to/model.res \
  --hkl /path/to/model.hkl \
  --profile generic \
  --json
```

SiO2 沸石模型：

```bash
python AutoRefine/scripts/autorefine.py inspect \
  --target sample_a \
  --profile zeolite-sio2 \
  --json
```

### 8.3 隔离精修并诊断

```bash
python AutoRefine/scripts/autorefine.py refine \
  --res /path/to/model.res \
  --hkl /path/to/model.hkl \
  --profile generic \
  --cycles 3 \
  --peaks 20 \
  --threads 4 \
  --json
```

先预演路径和命令：

```bash
python AutoRefine/scripts/autorefine.py refine \
  --res /path/to/model.res \
  --hkl /path/to/model.hkl \
  --dry-run --json
```

AutoRefine 默认输出到 `log/AutoRefine/<basename>_<timestamp>/`，并调用
`AutoSolve/scripts/auto_shelxl.py`。它不会覆盖源 `.res/.hkl`。

## 9. 命令行完整工作流

### 9.1 AutoXDS 列表

```bash
python main.py xds list --root Data
python main.py xds list --root Data --json
```

### 9.2 处理一个或多个数据集

```bash
python main.py xds process \
  --root Data \
  --dataset experiment_001 \
  --dataset experiment_002
```

声明 cell/SG 和分辨率：

```bash
python main.py xds process \
  --root Data \
  --dataset experiment_001 \
  --declared-cell '19.81 19.81 13.20 90 90 120' \
  --declared-sg 194 \
  --resolution '20 1.0' \
  --json
```

清除声明约束：

```bash
python main.py xds set-cell-sg \
  --root Data \
  --dataset experiment_001 \
  --clear-declared-cell-sg
```

安全预演：

```bash
python main.py xds process --root Data --dataset experiment_001 --dry-run --json
```

常用选项：

- `--all`：处理搜索根下全部标准数据集；
- `--replace-summary`：重新生成 summary，而不是追加；
- `--no-archive`：不归档旧输出，通常不推荐；
- `--skip-resolution`：不自动选截止并重跑；
- `--skip-xdsconv`：只跑 XDS；
- `--xds-bin` / `--xdsconv-bin`：显式程序路径；
- `--timeout`：单次命令超时，默认 1200 秒。

### 9.3 HCA

```bash
python main.py xds cluster \
  --root Data \
  --dataset experiment_001 \
  --dataset experiment_002 \
  --cluster-method both \
  --cluster-linkage average \
  --dendrogram AutoXDS/cache/manual_hca.svg \
  --json
```

`cc1` 默认直接读取各数据集 `XDS_ASCII.HKL` 的共同反射，不再强制至少 100 个共同
反射；`--min-common-reflections` 仅为兼容参数。需要限制大数据读取时使用
`--max-reflections`。

### 9.4 合并

先只准备输入：

```bash
python main.py xds merge \
  --root Data \
  --dataset experiment_001 \
  --dataset experiment_002 \
  --merge-name sample_merge \
  --json
```

实际运行 XSCALE 和 XDSCONV：

```bash
python main.py xds merge \
  --root Data \
  --dataset experiment_001 \
  --dataset experiment_002 \
  --merge-name sample_merge \
  --resolution 1.0 \
  --run --json
```

`--allow-sg-mismatch` 只解除程序层拒绝，不代表不同空间群的数据科学上适合合并；使用
前必须人工确认。

### 9.5 AutoR3D 检查和运行

只检查参数、帧范围、尺寸和 placeholder：

```bash
python main.py r3d inspect Data/experiment_001
```

完整运行：

```bash
python main.py r3d run Data/experiment_001 \
  --out log/AutoR3D/experiment_001
```

旧 `raw/` 数据：

```bash
python main.py r3d run Data/legacy_sample \
  --frames raw \
  --out log/AutoR3D/legacy_sample
```

常见调参：

```bash
python main.py r3d run Data/experiment_001 \
  --peak-threshold 40 \
  --peak-filter-size 12 \
  --center-mask-radius 15 \
  --mask-edge-guard-px 5 \
  --voxel-size 0.01 \
  --max-peaks 2000 \
  --frame-workers 8
```

外部坏点 mask 可用图片、NPY 或 NPZ：

```bash
python main.py r3d run Data/experiment_001 \
  --mask-file detector_mask.npy \
  --mask-threshold 0
```

`--preprocess pixels` 使用阈值像素而不是局部极大值；数据量显著增大，只在明确需要时
使用。`--max-frames` 适合快速试跑，但不能代替完整结果。

### 9.6 AutoSolve

单数据集：

```bash
python main.py solve solve \
  --root . \
  --dataset experiment_001 \
  --composition 'Si12 O24' \
  --json
```

显式 HKL：

```bash
python main.py solve solve \
  --hkl Data/experiment_001/diff/p/sample_merge.hkl \
  --composition 'Si12 O24' \
  --cell '19.81 19.81 13.20 90 90 120' \
  --sg 194 \
  --json
```

分子式乘 Z：

```bash
python main.py solve solve \
  --hkl /path/to/sample.hkl \
  --formula 'SiO2' \
  --z 12 \
  --sg P63/mmc \
  --json
```

只准备输入，不运行：

```bash
python main.py solve prepare \
  --hkl /path/to/sample.hkl \
  --composition 'Si12 O24' \
  --sg 194 \
  --dry-run --json
```

结果分类：

- `solved_success`：非空 `.res` 和 `.lxt/.lst`，且 HKLF 前存在原子模型；
- `shelxt_no_solution`：有结果文件但没有清晰原子模型；
- `shelxt_failed`：`.res` 或 log 缺失/为空；
- `input_preparation_failed`：HKL、cell、composition、模板、元素或 SG 映射有问题。

### 9.7 直接 SHELXL

```bash
python AutoSolve/scripts/auto_shelxl.py \
  --res /path/to/model.res \
  --hkl /path/to/model.hkl \
  --cycles 4 \
  --peaks 20 \
  --threads 4 \
  --json
```

`--outdir` 必须是尚不存在的新目录。`--open-olex2` 只在精修成功后打开结果。

## 10. AI Assistant

### 10.1 模式

- `offline`：只用确定性规则和本地证据；
- `local`：使用 Ollama；
- `cloud`：使用选定云 profile；
- `agent`：通过 OpenClaw 执行完整 agent turn；
- `auto`：优先检测本地模型，再尝试选定云 profile，最后离线。

普通 `autocrys` 读取 `assistant.enabled`；`autocrys-ai` 强制开启，
`autocrys-base` 强制关闭。

### 10.2 使用原则

- Assistant 的建议基于最近一次操作、UI 状态和实际 artifacts；
- 自然语言上下文不等于执行授权；可执行动作会先显示确认；
- HCA、AutoXDS、AutoR3D、SHELXT、SHELXL 应以实际输出文件为证据；
- AI 不可替代对空间群、组成、晶胞和模型质量的人工判断；
- `config/ai_assistant.json` 是机器本地文件，不应复制给其他机器或提交。

### 10.3 Agent 模式

```json
{
  "llm": {
    "mode": "agent",
    "agent": {
      "command": "openclaw",
      "agent_id": "main",
      "session_key": "autocrys",
      "thinking": "",
      "timeout_seconds": 600
    }
  }
}
```

如果 OpenClaw 由 NVM 安装且 GUI 找不到命令，优先运行更新自检的 `--apply`，不要把
源机器的绝对路径硬编码到所有目标机。

## 11. 数据安全与可重复性

### 11.1 哪些原文件不应删除

```text
原始 TIFF
cRED2 参数文件
原始 XDS.INP
CORRECT.LP
XDS_ASCII.HKL
temp.hkl
合并 .hkl/.ahkl
初始和精修后的 .res/.lst
```

### 11.2 AutoXDS 的备份行为

- 改写 `XDS.INP` 前生成带用途后缀的备份；
- 重跑时把旧输出移入 `autoXDS_old_outputs_<timestamp>/`；
- 目标备份名已存在时自动追加序号；
- `--no-archive` 会降低可恢复性，只用于确认不需旧输出的场景。

### 11.3 精修隔离

AutoSolve SHELXL 和 AutoRefine 默认创建新输出目录，不覆盖源结构。不要用已有目录作为
`--outdir`。人工把结果复制回数据目录前，先比较 cell、SG、HKL 和 basename。

### 11.4 建议记录

每个交付结果至少记录：

- AutoCrys 版本/代码提交；
- WSL、Python、XDS、SHELXT、SHELXL 版本；
- 若使用 AutoDials：DIALS Python 路径、`dials`/`dxtbx` 版本、所选 run ID；
- 输入数据集和 XDS.INP 备份；
- Cell/SG/Resolution 是否人工声明；
- HCA 方法和 cutoff；
- 合并数据集顺序；
- AutoR3D 参数和 `qc_report.json`；
- SHELXT 命令、composition 和结果分类；
- SHELXL 输出目录和主要指标。

## 12. 验收与回归测试

基础检查：

```bash
python3 scripts/update_self_check.py
conda activate AutoCrys
python -m pip check
python -m compileall -q main.py config UI AutoDials AutoXDS AutoR3D AutoSolve AutoRefine
```

测试：

```bash
python -m unittest discover -s UI/tests -p 'test*.py' -v
python -m unittest discover -s AutoDials/tests -p 'test*.py' -v
python -m unittest discover -s AutoR3D/tests -p 'test*.py' -v
python -m unittest discover -s AutoXDS/tests -p 'test*.py' -v
python -m unittest discover -s AutoSolve/tests -p 'test*.py' -v
python -m unittest discover -s AutoRefine/tests -p 'test*.py' -v
```

按上面顺序串行运行，不要并行。套件共享真实 `Data/` 和部分输出目录，并行测试会造成
文件状态竞争，使结果在 failure、skip 和 error 之间变化。

部分测试直接使用 `Data/` 中的真实可变数据。若空间群、晶胞或模型原子数已因重新处理
改变，固定断言会失败。遇到这类失败应先比较测试期望与当前 artifact，而不是直接删除
当前数据或把失败改成通过。用于发布的稳定 CI 应复制只读 fixture。

## 13. 故障排查

### 13.1 GUI 启不来

```bash
python3 scripts/update_self_check.py
python -c 'import tkinter; print(tkinter.TkVersion)'
printf 'DISPLAY=%s WAYLAND=%s\n' "$DISPLAY" "$WAYLAND_DISPLAY"
```

若显示变量为空，关闭 GUI，在 Windows PowerShell 执行 `wsl --shutdown` 后重开。

### 13.2 GUI 能启动但找不到命令

GUI 继承启动它的 shell 的 `PATH`。在**同一个终端**检查：

```bash
command -v xds xscale xdsconv shelxt openclaw
```

然后从该终端运行 `autocrys`。修改 `~/.bashrc` 后要重开终端或 `source ~/.bashrc`。

### 13.3 AutoXDS 找不到数据集

```bash
find Data -path '*/diff/p/XDS.INP' -print
python main.py xds list --root Data --json
```

没有 `diff/p/XDS.INP` 时，先准备正确的 XDS 输入；不要只创建空文件骗过发现逻辑。

### 13.4 XDS 运行后没有 `XDS_ASCII.HKL`

检查：

```bash
sed -n '1,220p' Data/<dataset>/diff/p/XDS.LP
tail -n 120 Data/<dataset>/diff/p/CORRECT.LP
```

常见原因是图像模板/范围、探测器几何、束心、距离、旋转轴、cell/SG 约束或分辨率
错误。不要在没有证据时反复降低分辨率或强制空间群。

### 13.5 HCA/合并失败

- 确认每个数据集 summary 有有效 Cell、SG 和 Rfactor；
- `cc1` 确认 `XDS_ASCII.HKL` 非空且有共同反射；
- 空间群不一致默认不能合并；
- `merge_xscale_premerged` 表示 XSCALE 输出已预合并，程序为保护 Rint 拒绝继续；
- 检查 `XSCALE.LP` 和结果摘要，不要只看 `.ahkl` 是否存在。

### 13.6 AutoR3D 导入失败

```bash
python main.py r3d inspect Data/<dataset>
```

重点检查参数文件是否在根目录、帧目录是否为 `diff`、文件名是否匹配
`frame_*.tif`、TIFF 是否可读。旧 `raw/` 布局加 `--frames raw`。

### 13.7 AutoR3D 没有观测或点太多

- 没有观测：适度降低 `--peak-threshold`，检查中心/外部 mask；
- 点太多：提高 threshold、增大局部极大值半径或降低 `--max-spots-per-frame`；
- 边缘伪峰：增加 `--mask-edge-guard-px` 或提供 detector mask；
- 先用 `--max-frames` 快速试跑，再全量运行。

### 13.8 AutoSolve 报缺 composition/cell/SG

- Composition 必须显式给出；程序不会猜；
- Cell 优先级：用户参数、summary、`CORRECT.LP`、`XDS.INP`；
- SG 优先级相同，并通过 `shelxt_space_groups.json` 映射；
- 若确实不强制 SG，使用 `--no-force-sg`；不要把缺失 SG 静默当作 `P-1`。

### 13.9 Olex2 打不开

```bash
test -f "/mnt/c/Program Files/Olex2-1.5/olex2.exe"
```

若 Windows `.exe` 互通整体失败，先记录 `wsl --version` 并重启 WSL；不要把复制
`.res` 到任意 Windows 目录当作自动工作流修复。

### 13.10 OpenClaw 在终端可用、GUI 中不可用

这通常是 NVM PATH 没有被 GUI 进程继承。运行：

```bash
python3 scripts/update_self_check.py
python3 scripts/update_self_check.py --apply
```

自检会使用目标机已有的 NVM/OpenClaw，不重新安装；agent 模式下会生成 wrapper 并
更新本机配置。若 Gateway 未运行，再由用户根据现有部署方式执行
`openclaw gateway start` 或相应服务命令。

## 14. 每次正式处理前后的清单

开始前：

1. 在目标机运行更新自检；
2. 激活正确 Conda 环境；
3. 备份原始数据和 `XDS.INP`；
4. 确认数据集名称唯一；
5. 确认 Cell/SG 是声明值、已知值还是待索引值；
6. 确认 composition 指单位晶胞内容；
7. 确认输出目录和磁盘空间；
8. 先用一份复制的数据做烟雾测试。

完成后：

1. 检查 summary、LP、QC JSON，而不只看按钮显示成功；
2. 确认合并输出保持 `MERGE=FALSE`；
3. 确认 AutoR3D 的 axis source、placeholder、观测数、峰数和 hkl residual；
4. 确认 SHELXT 结果中 HKLF 前确有原子模型；
5. 确认 SHELXL 使用匹配 HKL，并在隔离目录运行；
6. 保存参数、版本、命令和异常说明；
7. 不把本机 AI 配置或密钥放入交付包。
