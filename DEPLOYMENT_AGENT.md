# AutoCrys 0.1 Agent 部署执行手册

本文件供 AI/coding Agent 在另一台 Windows 电脑上部署 AutoCrys。面向人的背景说明见
[`GETTING_STARTED.md`](GETTING_STARTED.md)。Agent 必须按 Gate A → E 顺序执行；任一 Gate
失败时停止，不得把“命令执行过”当成“部署成功”。

## 0. 目标、边界和默认值

目标环境：Windows + WSL 2 + Ubuntu，Linux x86-64。AutoCrys、Conda、Python、XDS、
SHELXT 和 SHELXL 均在 WSL/Linux 内运行。不得使用 Windows Python 直接运行
`\\wsl.localhost\...\AutoCrys` 中的代码。

默认值：

```text
WSL distribution: Ubuntu
project root:      /home/<linux-user>/AutoCrys
Conda environment: AutoCrys
Python:            3.12
```

Agent 开始前必须获得或确认：

- 项目来源：压缩包路径或已经复制好的目录。
- 目标 WSL 发行版名称和 Linux 用户。
- 目标目录；未指定时使用 `$HOME/AutoCrys`。
- XDS 的合法安装来源或现有安装路径。
- 是否启用可选 AI；密钥只写入目标机忽略跟踪的
  `config/ai_assistant.json`，不要让用户把密钥发进聊天、日志或 Markdown。
- 是否需要可选 Olex2 联动，以及 Windows 端真实安装路径。

未经用户确认，Agent 不得：

- 安装/重装 WSL、请求重启、执行 `sudo`，或修改系统级配置。
- 覆盖既有 `AutoCrys`、`Data/`、`log/` 或 `config/ai_assistant.json`。
- 删除或重建既有 Conda 环境。
- 下载、转发或重新分发有许可约束的 XDS/SHELX 程序。
- 把 API key 写入仓库、报告、shell 历史或除目标机
  `config/ai_assistant.json` 之外的文件。

所有命令必须显示真实输出。禁止用 `|| true`、忽略退出码或虚构通过结果。

## 1. Gate A：平台预检

先在 Windows PowerShell 只读检查：

```powershell
wsl --status
wsl --version
wsl -l -v
```

验收：目标 Ubuntu 存在且 `VERSION` 为 `2`。若不存在，报告 `BLOCKED: WSL2/Ubuntu
missing`，说明需要管理员 PowerShell 执行以下命令并可能重启；得到用户确认后才执行：

```powershell
wsl --install -d Ubuntu
```

不得把 `wsl --update` 作为部署的默认步骤。先记录当前版本，升级前查看官方发布说明并备份重要数据。`wsl --shutdown` 会终止所有 WSL 进程。

进入目标 Ubuntu 后执行：

```bash
set -eu
uname -m
cat /etc/os-release
printf 'HOME=%s\n' "$HOME"
printf 'DISPLAY=%s\n' "${DISPLAY:-}"
printf 'WAYLAND_DISPLAY=%s\n' "${WAYLAND_DISPLAY:-}"
```

验收：`uname -m` 为 `x86_64`；`HOME` 是 Linux 路径；系统为 Ubuntu。其他架构不可使用
用户自行安装的 Linux SHELX 二进制。没有 DISPLAY/WAYLAND 时先记录 GUI 阻塞，不影响继续
准备命令行环境。

## 2. 安装系统前置依赖

执行此节需要用户允许 `sudo`：

```bash
sudo apt update
sudo apt install -y wget ca-certificates bzip2 file fontconfig \
  x11-xserver-utils xfonts-utils
```

不得安装 Windows 版 Python 或 Windows 版 Conda 来替代本节后的 Linux 环境。

## 3. 安置项目，保护已有数据

工作副本应位于 WSL 文件系统（例如 `$HOME/AutoCrys`），不应位于 `/mnt/c`。先解析并检查
目标，禁止对 `$HOME`、`/` 或未解析变量做递归删除/覆盖：

```bash
target="$HOME/AutoCrys"
printf 'TARGET=%s\n' "$target"
test "$target" != "$HOME"
test "$target" != "/"
```

若目标已存在：

```bash
test -e "$target" && printf 'EXISTING_TARGET=%s\n' "$target"
```

此时停止并让用户决定“更新现有安装”还是“使用新目录”。不得自动覆盖。更新时必须保留
`Data/`、`log/` 和本机 `config/ai_assistant.json`。

若来源为 `AutoCrys-0.1.0-linux-x86_64.tar.gz`，先检查内容，再解压：

```bash
cd "$HOME"
tar -tzf AutoCrys-0.1.0-linux-x86_64.tar.gz | sed -n '1,40p'
test ! -e "$HOME/AutoCrys"
tar -xzf AutoCrys-0.1.0-linux-x86_64.tar.gz
cd "$HOME/AutoCrys"
```

确认项目根：

```bash
test -f pyproject.toml
test -f main.py
test -f config/environment-autocrys.yml
test -f config/ai_assistant.example.json
```

安全检查：真实本机配置不应来自源机器；本机 JSON 可以包含 key，但只打印布尔值，不打印密钥：

```bash
python3 - <<'PY'
import json
from pathlib import Path

p = Path("config/ai_assistant.json")
data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
cloud = data.get("llm", {}).get("cloud", {})
profiles = cloud.get("profiles", {})
keys = [cloud.get("api_key", "")]
keys.extend(v.get("api_key", "") for v in profiles.values() if isinstance(v, dict))
print("machine-local config exists:", p.exists())
print("inline API key configured:", any(keys))
PY
```

`inline API key configured: True` 是允许状态。确认文件由 `.gitignore` 排除且未进入传输包；不得显示该值。

## 4. Gate B：Linux Miniconda 和 AutoCrys 环境

优先复用目标用户现有的 Linux Conda。检查：

```bash
command -v conda || true
test -x "$HOME/miniconda3/bin/conda" && "$HOME/miniconda3/bin/conda" --version
```

若没有 Conda，在获得下载许可后安装 Linux x86-64 Miniconda：

```bash
wget -O /tmp/miniconda-autocrys.sh \
  https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash /tmp/miniconda-autocrys.sh -b -p "$HOME/miniconda3"
eval "$("$HOME/miniconda3/bin/conda" shell.bash hook)"
conda init bash
```

不要依赖交互式 shell 是否已重新打开；当前执行过程显式加载 hook：

```bash
eval "$("$HOME/miniconda3/bin/conda" shell.bash hook)"
cd "$HOME/AutoCrys"
```

若环境不存在则创建：

```bash
conda env list
conda env create -f config/environment-autocrys.yml
```

若环境已经存在，先检查 Python：

```bash
conda run -n AutoCrys python --version
```

不是 Python 3.12 时不要静默原地升级或删除环境；停止并请用户选择备份后重建，或创建一个新的
环境。Python 3.10 在本项目真实 AutoRefine 压力测试中出现过间歇性 GC 段错误。若现有环境已经
是 3.12，可更新它：

```bash
conda env update -n AutoCrys -f config/environment-autocrys.yml --prune
```

随后统一执行：

```bash
conda activate AutoCrys
python -m pip install -e .
python -m pip check
python - <<'PY'
import sys
print("platform:", sys.platform)
print("executable:", sys.executable)
print("version:", sys.version)
PY
```

验收：`platform: linux`；解释器属于 WSL 内的 AutoCrys Conda 环境；Python 为 3.12；
`pip check` 无依赖冲突。否则 Gate B 失败。

## 5. Gate C：晶体学外部程序

### SHELXL / SHELXT（用户自行获取）

仓库及 Python 分发包不包含任何 SHELX/XDS 二进制或压缩包。用户自行从
[SHELX 官网](https://shelx.uni-goettingen.de/)注册并下载匹配系统与架构的程序，
按 [GETTING_STARTED.md 第 8 节](GETTING_STARTED.md#8-配置晶体学外部程序)安装。
Agent 只使用用户明确提供的合法安装路径，不自动下载或重新分发。
源码安装的 SHELXL 默认路径为 `AutoSolve/tools/shelxl`，SHELXT 需在 Linux PATH。

```bash
cd "$HOME/AutoCrys"
test -x AutoSolve/tools/shelxl
file AutoSolve/tools/shelxl
command -v shelxt
file "$(command -v shelxt)"
```

缺失时报告 `BLOCKED: user-installed SHELX missing`。用户安装后再继续 Gate C。

### XDS / XSCALE / XDSCONV

Agent 不得猜测下载地址或绕过许可证。使用用户提供的合法安装，确保以下 Linux 命令存在：

```bash
command -v xds
command -v xscale
command -v xdsconv
```

缺任一命令时报告 `BLOCKED: missing XDS command(s)`，列出缺失项和已搜索的 PATH。允许先完成
不依赖 XDS 的测试，但 Gate C 不能通过。

### Olex2（可选）

只有用户要求联动时检查，不存在不应阻断核心部署：

```bash
test -f "/mnt/c/Program Files/Olex2-1.5/olex2.exe" && echo "Olex2 found"
```

路径不同则记录实际路径，不要擅自改写配置。

## 6. 可选 AI 配置

AI 不影响 AutoXDS、AutoR3D、AutoSolve、AutoRefine 的核心验收。目标机没有本机配置时才创建：

```bash
cd "$HOME/AutoCrys"
test -e config/ai_assistant.json || \
  cp config/ai_assistant.example.json config/ai_assistant.json
```

目标文件 `config/ai_assistant.json` 可直接使用以下模板。模板中的 `api_key` 为空；用户应只在
目标机本地文件中填入真实密钥。Agent 不得读取、回显或复制已有值：

```json
{
  "assistant": {
    "enabled": false,
    "max_suggestion_chars": 300
  },
  "structure_viewer": {
    "enabled": true,
    "show_unit_cell": true,
    "bond_tolerance_angstrom": 0.45,
    "grow_steps_per_click": 2
  },
  "llm": {
    "mode": "auto",
    "local": {
      "provider": "ollama",
      "host": "http://localhost:11434",
      "model": "",
      "model_pattern": "(?i)(instruct|qwen|llama|gemma|mistral)"
    },
    "cloud": {
      "provider": "openai",
      "api_key": "",
      "api_key_env": "AUTOCRYS_API_KEY",
      "base_url": "https://api.openai.com/v1",
      "model": ""
    },
    "agent": {
      "command": "openclaw",
      "agent_id": "main",
      "session_key": "autocrys",
      "thinking": "",
      "timeout_seconds": 600
    }
  },
  "auto_diagnose": true,
  "auto_context": true,
  "max_console_lines": 200,
  "language": "zh"
}
```

填写 `llm.cloud.base_url`、`model` 和用户自己的 API 配置后重启 AutoCrys。模板不包含机构接口、额度或到期信息。
`llm.mode` 设为 `agent` 时，用户必须显式启动 OpenClaw Gateway；
本部署流程不启动、停止、启用或修改该服务。

密钥仅由用户在目标机编辑 `config/ai_assistant.json` 时填写。AutoCrys 优先读取云端配置的
`api_key`；该值为空时，才使用 `api_key_env` 指向的环境变量作为可选后备。Agent 不读取、
不回显、不记录真实值，也不得把本机配置复制到其他电脑或交付包。

## 7. Gate D：自动化测试

必须在项目根和 AutoCrys 环境中逐项执行，并保存每项退出码与摘要：

```bash
conda activate AutoCrys
cd "$HOME/AutoCrys"
python -m unittest discover -s UI/tests -p 'test*.py'
python -m unittest discover -s AutoDials/tests -p 'test*.py'
python -m unittest discover -s AutoR3D/tests -p 'test*.py'
python -m unittest discover -s AutoXDS/tests -p 'test*.py'
python -m unittest discover -s AutoSolve/tests -p 'test*.py'
python -m unittest discover -s AutoRefine/tests -p 'test*.py'
```

AutoDials 的 `test_service` 不需要 DIALS，在任意目标机都应通过；`test_adapters` 的
原生几何测试在没有历史运行时解析为 skip。真实 DIALS 单数据集、HCA 和合并验收须使用用户提供的测试数据在独立输出目录完成。Git 版不附带源机器的实验验证脚本或结果。

DIALS 是可选外部运行时：由 `scripts/update_self_check.py` 报告，未安装时 AutoDials
标记为 WARN 而非安装失败。

还要检查入口：

```bash
python main.py --help
python -m AutoR3D --help
autocrys --help
autor3d --help
```

验收：所有必需测试退出码为 0。Ollama 集成测试默认跳过是允许的。测试数量可能随代码更新，
不得只根据旧数量判断；当前参考基线见 `GETTING_STARTED.md`。任何失败必须附最后一段 traceback、
失败命令和环境路径，不能宣称 Gate D 通过。

Git 版不分发 Data/Demo。依赖真实实验数据或第三方程序的测试，在缺失前置条件时明确 skip；离线单元测试仍必须全部通过。skip 不代表真实数据处理、SHELX 精修或 DIALS/XDS 全流程验收通过。使用用户自己的非生产测试数据和自行安装的程序，另行完成 Gate E。

## 8. Gate E：GUI 与首次烟雾测试

先验证 Tk/WSLg：

```bash
conda activate AutoCrys
python - <<'PY'
import tkinter as tk

root = tk.Tk()
print("Tk OK:", root.tk.call("info", "patchlevel"))
root.after(100, root.destroy)
root.mainloop()
PY
```

随后从 WSL 启动：

```bash
cd "$HOME/AutoCrys"
autocrys
```

由用户确认窗口出现后，用一份复制的非生产数据完成：导入 → AutoXDS → AutoR3D →
AutoSolve → AutoRefine。至少再导入第二个数据集，确认上一个数据集的规定晶胞和面板状态不会
污染新数据集。不要以真实唯一数据作为首次测试。

Gate E 验收：UI 可打开；测试数据流程符合预期；关闭窗口无异常。若显示变量为空或 Tk 报错，
先记录 `wsl --version` 和 `/mnt/wslg/weston.log`，再建议用户在 PowerShell 执行
`wsl --shutdown` 后重试。不要因此自动执行 `wsl --update`；升级必须遵守 Gate A 的版本检查规则。

## 9. 幂等更新规则

以后重新执行本手册时：

- 现有项目先停止并确认更新策略，不覆盖 `Data/`、`log/` 和本机 AI 配置。
- 现有 Conda 环境用 `conda env update ... --prune`，不自动删除重建。
- `pip install -e .` 可重复执行。
- SHELXT 目标存在时先用 `file` 校验；不静默覆盖不同文件。
- XDS 和 Olex2 仅复用已确认路径。
- 每次更新后重新执行 Gate B、C、D；涉及 UI 时也执行 Gate E。

## 10. Agent 最终报告模板

Agent 必须用以下结构交付，不得省略失败或未检查项：

```text
AutoCrys deployment status: PASS | PARTIAL | BLOCKED | FAIL

Platform
- WSL distro/version:
- Ubuntu version / architecture:
- Project root:

Runtime
- Conda executable:
- Environment:
- Python platform/version/executable:
- pip check:

External tools
- xds:
- xscale:
- xdsconv:
- shelxt:
- shelxl:
- Olex2 (optional):

Validation
- UI tests:
- AutoR3D tests:
- AutoXDS tests:
- AutoSolve tests:
- AutoRefine tests:
- Tk/WSLg:
- End-to-end smoke test:

Security/data
- Existing Data preserved: yes/no/not applicable
- Existing local config preserved: yes/no/not applicable
- Inline API key configured: true/false/not checked
- Secret value printed: must be no

Remaining blockers / exact next action:
```

只有 Gate A–E 全部通过才可报告 `PASS`。缺 XDS、没有 GUI 验收或未做端到端流程时应报告
`PARTIAL` 或 `BLOCKED`，并给出一个明确的下一步。

## 11. 可直接交给 Agent 的提示词

```text
请在目标电脑上严格按照 AutoCrys/DEPLOYMENT_AGENT.md 部署 AutoCrys。
先做只读预检，逐个通过 Gate A–E；需要 sudo、安装 WSL、重启、覆盖目录、
修改 shell 配置、下载许可软件或设置密钥时先停下征求我确认。不要覆盖 Data、log
或 config/ai_assistant.json，不要读取或输出密钥。每条命令保留真实退出状态，最后
按手册第 10 节模板报告；未通过全部 Gate 时不要声称部署成功。
```
