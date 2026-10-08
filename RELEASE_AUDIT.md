# AutoCrys Git 版发布验收

日期：2026-10-08。结论：本地 Git 发布内容和离线验收通过；真实实验数据全流程未验收。
本目录为独立 Git 仓库，初始发布分支 main。原工作副本未修改，
对复制前保存的 154 个原文件内容哈希复核一致。未创建远程仓库、未上传。
未添加开源许可证。作者 Shitao Wu、单位 ShanghaiTech，权利声明见 NOTICE.md；
公开 GitHub 仓库无法技术性限制下载，使用等额外权利要求事先书面许可。

## 发布内容与隐私审查

- 不含 Data/、Demo/、log/、缓存、构建产物、旧 wheel 或机器配置。
- 不含本机部署调试日志、个人数据验证记录、实验专用批处理脚本或截图。
- 示例 AI 配置的 API key 为空；机构接口、部署标识、额度、到期信息和个人路径已清理。
- 与本机配置中的实际凭据做了本地比对，无匹配；凭据未复制或写入报告。
- 常见密钥签名、个人路径和实验标识扫描无发现，内部 Markdown 文件链接无缺失。
- 无 SHELXT/SHELXL/SHELXS、PDB2INS、AnoDe、XDS 等外部二进制或下载压缩包。
- Git 忽略本机程序及配置；pyproject.toml 和 MANIFEST.in 不打包第三方程序。
- 部署指南说明用户自行从官方获取 SHELX/XDS；DIALS/Olex2 也自行安装。
- Git 换行规则已统一；git diff --cached --check 通过。

## 测试结果

在无 Data/Demo 的临时副本中串行执行 Python 3.12 unittest。

| 模块 | 总数 | 通过 | 跳过 | 失败/错误 |
|---|---:|---:|---:|---:|
| UI | 82 | 74 | 8 | 0 |
| AutoDials | 20 | 18 | 2 | 0 |
| AutoR3D | 21 | 19 | 2 | 0 |
| AutoXDS | 12 | 12 | 0 | 0 |
| AutoSolve | 10 | 8 | 2 | 0 |
| AutoRefine | 4 | 2 | 2 | 0 |
| 合计 | 149 | 133 | 16 | 0 |

跳过项需要非分发的真实数据、用户安装的 SHELXL、Windows DIALS 或可选 AI 服务。
测试显式声明前置条件，未把跳过记为通过。QC 边界、离线 AI 证据和 dry-run/缺失输入
检查使用合成临时夹具。真实数据回归断言仍保留为可选集成检查。
Tk 测试关闭窗口前取消定时事件，最终测试输出无残留 Tcl 回调错误。

复现命令（项目根目录，已安装 Python 依赖）：

```bash
python -m unittest discover -s UI/tests -p 'test*.py'
python -m unittest discover -s AutoDials/tests -p 'test*.py'
python -m unittest discover -s AutoR3D/tests -p 'test*.py'
python -m unittest discover -s AutoXDS/tests -p 'test*.py'
python -m unittest discover -s AutoSolve/tests -p 'test*.py'
python -m unittest discover -s AutoRefine/tests -p 'test*.py'
git diff --cached --check
```

## 构建和隔离安装

- 86 个 Python 源文件语法检查通过。
- wheel 和源码包构建通过，逐个核查分发包成员：无数据、个人配置或外部程序。
- 源码包包含部署指南、用户手册和本报告。
- 在隔离 venv 安装 wheel；Python 依赖复用既有 Python 3.12 环境的 site-packages，
  pip 安装不访问网络、不改动既有环境。此项不是全新机器联网部署验证。
- pip check 通过；模块导入确认来自隔离 wheel 安装。
- 8 个已安装入口 --help 全通过，源码 main/xds/solve/r3d --help 全通过。
- wheel 中的实际 Tk UI 可构建四个工作流标签并正常关闭，AI 关闭，不调用外部处理程序。
- 自检 --json 正常输出。空配置、缺少 SHELXL 的环境明确报告 SHELXL fail；
  未安装 XDS/SHELXT 时报告 warn，没有自动下载、安装或隐瞒缺失。

## 使用限制及仍需完成的工作

- SHELXL 源码/UI 默认位置为 AutoSolve/tools/shelxl，由用户自行安装且不跟踪。
  wheel CLI 可以使用 --shelxl-bin 显式提供路径；完整 UI 建议源码安装。
- 无实际 TIFF/HKL 流程、SHELXT 求解、SHELXL 精修或 DIALS/XDS 全链验证。
  用户提供自己的非生产测试数据并自行安装程序后，按部署指南 Gate C/E 另行验收。
- 未添加开源许可证；公开查看与额外使用授权分开说明，见 NOTICE.md。
- Git 暂存文件已审查。首次提交使用用户指定的作者姓名和 GitHub noreply 邮箱，
  不读取或公开本机作者邮箱。
- 拟用仓库名 AutoCrys；远程创建、权限设置和上传尚待完成。
