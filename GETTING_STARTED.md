# AutoCrys v1.0 安装与首次启动

本文说明如何在 Windows + WSL 2 + Ubuntu 上安装 AutoCrys，并完成第一次可验证的启动。日常处理、GUI 各按钮、完整 CLI、输出解释和故障排查见 [USER_MANUAL.md](USER_MANUAL.md)。

本文已按 2026-09-22 的实际代码重新核对，命令来源包括 `pyproject.toml`、`main.py` 和各模块的 `argparse` 定义。请以当前代码的 `--help` 输出为最终依据。

## 1. 运行边界

AutoCrys 的 Python、XDS、XSCALE、XDSCONV、SHELXT 和 SHELXL 必须在 WSL/Linux 内运行。Olex2 可以是 Windows 程序，由 WSL 调用。

不要用 Windows Python 直接运行 `\\wsl.localhost\...\AutoCrys` 下的代码。Windows Python 无法直接执行用户自行安装的 Linux SHELXL，典型错误是 `WinError 193` 或 “not a valid Win32 application”。

推荐环境：

- Windows 10 2004（Build 19041）或更新版本，或 Windows 11；
- WSL 2 + Ubuntu，x86-64；
- Python 3.12；
- 至少 8 GB 内存；实际所需磁盘空间由 TIFF 数据量决定；
- 安装期间可联网；AI 和导出的浏览器 3D panel 另有联网要求。

部署时检查当前 WSL、Python 和 Tk 版本。升级 WSL 前查看官方发布说明并备份重要数据。

参考资料：

- [Microsoft：安装 WSL](https://learn.microsoft.com/windows/wsl/install)
- [Microsoft：配置 WSL 开发环境](https://learn.microsoft.com/windows/wsl/setup/environment)
- [Conda：从环境文件创建环境](https://docs.conda.io/projects/conda/en/stable/commands/env/create.html)
- [XDS：下载与安装](https://xds.mr.mpg.de/html_doc/downloading.html)

## 2. 已部署系统：先自检，再原位更新

如果目标机已经部署过 AutoCrys、Conda、OpenClaw 或其他工具，**不要从第 3 节开始
逐项重装**。先在现有项目根目录运行只读自检；它只依赖系统 Python 3：

```bash
cd /path/to/AutoCrys
python3 scripts/update_self_check.py
```

已经完成 editable install 的环境也可以使用 `autocrys-self-check`；直接运行脚本更适合
入口可能已经过期的旧部署。

自检会按目标机实际状态识别：

- WSL/Linux、CPU 架构和 WSLg 显示变量；
- 当前登录 shell、`bash`，以及存在时的 `bash2`；
- `PATH` 中的 Conda，或 `~/miniconda3`、`~/miniforge3`、`~/mambaforge`、
  `~/anaconda3` 中已有的 Conda；
- 已有 `AutoCrys` Conda 环境、Python 版本、依赖、editable install 和启动器；
- XDS、XSCALE、XDSCONV、SHELXT、用户自行安装的 SHELXL；
- 可用的 DIALS Python（`DIALS_PYTHON`、`C:\dials`、Linux/conda dials 环境）及其
  `dials`/`dxtbx` 导入；
- 当前 AI 模式、配置的 OpenClaw 命令、NVM 中已有的 OpenClaw，以及 Gateway
  是否可达；
- 目标机已有的 XDS 数据集标记和 cRED2 参数文件数量。

输出只显示路径、版本和状态，不会打印 API key。机器读取自检结果时可用：

```bash
python3 scripts/update_self_check.py --json
```

确认结果后执行最小原位适配：

```bash
python3 scripts/update_self_check.py --apply
```

`--apply` 的边界是：

- 只更新**已经存在**的 AutoCrys Conda 环境，不删除、不重建环境，也不使用
  `--prune` 清掉额外包；
- 重新执行 `pip install -e`，使入口指向当前工作副本；
- 必要时修复 本机安装的 SHELXL 可执行位；
- SHELXT 未安装时提示用户自行从官网下载；不安装或下载第三方程序；
- 根据实际 shell 选择 `~/.bashrc`（包括 `bash2`）或其他 profile，只补充
  `~/.local/bin`；
- 当配置确实使用 `llm.mode = "agent"`、`openclaw` 又不在当前 `PATH` 时，识别
  NVM 中可运行的 OpenClaw，生成 PATH-safe wrapper，并只更新现有配置中的
  `llm.agent.command`；
- 不安装或升级 WSL，不创建/删除 Conda 环境，不自动安装 XDS，不启动/停止
  OpenClaw Gateway，也不改动实验数据。

执行后脚本会自动再跑一次自检。仍为 `WARN` 的可选组件可以保留；`FAIL` 或当前
工作所需组件的 `WARN` 才需要人工处理。只有全新机器或自检明确显示基础组件不存在时，
才继续下面的首次安装章节。

## 3. 安装 WSL 2 和 Ubuntu

只在尚未安装 WSL/Ubuntu 时执行本节。在“以管理员身份运行”的 PowerShell 中：

```powershell
wsl --install -d Ubuntu
```

按提示重启 Windows，第一次打开 Ubuntu 时创建 Linux 用户名和密码。随后在 PowerShell 检查：

```powershell
wsl --version
wsl -l -v
```

Ubuntu 的 `VERSION` 必须为 `2`。如果是 1：

```powershell
wsl --set-version Ubuntu 2
```

从下一节开始，除非代码块明确标为 PowerShell，所有命令都在 Ubuntu 终端运行。

## 4. 安装 Linux 基础依赖

```bash
sudo apt update
sudo apt install -y \
  wget ca-certificates bzip2 file \
  fontconfig x11-xserver-utils xfonts-utils
```

检查 WSLg 环境：

```bash
printf 'DISPLAY=%s\n' "$DISPLAY"
printf 'WAYLAND_DISPLAY=%s\n' "$WAYLAND_DISPLAY"
uname -m
```

架构应为 `x86_64`，`DISPLAY` 或 `WAYLAND_DISPLAY` 通常至少有一个非空。若二者都为空，先关闭 AutoCrys，再在 PowerShell 执行 `wsl --shutdown`，然后重新打开 Ubuntu。这个命令会结束所有 WSL 进程。

## 5. 在 WSL 中安装 Miniconda

必须安装 Linux x86-64 版本，不要复用 Windows Conda：

```bash
wget -O /tmp/miniconda.sh \
  https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash /tmp/miniconda.sh -b -p "$HOME/miniconda3"
eval "$("$HOME/miniconda3/bin/conda" shell.bash hook)"
conda init bash
```

关闭并重新打开 Ubuntu，然后检查：

```bash
conda --version
```

如果新终端仍找不到 `conda`，临时执行：

```bash
eval "$("$HOME/miniconda3/bin/conda" shell.bash hook)"
```

## 6. 把项目放在 Linux 文件系统

推荐路径是 `/home/<linux-user>/AutoCrys`。处理大量 TIFF 时不要把工作副本放在 `/mnt/c/...`；WSL 文件系统通常有更好的 Linux I/O 性能。

### 6.1 使用 Git

```bash
cd "$HOME"
git clone <repository-url> AutoCrys
cd "$HOME/AutoCrys"
```

### 6.2 使用压缩包

在源 WSL 机器、包含 `AutoCrys/` 的目录中打包。机器本地密钥、数据和运行输出不应进入安装包：

```bash
tar \
  --exclude='AutoCrys/config/ai_assistant.json' \
  --exclude='AutoCrys/Data' \
  --exclude='AutoCrys/Demo' \
  --exclude='AutoCrys/.git' \
  --exclude='AutoCrys/AutoSolve/tools/*' \
  --exclude='AutoCrys/log' \
  --exclude='AutoCrys/build' \
  --exclude='AutoCrys/dist' \
  --exclude='AutoCrys/*.whl' \
  --exclude='AutoCrys/*.egg-info' \
  --exclude='AutoCrys/__pycache__' \
  --exclude='*:Zone.Identifier' \
  -czf AutoCrys-v1.0-source.tar.gz AutoCrys
```

把压缩包复制到目标 WSL 用户家目录后：

```bash
cd "$HOME"
tar -xzf AutoCrys-v1.0-source.tar.gz
cd "$HOME/AutoCrys"
```

使用 `tar` 可以保留 Linux 文件权限。实验数据应单独复制到 `Data/`。

## 7. 创建 Conda 环境并安装 AutoCrys

项目的 `config/environment-autocrys.yml` 创建名为 `AutoCrys` 的 Python 3.12 环境：

```bash
cd "$HOME/AutoCrys"
conda env create -f config/environment-autocrys.yml
conda activate AutoCrys
python -m pip install -e .
```

`environment-autocrys.yml` **不再包含** `pip: -e .`，因此最后一条可编辑安装命令不可省略。它会注册这些入口：

```text
autocrys
autocrys-ai
autocrys-base
autocrys-main
autor3d
autor3d-ui
```

不要复用旧的 Python 3.10 AutoCrys 环境；部署记录中该环境在真实 AutoRefine 压力测试里出现过间歇性崩溃。

更新已有安装时：

```bash
cd "$HOME/AutoCrys"
conda activate AutoCrys
conda env update -f config/environment-autocrys.yml --prune
python -m pip install -e .
```

不要在未备份的情况下删除旧环境或覆盖 `Data/`、`log/`、`config/ai_assistant.json`。

## 8. 配置晶体学外部程序

### 8.1 SHELXL（用户自行下载）

Git 仓库、源码包和 wheel 均不附带 SHELXT/SHELXL 或其他第三方程序。
请用户自行访问 [SHELX 官网](https://shelx.uni-goettingen.de/)，完成所需注册、
确认适用条款后下载与目标系统和架构匹配的 Linux 程序。AutoCrys 不代下载或重新分发。

源码安装的 AutoSolve/AutoRefine 和集成 UI 默认使用 `AutoSolve/tools/shelxl`。
把**自行下载并解压后的 SHELXL** 放在该位置；这是被 Git 忽略的本机文件：

```bash
cd "$HOME/AutoCrys"
mkdir -p AutoSolve/tools
# /path/to/downloaded/shelxl 替换为你自行下载并解压的文件，目标不存在时才安装。
test ! -e AutoSolve/tools/shelxl
install -m 755 /path/to/downloaded/shelxl AutoSolve/tools/shelxl
file AutoSolve/tools/shelxl
ldd AutoSolve/tools/shelxl
```

`file` 应显示与本机架构匹配的 Linux ELF；`ldd` 如显示 `not found`，先补齐依赖。
静态链接程序可能显示 `not a dynamic executable`，不按缺库处理。
从 wheel 安装时，CLI 可显式传入 `--shelxl-bin /path/to/shelxl`；
集成 UI 建议按本文使用源码安装及上述本机路径。

### 8.2 SHELXT（用户自行下载）

从同一 [SHELX 官网](https://shelx.uni-goettingen.de/)自行获得对应 Linux SHELXT，
按官方说明解压，然后放入 Linux PATH。仓库里没有 `shelxt.bz2`。

```bash
mkdir -p "$HOME/.local/bin"
# /path/to/downloaded/shelxt 替换为自行下载并解压的文件。
test ! -e "$HOME/.local/bin/shelxt"
install -m 755 /path/to/downloaded/shelxt "$HOME/.local/bin/shelxt"
export PATH="$HOME/.local/bin:$PATH"
command -v shelxt
file "$(command -v shelxt)"
```

需要持久化 PATH 时自行把 `export PATH="$HOME/.local/bin:$PATH"` 加到 shell 配置。
更新自检不会下载或安装 SHELX；缺失时按本节自行安装。其他 SHELX 工具也不随仓库分发。

### 8.3 XDS、XSCALE、XDSCONV

Conda 环境不会安装 XDS。请从 [XDS 官方下载页](https://xds.mr.mpg.de/html_doc/downloading.html)获取适用于 Linux x86-64 的程序并接受相应许可。AutoCrys 默认调用以下三个精确命令名：

```text
xds
xscale
xdsconv
```

可以把 XDS 解压目录直接加入 `PATH`：

```bash
printf '\nexport PATH="$HOME/XDS-INTEL64_Linux_x86_64:$PATH"\n' >> "$HOME/.bashrc"
export PATH="$HOME/XDS-INTEL64_Linux_x86_64:$PATH"
```

目录名按实际解压结果修改。官方 Linux 包同时提供 `xds`/`xds_par` 和 `xscale`/`xscale_par`；AutoCrys 的默认值是单处理器名称，可在 CLI 中用 `--xds-bin`、`--xscale-bin` 显式替换。

检查：

```bash
command -v xds xscale xdsconv
file "$(command -v xds)" "$(command -v xscale)" "$(command -v xdsconv)"
```

### 8.4 Olex2（可选）

默认 Windows 路径是 `/mnt/c/Program Files/Olex2-1.5/olex2.exe`。在 WSL 中检查：

```bash
test -f "/mnt/c/Program Files/Olex2-1.5/olex2.exe" && echo "Olex2 found"
```

Olex2 不存在或安装在其他位置时，不要勾选 **Open Olex2**；CLI 可通过 `--olex2-bin` 指定路径。

## 9. 配置 AI 助手（可选）

科学流程不依赖 AI。首次配置时只复制无密钥模板：

```bash
cd "$HOME/AutoCrys"
cp -n config/ai_assistant.example.json config/ai_assistant.json
```

然后编辑 `config/ai_assistant.json`：

- `assistant.enabled`：普通 `autocrys` 是否显示 Assistant 标签；
- `llm.mode`：`auto`、`cloud`、`local`、`agent` 或 `offline`；
- `llm.cloud.base_url` 和 `llm.cloud.model`：填写用户自己的兼容 API 服务与模型名；
- `llm.local.host` / `model`：Ollama 地址和模型；
- `llm.agent`：OpenClaw 命令、agent、会话和超时；
- `structure_viewer`：内置结构查看器设置。

密钥优先从所选 profile 的 `api_key` 读取；为空时再读取 `api_key_env` 指定的环境变量。更推荐环境变量，例如：

```bash
export DEEPSEEK_API_KEY='<your-key>'
```

不要提交、打包、打印或在报告中粘贴 `config/ai_assistant.json`。该文件已在 `.gitignore`、`MANIFEST.in` 和包配置中排除。

## 10. 安装验证

### 10.1 Python、依赖和入口

```bash
conda activate AutoCrys
cd "$HOME/AutoCrys"
python -c 'import sys; print(sys.version); print(sys.platform); print(sys.executable)'
python -m pip check
python main.py --help
python main.py xds --help
python main.py solve --help
python main.py r3d --help
python AutoRefine/scripts/autorefine.py --help
```

必须看到 `linux`，解释器应位于 WSL 的 AutoCrys Conda 环境中。

### 10.2 外部程序

```bash
command -v xds xscale xdsconv shelxt
test -x AutoSolve/tools/shelxl
ldd AutoSolve/tools/shelxl | grep 'not found' && echo 'missing library' || true
```

DIALS 是可选的独立运行时，不在 WSL 内由 `environment-autocrys.yml` 安装；AutoDials
通过它自己的 Python 以子进程方式调用。指定方式：

```bash
export DIALS_PYTHON=/path/to/dials/python        # 任意平台
export DIALS_PYTHON=/mnt/c/dials/python.exe      # WSL 调用 Windows DIALS
python3 - <<'PY'
from AutoDials.ui_support import probe_dials
print(probe_dials(__import__("os").environ["DIALS_PYTHON"]))
PY
```

未设置时，AutoDials 会依次查找 `C:\dials\python.exe`、`C:\DIALS\python.exe`，
以及 `~/miniconda3|miniforge3|mambaforge|anaconda3/envs/dials/bin/python` 和
`/mnt/c/dials/python.exe`。

### 10.3 Tk/WSLg

```bash
python - <<'PY'
import tkinter as tk

root = tk.Tk()
print("Tk OK:", root.tk.call("info", "patchlevel"))
root.destroy()
PY
```

### 10.4 回归测试

```bash
python -m unittest discover -s UI/tests -p 'test*.py' -v
python -m unittest discover -s AutoDials/tests -p 'test*.py' -v
python -m unittest discover -s AutoR3D/tests -p 'test*.py' -v
python -m unittest discover -s AutoXDS/tests -p 'test*.py' -v
python -m unittest discover -s AutoSolve/tests -p 'test*.py' -v
python -m unittest discover -s AutoRefine/tests -p 'test*.py' -v
```

这些命令必须串行执行。测试共享 `Data/` 和部分运行输出；并行执行会让一个套件在另一个
套件检查文件的同时看到不同状态，产生不稳定的 skip/error。

Git 版不提供 Data/Demo。离线单元测试使用合成临时夹具；依赖真实数据和外部程序的测试在缺失前置条件时明确跳过。通过离线测试不代表通过真实实验数据或第三方处理链验收。当前 Git 版验收结果见 RELEASE_AUDIT.md。

## 11. 准备第一个数据集

AutoXDS 和 AutoR3D 对同一数据集的推荐布局是：

```text
Data/
└── experiment_001/
    ├── Continuous 3D ED (cRED2) parameters.txt
    └── diff/
        ├── frame_0001.tif
        ├── frame_0002.tif
        ├── ...
        └── p/
            └── XDS.INP
```

重要规则：

- AutoXDS **不会创建初始 `XDS.INP`**；它只发现并修改 `<dataset>/diff/p/XDS.INP`；
- AutoXDS 默认把 `NAME_TEMPLATE_OF_DATA_FRAMES` 设为 `../frame_????.tif tiff`，所以默认 TIFF 位于 `diff/`；
- AutoR3D 参数文件必须位于数据集根目录，名称匹配 `*cRED2*parameters*.txt` 或 `*3D ED*parameters*.txt`；
- AutoR3D 默认读取 `diff/frame_*.tif`；旧数据在 `raw/` 时要用 `--frames raw` 或在 GUI 中选择相应目录；
- XDS 运行后，`diff/p/` 中会出现 `CORRECT.LP`、`XDS_ASCII.HKL` 等文件；AutoR3D 会优先用这些文件获得旋转轴、晶胞、取向、束心和波长。

## 12. 首次启动

```bash
conda activate AutoCrys
cd "$HOME/AutoCrys"
autocrys
```

其他入口：

```bash
autocrys-ai       # 强制显示 AI Assistant
autocrys-base     # 强制关闭 AI Assistant
autocrys-main     # 完整、不过滤的 Console
autor3d-ui        # 同一个集成 UI
python main.py    # 等价于普通 autocrys
```

首次验收建议依次确认：

1. UI 窗口正常显示，不透明、中文字体正常；
2. AutoXDS 能列出测试数据集；
3. AutoXDS 能调用 `xds` 和 `xdsconv`；
4. 可选：AutoDials 的 **Check DIALS runtime** 成功，并能处理单个数据集生成 MTZ/HKL/INS；
5. AutoR3D 能导入参数文件和 TIFF，并生成 QC、3D panel 与 slices；
6. AutoSolve 能调用 `shelxt` 生成 `.res`；
7. AutoSolve 的 **Refine (SHELXL)** 在新的隔离目录完成短周期精修；
8. 可选：成功结果能用 Olex2 打开；
9. 关闭网络和 AI 后，科学流程仍能运行。

完成安装后请继续阅读 [USER_MANUAL.md](USER_MANUAL.md)。

## 13. 安装阶段常见错误

### `conda: command not found`

```bash
eval "$("$HOME/miniconda3/bin/conda" shell.bash hook)"
conda activate AutoCrys
```

### `autocrys: command not found`

通常是没有激活环境或漏掉可编辑安装：

```bash
conda activate AutoCrys
cd "$HOME/AutoCrys"
python -m pip install -e .
```

### `xds` / `xscale` / `xdsconv` / `shelxt`: command not found

```bash
echo "$PATH"
command -v xds xscale xdsconv shelxt
```

把 Linux 可执行文件所在目录加入 `~/.bashrc`，重新打开终端后再检查。

### SHELXL `Permission denied`

```bash
chmod 755 "$HOME/AutoCrys/AutoSolve/tools/shelxl"
```

### `cannot open display` 或只有任务栏图标

先记录 `wsl --version`，关闭 AutoCrys，在 PowerShell 执行：

```powershell
wsl --shutdown
```

重新打开 Ubuntu并重复 Tk 检查。不要在没有核对发布说明和备份的情况下，用 `wsl --update` 作为显示问题的第一反应。

### `WinError 193`

程序由 Windows Python 启动。关闭它，回到 Ubuntu，激活 Conda 环境并从项目根目录重新启动。
