# 开发命令与环境

代理的硬约束在 `AGENTS.md`；项目概况和模块图在 `docs/project.md`；事实、决定和坑在 `docs/knowledge.md`。

## 命令

- 本地与 CI 验收使用 Python 3.11.9、Node 24.20.0；Python 入口：`.\.venv\Scripts\python.exe`（`_pyguard` 会拒绝其他版本）。
- **提交前与 CI 对齐**：`.\.venv\Scripts\python.exe scripts/check_ci.py --clean`。在 `sandbox/` 新建不带产物的副本、独立 Git 根与 venv，安装 `requirements-dev.txt -c constraints.txt` 和 `npm ci`，再调用 CI / release 共用的构建、单测、页面检查和 Python 测试。`--part frontend|python` 可只跑一个 CI 作业；`--part windows` 会重建页面并运行全部被 CI 排除的整机模块（按同一个 glob 自动选取，包含计划／导航／侧栏等独立回归），不要只跑主 e2e 文件；原有缓存、`node_modules`、页面产物和全局 Qt 临时缓存不会混进验收。
- 测试（不调用模型）：`.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`。全量约 8 分钟，放到后台跑、输出重定向到文件。`tests/test_app_e2e.py` 是整机测试（真 Qt 窗口 + 真 React 页 + 自有循环接本机假 Responses 服务，需要第二个引擎时用假 Devin），页面没构建时自动跳过；CI 不跑它（`--ignore`），只在本地跑，变异测试也只在本地跑。CI 用 `constraints.txt` 固定依赖版本。
- 构建页面（改 `frontend/` 后必须做，否则应用和 e2e 跑的是旧页面）：`cd frontend; npm run build:app`。界面检查在 `frontend/` 里运行：`node scripts/check-page-readiness.mjs`、`node scripts/check.mjs`（模拟数据与新连接页）、`node scripts/check-live.mjs`（真 QWebChannel 客户端接假后端，不调用模型）、`npm run unit`、`npm run typecheck`。统一入口会强制使用选定 Python 的真实 Qt 客户端，不静默换成 stand-in。
- 启动：`.\.venv\Scripts\python.exe -m desktop`；无控制台：`.\.venv\Scripts\pythonw.exe WittyCode.pyw`。
- 自己开窗口试用时把设置指到 `sandbox/`：`.\.venv\Scripts\python.exe -m desktop --settings sandbox/try/settings.json --project .`（不动 `%APPDATA%\WittyCode\`）。
- 发布包冒烟（离屏，不调用模型，设置在临时目录）：`$env:PYTHONPATH='.'; .\.venv\Scripts\python.exe scripts\smoke_release.py`。
- 真实窗口冒烟（不调用模型，会在屏幕上开一个窗口再自己关掉）：`.\.venv\Scripts\python.exe sandbox\real_window_smoke.py`
- README 截图重拍（演示数据，不含真实对话）：`cd frontend; npm run build; node scripts/readme-shots.mjs`
- 发布包：先 `cd frontend; npm run build:app`，再 `.\.venv\Scripts\python.exe scripts\build_release.py`（`--no-smoke` / `--no-zip` / `--keep-work`），产物在 `dist/`。包的结构和验证见 `docs/project.md`「发布包」。
- 实机探针用便宜的模型，轮数按任务给的上限。

## 测试的坑

- 变异脚本 `sandbox/mutate_*.py` 有两种：**在副本里跑**的（`mutate_llm.py`、`mutate_commands.py`、`mutate_harness_g*.py`……，有 `--check`）可以直接跑；**直接改工作树**的老脚本（`mutate_compaction.py`、`mutate_chatgpt.py`、`mutate_curate.py`……）没有 `--check` 保护，`import` 就开跑，被超时杀掉会把变异留在源码里：要跑就完整地跑、不设短超时，最好在仓库的临时副本里跑，跑完用全量 pytest 确认工作树完好。只读地查模式串用 `sandbox/check_old_patterns.py`。细节见 `docs/knowledge.md`「变异脚本两种」。
- 写 e2e 测试前先读 `docs/knowledge.md` 的「e2e 踩出的前三个坑」「e2e 的第四个坑」；崩溃排查见「崩溃排查流程（Windows）」。
- 测试**不许碰用户的目录**：`tests/conftest.py` 的 autouse `_no_files_of_the_real_user` 把每个测试的 `WITTYCODE_SETTINGS` 指到临时目录；测默认位置的测试自己再 `monkeypatch` `WITTYCODE_SETTINGS` 和 `Path.home`（见 `docs/knowledge.md` 第 67 条）。
- 往工具声明或系统提示里加东西会抬高每个请求的固定 token，按窗口大小调好的压缩测试可能要跟着调（`test_chatgpt_compaction.roomy`）。

## 环境

- Shell 是 Windows PowerShell 5.1：不用 `&&`，用 `;` 和 `$LASTEXITCODE`。
- **不要用 `Get-Content` / `Set-Content` 改带中文的文件**：它们按系统代码页读无 BOM 的 UTF-8，再按 UTF-8 写回，中文全变成乱码。改文件用编辑工具或 Python；`sandbox/scan_mojibake.py` 扫整个仓库，`sandbox/repair_mojibake.py <文件> --write` 能把「UTF-8 被当 Windows-1252 读」的错位还原（可逆）。
- `docs/knowledge.md` 和 `docs/handoff.md` 里有几千字的长行：搜索时只列文件名或限制输出长度。
- 有的代理的 shell 里设置了环境变量 `GIT_SSH_COMMAND`，它会盖过仓库的 `core.sshCommand`：推送前在同一条命令里先重设它（见 `docs/knowledge.md`「斜杠菜单去掉 `/new`…」一条的 (d)）。

## Git

- 远端 `origin` 是公开仓库。提交标题用英文一行祈使句（如 `Add file links in the conversation`），正文写做了什么和为什么，结尾带上做这次改动的代理的 `Co-authored-by` 行。提交前看一遍 `git status` 和暂存的差异，不要把无关文件带进去。
- `sandbox/` 整个被忽略，只放行文档里要求别人跑的脚本；新脚本如果文档要求别人跑，要在 `.gitignore` 里加放行。`desktop/ui_dist/`、`dist/`、`build/`、`.venv*`、`outputs/` 不进库。`.gitattributes` 是 `* text=auto`（库里统一 LF；「LF will be replaced by CRLF」的提示不是问题）。
