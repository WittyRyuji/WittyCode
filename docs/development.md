# 开发命令与环境

代理的硬约束在 `AGENTS.md`；项目概况和模块图在 `docs/project.md`；事实、决定和坑在 `docs/knowledge.md`。

## 命令

- 本地与 CI 验收使用 Python 3.11.9、Node 24.20.0；Python 入口：`.\.venv\Scripts\python.exe`（`_pyguard` 会拒绝其他版本）。
- **提交前与 CI 对齐**：`.\.venv\Scripts\python.exe scripts/check_ci.py --clean`。在仓库外建立一次性的副本、独立 Git 根与 venv，安装 `requirements-dev.txt -c constraints.txt` 和 `npm ci`，再调用 CI / release 共用的构建、单测、页面检查和 Python 测试。退出时自动回收本次副本和依赖，结果留在 `outputs/test-runs/<本次运行>/result.json`。`--part frontend|python` 可只跑一个 CI 作业；`--part windows` 会重建页面并运行全部被 CI 排除的整机模块（按同一个 glob 自动选取），不要只跑主 e2e 文件。
- 测试（不调用模型）：`.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`。全量约 8 分钟，放到后台跑、输出重定向到文件。`tests/test_app_e2e.py` 是整机测试（真 Qt 窗口 + 真 React 页 + 自有循环接本机假 Responses 服务，需要第二个引擎时用假 Devin），页面没构建时自动跳过；CI 不跑它（`--ignore`），只在本地跑，变异测试也只在本地跑。CI 用 `constraints.txt` 固定依赖版本。
- 构建页面（改 `frontend/` 后必须做，否则应用和 e2e 跑的是旧页面）：`cd frontend; npm run build:app`。界面检查在 `frontend/` 里运行：`node scripts/check-page-readiness.mjs`、`node scripts/check.mjs`（模拟数据与新连接页）、`node scripts/check-live.mjs`（真 QWebChannel 客户端接假后端，不调用模型）、`npm run unit`、`npm run typecheck`。统一入口会强制使用选定 Python 的真实 Qt 客户端，不静默换成 stand-in。
- 启动：`.\.venv\Scripts\python.exe -m desktop`；无控制台：`.\.venv\Scripts\pythonw.exe WittyCode.pyw`。
- 自己开窗口试用时把设置指到 `sandbox/`：`.\.venv\Scripts\python.exe -m desktop --settings sandbox/try/settings.json --project .`（不动 `%APPDATA%\WittyCode\`）。
- 发布包冒烟（离屏，不调用模型，设置在临时目录）：`$env:PYTHONPATH='.'; .\.venv\Scripts\python.exe scripts\smoke_release.py`。
- 真实窗口冒烟（不调用模型，会在屏幕上开一个窗口再自己关掉）：`.\.venv\Scripts\python.exe sandbox\real_window_smoke.py`
- README 截图重拍（演示数据，不含真实对话）：`cd frontend; npm run build; node scripts/readme-shots.mjs`
- 发布包：先 `cd frontend; npm run build:app`，再 `.\.venv\Scripts\python.exe scripts\build_release.py`（`--no-smoke` / `--no-zip` / `--keep-work`），产物在 `dist/`。包的结构和验证见 `docs/project.md`「发布包」。
- 实机探针用便宜的模型，轮数按任务给的上限。
- **评测集**（`evals/`：衡量自有循环的任务通过率、Token 和用时；设计和读法见 `docs/knowledge.md`「Harness 评测集」）：
  - 不调用模型：`.\.venv\Scripts\python.exe -m evals list`（任务和套件）、`-m evals verify`（每个任务原样必须不通过、套上参考解必须通过）、`-m evals compare <运行 A> <运行 B>`（两次运行逐任务对比）。
  - **调用真实模型**：`.\.venv\Scripts\python.exe -m evals run --model <模型> [--reasoning low] [--suite smoke|core|full]`。默认套件 `core`（10 个任务）；默认预算 100 万 token（`--budget 1m`，最多 `5m`，再大直接拒绝），单任务默认 20 万（`--task-budget`）、30 步、900 秒。结果在 `sandbox/eval/runs/<时间>-<模型>/`：`result.json`、`summary.md`、`transcripts/`（每个任务的对话）、`work/`（任务文件夹）。第一次先用 `--suite smoke`（3 个任务）。
  - 套餐模型需要一份登录。默认用评测自己的资料夹：先运行 `.\.venv\Scripts\python.exe -m desktop --settings sandbox/eval/profile/settings.json`，在窗口里登录一次再关掉；也可以用 `--account <含登录的 chatgpt 文件夹>` 指到别处。API 模型用 `--model api:<provider>:<id> --models <models.json>`，密钥按该文件里的 `${变量名}` 从环境读。
  - 评测没有沙箱：批准的命令以当前用户在本机运行，所以只放不需要外部东西的小任务；任务文件夹之外的读写请求一律拒绝。
  - 改了 `evals/` 之后跑变异检查：`.\.venv\Scripts\python.exe sandbox\mutate_evals.py`（在临时副本里跑，约 25 分钟；`--check` 只查模式串）。

## 关于页联系方式

- 本机构建前，可在 Git 已忽略的 `frontend/.env.local` 中设置 `VITE_WITTYCODE_CONTACT_EMAIL=demo@example.com`，保留文件中的其他配置。修改后要重新运行 `npm run build:app`；未配置或配置不符合单个邮箱格式时，「联系方式」指向项目的 GitHub Issues。
- GitHub 发布由维护者在仓库 **Settings → Secrets and variables → Actions → Variables** 中设置 `WITTYCODE_CONTACT_EMAIL`。Release 工作流将它映射为 `VITE_WITTYCODE_CONTACT_EMAIL`，注入执行 `scripts/check_ci.py --part all` 的步骤；该步骤构建随后打包的页面。变量未设置时，发布版一样可用 GitHub Issues 联系。邮箱会写进前端发布产物，只填愿意公开的地址；不要把真实地址放进 Git、示例或测试证据。
- 定向验证（仓库根目录，使用项目 venv 的 Python）：`python desktop/core/test_runs.py -- node --test frontend/scripts/about.test.mjs`。整机验证先设 `$env:WITTYCODE_ABOUT_E2E='1'`，再运行 `python desktop/core/test_runs.py -- python -m pytest tests/test_about.py -q -p no:cacheprovider`；它在临时目录分别构建空配置和示例配置，不读取本机的 `.env.local`，也不覆盖 `desktop/ui_dist`。

## 测试的坑

- 变异脚本 `sandbox/mutate_*.py` 有两种：**在副本里跑**的（`mutate_llm.py`、`mutate_commands.py`、`mutate_harness_g*.py`……，有 `--check`）可以直接跑；**直接改工作树**的老脚本（`mutate_compaction.py`、`mutate_chatgpt.py`、`mutate_curate.py`……）没有 `--check` 保护，`import` 就开跑，被超时杀掉会把变异留在源码里：要跑就完整地跑、不设短超时，最好在仓库的临时副本里跑，跑完用全量 pytest 确认工作树完好。只读地查模式串用 `sandbox/check_old_patterns.py`。细节见 `docs/knowledge.md`「变异脚本两种」。
- 写 e2e 测试前先读 `docs/knowledge.md` 的「e2e 踩出的前三个坑」「e2e 的第四个坑」；崩溃排查见「崩溃排查流程（Windows）」。
- 聊天公式（`frontend/src/lib/markdownMath.ts` 的分隔符归一化）改后跑 `cd frontend; npm run unit`；真 Qt 排版回归在 `tests/test_app_math_e2e.py`（需先 `npm run build:app`，KaTeX 输出 MathML 由 Qt Chromium 原生绘制），公式截图留在 `WITTYCODE_TEST_EVIDENCE/math-chat.png`。
- 测试**不许碰用户的目录**：`tests/conftest.py` 的 autouse `_no_files_of_the_real_user` 把每个测试的 `WITTYCODE_SETTINGS` 指到临时目录；测默认位置的测试自己再 `monkeypatch` `WITTYCODE_SETTINGS` 和 `Path.home`（见 `docs/knowledge.md` 第 67 条）。
- 往工具声明或系统提示里加东西会抬高每个请求的固定 token，按窗口大小调好的压缩测试可能要跟着调（`test_chatgpt_compaction.roomy`）。

## 环境

- Shell 是 Windows PowerShell 5.1：不用 `&&`，用 `;` 和 `$LASTEXITCODE`。
- **不要用 `Get-Content` / `Set-Content` 改带中文的文件**：它们按系统代码页读无 BOM 的 UTF-8，再按 UTF-8 写回，中文全变成乱码。改文件用编辑工具或 Python；`sandbox/scan_mojibake.py` 扫整个仓库，`sandbox/repair_mojibake.py <文件> --write` 能把「UTF-8 被当 Windows-1252 读」的错位还原（可逆）。
- `docs/knowledge.md` 和 `docs/handoff.md` 里有几千字的长行：搜索时只列文件名或限制输出长度。
- 有的代理的 shell 里设置了环境变量 `GIT_SSH_COMMAND`，它会盖过仓库的 `core.sshCommand`：推送前在同一条命令里先重设它（见 `docs/knowledge.md`「斜杠菜单去掉 `/new`…」一条的 (d)）。

## 项目规则

本项目自己的规则，以及 `AGENTS.md` 的规则在这里具体指什么。改代码、开窗口、调用真实模型或提交之前看一遍。

- **语言**：代码只用 Python 和 React（TypeScript）。任何用途都不许引入 C#、C++、Rust 等其他语言，小工具、启动器、安装器也不行；需要的东西用 Python 或前端工具链做。
- **不许改的文件**：`spikes/ctx_long_session.py`、`fixtures/long_session/` 和 `LICENSE`。
- **用户的数据和设置**在 `%APPDATA%\WittyCode\` 和旧位置 `%APPDATA%\droid-desk\`。自己开的窗口用 `--settings` 指到 `sandbox/` 里，并尽快关掉；真实模型调用的工作目录放在 `sandbox/` 下。
- **公开仓库**：下面这些不许进库，截图、录像、测试数据、代码注释、提交说明和文档里都不行——真实路径和用户名、机器信息、令牌和密钥、SSH 密钥文件名、邮箱、账号和会话 id、套餐等级、用户的其他项目和个人内容。演示和测试用中性名字（`demo@example.com`、`C:\Users\me\…`、项目 `wittycode` / `inventory-api`）。测试里的假密钥拆开写（`"sk-" + "…"`），不要绕过密钥扫描。进库前用 `git grep` 自查；更细的清单在 `docs/project.md`「仓库与许可证」。
- **提交格式**：见下面「Git」一节。

## Git

- 远端 `origin` 是公开仓库。提交标题用英文一行祈使句（如 `Add file links in the conversation`），正文写做了什么和为什么，结尾带上做这次改动的代理的 `Co-authored-by` 行。提交前看一遍 `git status` 和暂存的差异，不要把无关文件带进去。
- `sandbox/` 整个被忽略，只放行文档里要求别人跑的脚本；新脚本如果文档要求别人跑，要在 `.gitignore` 里加放行。`desktop/ui_dist/`、`dist/`、`build/`、`.venv*`、`outputs/` 不进库。`.gitattributes` 是 `* text=auto`（库里统一 LF；「LF will be replaced by CRLF」的提示不是问题）。
