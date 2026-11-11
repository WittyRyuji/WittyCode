# WittyCode Phase 1（MVP）

Factory 官方桌面版把每一次工具调用都铺成一长串瀑布，真正的回答和阶段结果反而被淹没。WittyCode 只解决这一件事：**助手的文字和阶段结果始终完整、醒目；工具调用折叠成可展开的一行摘要**。它是 Factory Droid 的 Windows 桌面客户端，建立在官方 Python SDK（`droid-sdk 0.5.0`）之上，界面文字全部是中文。

> 批次 2 起应用改名 **WittyCode**：窗口标题、设置目录（`%APPDATA%\WittyCode\`）、日志（`WittyCode.log`）、启动文件（`WittyCode.pyw`）和任务栏分组 id 用新名字；Python 包 2026-11-11 起叫 `desktop`（本文的路径已按新名字写）。本文其余地方写的 `droid-desk.pyw`、`droid-desk.log`、`%APPDATA%\droid-desk\` 等旧名字，除历史记录外都已换成新名字。

> 进度流水账见 `docs/phase1-progress.md`；Phase 0 的调研结论见 `docs/phase0-findings.md`、`docs/phase0b-*.md`。

## 1. 运行

### 环境

- Windows 10/11，**Python 3.11.x**（`.python-version` 和 `pyproject.toml` 都锁了 3.11，`desktop/_pyguard.py` 在其他版本上直接拒绝启动）。
- 本机有 Factory 桌面版（自带 `droid.exe`），并且已经登录。凭据由 droid 自己管理，WittyCode 不读取、不保存任何令牌。
- droid 的查找顺序：设置文件里的 `droid_path` → 环境变量 `WITTYCODE_DROID_EXECUTABLE` → `%LOCALAPPDATA%\Factory\app-<最新版本>\resources\bin\droid.exe` → PATH 里的 `droid`。每次启动进程前都会重新查找（桌面版升级会换目录）。

### 安装与启动

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt       # 只运行
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # 加上测试

.\.venv\Scripts\python.exe -m desktop                            # 启动
.\.venv\Scripts\pythonw.exe -m desktop                           # 不带控制台窗口（日志写到设置文件旁边的 WittyCode.log）
.\.venv\Scripts\pythonw.exe WittyCode.pyw                           # 同上；启动失败时用消息框说明原因
```

命令行选项：

| 选项 | 作用 |
| --- | --- |
| `--project DIR` | 启动时加入一个项目文件夹（可重复） |
| `--settings FILE` | 使用指定的设置文件，不碰 `%APPDATA%\WittyCode\settings.json` |
| `--replay FIXTURE` `--speed N` | 离线演示：回放录制的会话，不启动 droid，也不调用模型 |
| `--debug` | 输出调试日志 |

第一次使用：左侧点「添加文件夹」选一个项目，再点「新会话」（或 `Ctrl+N`）。

### 不带控制台窗口

- **快捷方式**：目标写 `"<项目>\.venv\Scripts\pythonw.exe" "<项目>\WittyCode.pyw"`。直接双击 `.pyw` 会用 Windows 关联的解释器，通常没有 `.venv` 里的包，所以要指定项目自己的 `pythonw.exe`。旧名 `droid-desk.pyw` 已在 2026-11-11 删除，改名前做的快捷方式要改成指向 `WittyCode.pyw`。
- **启动失败**：没有控制台就没有地方打印错误，所以 `WittyCode.pyw` 在启动失败时弹出消息框（标题「WittyCode 无法启动」；版本守卫的说明文字、或缺包之类的异常回溯）。`pythonw -m desktop --replay 不存在的文件` 这类启动期错误写进日志文件，退出码为 2。
- **日志**：没有 `stderr` 时，日志写到设置文件旁边的 `WittyCode.log`，即默认的 `%APPDATA%\WittyCode\WittyCode.log`；用了 `--settings FILE` 或环境变量 `WITTYCODE_SETTINGS` 就在那个文件旁边（`--settings` 在配置日志之前就生效）。默认只记警告和错误，不做轮转。日志文件建不出来时不影响启动。
- **子进程的窗口**：`droid.exe` 是控制台程序。没有控制台的进程在不带 `creationflags` 时启动它，Windows 会为它新开一个控制台窗口（本机默认终端是 Windows Terminal，开的就是一个 Windows Terminal 窗口），droid 为 Execute 工具启动的 PowerShell 也共用这个窗口。SDK 没有提供这个选项，补救办法是 `core/winproc.py`（见第 3 节）。

### 离线演示（不需要 droid，不花钱）

```powershell
.\.venv\Scripts\python.exe -m desktop --replay fixtures\stream_turn.jsonl --speed 20
```

窗口打开后：添加一个文件夹（或在命令行里加 `--project .`）、点「新会话」、随便发一条消息，就会回放录制里的下一轮；录制用完之后，再发消息只会得到一句提示。

`fixtures\` 里是 Phase 0 录下的真实事件流：`stream_turn.jsonl`（多工具调用，带两次权限确认）、`ask_user.jsonl`（AskUser 提问）、`interrupt.jsonl` 与 `interrupt_running_tool.jsonl`（打断）、`resume.jsonl`（恢复）。回放里的权限卡片同样要点击才会继续。

### 设置

`%APPDATA%\WittyCode\settings.json`（环境变量 `WITTYCODE_SETTINGS` 可改位置）。改名前的设置留在 `%APPDATA%\droid-desk\settings.json`：第一次启动时**复制**（不是移动）到新位置，只复制一次——新位置已有设置就不动，旧文件永远不删不改（旧版本照样能用），损坏的旧文件不会被复制成半份（先写成临时文件再改名）。损坏的设置文件会被改名为 `.bad` 保留，不会被覆盖。

| 字段 | 含义 |
| --- | --- |
| `droid_path` | 指定 `droid.exe`，空表示自动查找 |
| `default_model`、`reasoning_effort` | 新会话的模型和推理强度，空表示由 Droid 决定 |
| `autonomy` | `off`（默认）/`low`/`medium`/`high`：决定哪些操作需要你确认 |
| `mode` | `auto`（默认）/`spec` |
| `projects` | 侧边栏里的项目文件夹 |
| `max_open_sessions` | 同时打开的会话数，1 到 16，默认 4 |
| `window_geometry`、`splitter_sizes` | 窗口位置和侧边栏宽度，退出时自动保存 |

输入框下方的「模型 / 推理 / 自主 / 模式」既保存为新会话的默认值，也会立刻作用于当前会话；显示的永远是 Droid 实际在用的值（它可能替换掉无效的模型 id，甚至把回合交给另一个模型服务，这时控件和状态栏的上下文窗口都跟着变）。

### 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q        # 644 个，约 38 秒，不调用模型，窗口测试在离屏模式下运行
```

其中 `tests/test_no_console.py` 会启动真的 `pythonw.exe` 小进程，来检查「没有控制台的进程启动控制台程序」时到底有没有窗口；不是 Windows 或找不到 `pythonw.exe` 时这几个测试跳过。

### 实机验证脚本

```powershell
.\.venv\Scripts\python.exe scripts\live_check.py --stages 0          # 只启动、开一个会话、关闭，不发消息，不花钱
.\.venv\Scripts\python.exe scripts\live_check.py --stages 1,2        # 只跑指定阶段（逗号分隔）；不写 --stages 就是 0 到 5 全跑
```

用真窗口（不是离屏）和真 droid.exe，模型固定为 `claude-haiku-5-5`，工作目录 `sandbox/phase1/demo`，用临时设置文件。阶段 1 到 5 各发 1 个回合；回合数上限 6，累计记在 `sandbox/phase1/live_state.json`，跨多次运行都有效，**目前 6 个已经用完**，再跑要发消息的阶段得先加大 `--max-turns`（超出时脚本直接退出，什么都不启动）。会消耗少量 Factory 积分。结果见第 5 节。

```powershell
.\.venv\Scripts\pythonw.exe scripts\live_noconsole_check.py --stages 0                  # 不带控制台启动、开会话、关闭；不发消息，不花钱
.\.venv\Scripts\pythonw.exe scripts\live_noconsole_check.py --stages 0 --control        # 关掉补丁：应当看到控制台窗口，证明监视器看得到问题（不发消息）
.\.venv\Scripts\pythonw.exe scripts\live_noconsole_check.py --stages 0 --probe-stop     # 在空闲会话上试「丢掉停止请求」（不发消息）
.\.venv\Scripts\pythonw.exe scripts\live_noconsole_check.py --stages 1,2,3              # 3 个实机回合（阶段 2、3 接着上一次留下的会话）
```

这个脚本要用 `pythonw.exe` 运行（没有控制台正是被测的条件），所以屏幕上不会打印任何东西：过程记在 `sandbox/phase1/noconsole/check_log.txt`，结果在同一个文件夹的 `report.json`（`finished` 为 true 才算跑完），每次跑完另存一份到 `runs/`，应用自己的日志是 `droid-desk.log`。脚本里有一个监视线程，每秒约 20 次枚举所有可见的顶层窗口和本进程之下的进程；属于 droid.exe、控制台宿主（conhost / OpenConsole）或 droid 子进程（Execute 工具的 shell）的可见窗口，以及启动前不存在的控制台窗口，都算违规。阶段：0 启动；1 一条 Execute 命令（经页面点击批准）；2 命令运行中点「停止」，而 droid 收不到停止请求（请求在 transport 里被丢弃），应用要换掉连接；3 在重启后的会话里追问，要求答出重启前的内容。阶段 1 到 3 各发 1 个回合，上限 3，记在 `sandbox/phase1/noconsole/state.json`，**目前 3 个已经用完**，再跑要发消息的阶段得先加大 `--max-turns`（超出时脚本直接退出，什么都不启动）；`--control` 和 `--probe-stop` 不发消息。结果见第 5 节。

```powershell
.\.venv\Scripts\python.exe scripts\live_attach_check.py --stages a,b                  # 粘贴图片；📎 选文本和 PDF（2 个实机回合）
.\.venv\Scripts\python.exe scripts\live_attach_check.py --stages c --no-follow-up     # 恢复历史里的附件，点缩略图看大图（不发消息）
.\.venv\Scripts\python.exe scripts\live_attach_check.py --stages c                    # 同上，再在恢复出来的会话里追问（1 个实机回合）
```

附件的实机验证：真窗口和真 droid.exe，模型 `claude-haiku-5-5`，工作目录 `sandbox/phase1/attach`，临时设置文件；输入文件放在工作目录之外（`sandbox/phase1/attach-inputs`），所以正确的回答只可能来自附件；提示词禁止用工具。阶段 a：用剪贴板和 `Ctrl+V` 粘贴一张生成的 PNG，发送前还像资源管理器那样粘贴一个文本文件、往对话区拖放一个 PDF（为了截一张三种卡片的图），再用「×」移除这两个，只发图片并问图里有什么（1 个回合）；阶段 b：用 📎 选文本文件和 PDF（文件对话框由脚本代答），问题需要只有它们才有的信息（1 个回合）；阶段 c：关掉标签、从侧边栏恢复同一个会话，检查历史里的缩略图和文件卡片，点缩略图看大图，再追问文本文件里的暗号和 PDF 里的应付金额（1 个回合，加 `--no-follow-up` 就不追问、不花回合）。回合数上限 3（`--max-turns`），累计和会话 id 记在 `sandbox/phase1/live_attach_state.json`，跨多次运行都有效，**目前 3 个已经用完**，再跑要发消息的阶段得先加大 `--max-turns`（超出时脚本直接退出，什么都不启动；`--stages c --no-follow-up` 不发消息，仍可运行）。结果写在 `sandbox/phase1/live_attach_report.json`（每次另存一份到 `runs/attach-*.json`），过程在 `live_attach_log.txt`。脚本会先保存、结束后尽量恢复你的剪贴板（文本、网址、文件链接和图片）；发现 droid 报告的模型不是 `claude-haiku-5-5` 就中止。要发消息的阶段会消耗少量 Factory 积分。结果见第 5 节。

没有 droid 时可以用 `tests/echo_droid.py` 里的假 droid 演练整个脚本（`tests/test_live_attach_script.py`）：它按收到的像素、文本和 PDF 文字作答，也像真 droid 那样存历史。

```powershell
.\.venv\Scripts\python.exe scripts\live_batch2_check.py                # 1 个实机回合：窗口标题、输入框下方的控件、状态栏的上下文显示
.\.venv\Scripts\python.exe scripts\live_batch2_check.py --no-turn      # 只启动、开会话、截图，不发消息，不花钱
```

批次 2 的实机验证：真窗口 + 真 droid.exe，模型 `claude-haiku-5-5`，工作目录 `sandbox/batch2/demo`，临时设置文件 `sandbox/batch2/settings.json`。核对窗口标题是 WittyCode、控件跟随 Droid 报告的模型、新会话的上下文窗口和自动压缩阈值、一个回合之后「已用 / 窗口」的显示；写 `sandbox/batch2/live_report.json`。发消息的完整运行消耗少量 Factory 积分（本批共用了 2 个回合）。结果见第 5 节。

## 2. 界面

- **左侧**：项目文件夹，每个项目下是它已保存的会话（标题、相对时间、状态标记：已打开 / 运行中 / 等待你确认 / 已断开）。点击会话即恢复；项目右侧的「＋」在该文件夹里新建会话。
- **中间**：每个打开的会话一个标签页（小圆点表示状态），下面是转写页和输入框。
  - 你的消息、助手的文字（Markdown + 代码高亮）始终完整显示。
  - 连续的工具调用折叠成**一行**，例如 `4 步 · 读取 2 · 编辑 2`；运行中这一行会显示「正在运行：…」或「等待确认：…」。点击展开成逐步列表，再点某一步看详情：编辑和创建显示行级 diff，命令显示命令和输出尾部（可展开完整输出），其他工具显示输入和结果。
  - 这一行开头的图标按最坏的结果显示：转圈（运行中）、琥珀色 `!`（等你确认）、红色 `!`（有步骤失败，摘要里写「N 个失败」）、琥珀色 `−`（有步骤被取消，摘要里写「N 个已取消」）；只有每一步都完成才是 `✓`。
  - 一轮结束后，末尾给出 `用时 12 秒 · 4 步`；被停止或失败的回合会标明。
  - **权限确认**和 **AskUser 提问**直接嵌在对话里：权限卡片带命令或 diff 预览，AskUser 表单支持单选、多选和自由输入，也可以跳过。窗口里同时有多个会话时，标签页和侧边栏会标出谁在等你。
- **输入框**：`Enter` 发送，`Shift+Enter` 换行；回合运行中按钮变成「停止」（也可以按 `Esc` 或 `Ctrl+.`），运行中可以先写下一条。点了停止、droid 8 秒内还没结束这一轮时，应用会换掉它的进程并恢复同一个会话，对话里多一行「停止超时，已重启会话连接」；这期间会话显示为「正在连接…」，不能发送，换好之后才回到「就绪」。
- **附件**：输入框上方的卡片是下一条消息的附件，三种方式添加：`Ctrl+V`（剪贴板里的图片，或在资源管理器里复制的文件；文字照常粘贴）、📎 按钮（提示「附加文件」，可多选，对话框从项目文件夹开始）、拖放（拖到输入框或对话区）。卡片显示缩略图（图片）或带类型的图标、文件名和大小；「×」移除，点缩略图是大图预览。回合运行中也可以先附加，切换标签页时每个会话各自保留。只有附件没有文字时，发出去的文字是「请查看附件。」。
  - 类型：PNG / JPEG / GIF / WebP（按内容判断）作为图片；其他能解码的图片转成 PNG；PDF 带着提取出的文字；文本和源码直接发文字（UTF-8、UTF-16，GBK 等旧编码会标「按 … 读取」）。其他二进制文件、文件夹不发送，输入框上方说明原因，并可「在提示词中插入路径」。
  - 限制：最多 10 个、合计 20 MiB；图片 5 MiB（超过 5 MiB 或长边超过 8000 像素的缩小到长边 2048 像素以内并重新压缩，卡片标「已压缩」，压缩后仍超限才拒绝）；PDF 3 MiB；文本 1 MiB（比 SDK 的 5 MiB 低得多：1 MiB 文本已经约 25 万 token）；同名文件改成 `名字 (2)`。当前模型已知不支持图片输入时，带图片的消息在发送前被拦下并提示。
  - 发送：消息立刻显示在对话里，带缩略图（Python 生成的 `data:` 图）和文件卡片，点缩略图看大图；带附件的消息发送失败时，对话里写明原因（例如「附件无法发送：PDF 超过 3 MB 的上限。」），文字和附件放回输入框，状态栏提示「这条消息没有发送成功：附件已放回输入框，可以直接重试。」。从侧边栏恢复的历史里，图片和文件也显示成缩略图和卡片（对话里不保留 PDF 和文本的内容，它们只是写着名字和大小的卡片）。
- **会话控件**（批次 2）：顶部没有工具栏；模型、推理、自主级别、模式是输入框下方一排左对齐的小下拉框（宽度跟随文字），跟随当前标签页里 Droid 实际在用的值；改动立刻作用于当前会话，也保存为新会话的默认值。

- **状态栏**（上下文显示是批次 2 加的）：左边是会话状态；右端先是用量，用量左边是上下文显示：`上下文 13K / 197K` 是「已用 / 模型窗口」，不知道的显示 `—`；悬停有逐项解释，包括 Droid 自动压缩对话的阈值。每轮结束、切换模型、Droid 发来压缩或用量通知时都会更新；刚压缩过还没有新的模型调用时，「已用」显示 `—`。

- 快捷键：`Ctrl+N` 新会话，`Ctrl+O` 添加项目，`Ctrl+W` 关闭会话，`Ctrl+L` 聚焦输入框，`Ctrl+End` 跳到最新，`F5` 刷新会话列表，`Ctrl+Q` 退出。

## 3. 架构

```
 ui/       主窗口 · 侧边栏 · 标签页 · 输入框(含下方的会话控件) · 状态栏 · TranscriptView(QWebEngineView + QWebChannel)
   |  监听回调（无信号槽跨线程）        ^ 页面补丁                 | 页面动作（data-act）
 render/   Presenter: Transcript -> 补丁   fragments: HTML 片段   markdown: markdown-it + Pygments
   |
 core/     SessionManager --> SessionController --> Transcript（reducer：SDK 事件 -> 视图模型）
   |            (无 Qt)                history.py：已保存的消息 -> 同样的视图模型
   v
 droid-sdk 0.5.0 (asyncio) --> droid.exe（每个打开的会话一个进程）
```

整个程序是**一个进程、一个事件循环**：qasync 把 asyncio 跑在 Qt 事件循环上，所以 SDK 的协程可以直接在界面里 `await`，不需要线程。

### 模块

| 模块 | 职责 |
| --- | --- |
| `desktop/__main__.py`、`_pyguard.py`、`WittyCode.pyw` | 入口；Python 3.11 版本守卫；`.pyw` 是不带控制台窗口的启动文件，启动失败时用消息框说明 |
| `core/droid_locator.py` | 查找 droid.exe（设置 > 环境变量 > 桌面版 > PATH） |
| `core/settings.py` | 设置的读写与校验（逐字段回退默认值，原子写入）；改名迁移 `migrate_legacy_settings`（只复制一次，旧文件不动） |
| `core/context.py` | 上下文用量：`ContextState`（已用 / 窗口 / 自动压缩阈值 / 模型）、`read_context`（窗口取 `Session.context().limit`；已用和阈值经 `Session._require_client().get_context_breakdown()`，SDK 私有 API）、`format_tokens` |
| `core/winproc.py` | 只在 Windows：让 SDK 启动 droid.exe 时带上 `CREATE_NO_WINDOW`（调用方没给 `creationflags` 时），没有控制台的进程才不会闪出控制台窗口 |
| `core/session_manager.py` | `SessionManager`（会话上限与登记锁、cwd 锁、模型缓存）与 `SessionController`（一个会话的生命周期、回合、打断与换进程、关闭、权限等待） |
| `core/transcript.py` | 视图模型和 reducer：把 SDK 事件归约成「轮 → 块 → 活动组 → 工具项」，并管理待确认提示 |
| `core/history.py` | 把已保存会话的消息还原成同样的视图模型 |
| `core/attachments.py` | 附件（不依赖 Qt）：`Attachment` 和限制常量；类型判断（`sniff_image`、`decode_text`）；`admit`（数量和总大小）；`to_sdk` / `stream_options`（变成 `Session.stream(images=, files=)` 的参数）；`attachments_from_content`、`typed_text`（把已保存消息里的图片和文档块读回来，并去掉 droid 加的 `<system-reminder>`） |
| `core/toolinfo.py`、`core/diffs.py`、`core/prompts.py` | 工具分类与中文摘要；文件变更的行 diff；权限和提问的展示模型 |
| `core/timefmt.py` | 会话列表里的相对时间 |
| `render/markdown.py` | Markdown 转净化后的 HTML（禁用原始 HTML）+ 代码高亮 |
| `render/fragments.py` | 所有 HTML 片段（消息、活动组、工具详情、diff、权限卡片、AskUser 表单、页脚） |
| `render/presenter.py` | `Transcript` 转页面补丁（只重发变了的节点，历史分页，补丁分块） |
| `render/page.py`、`web/` | 离线单页：HTML、CSS、页面脚本全部内联，不访问网络 |
| `ui/app.py` | 命令行、`QApplication`、qasync 事件循环、关闭收尾 |
| `ui/main_window.py` | 主窗口：组装各部件，打开、关闭会话，设置，退出流程 |
| `ui/transcript_view.py` | 转写页（Qt WebEngine，锁定的页面，节流刷新，与 Python 的通道） |
| `ui/sidebar.py`、`session_tabs.py`、`composer.py`、`settings_bar.py`、`status_line.py`、`theme.py`、`tasks.py` | 侧边栏；标签栏；输入框（含 📎、粘贴、拖放，以及挂在输入框下方的会话控件行）；会话控件行（模型 / 推理 / 自主 / 模式下拉框）；状态栏（会话状态、上下文用量、累计用量）；跟随系统的明暗主题；不会静默失败的后台任务 |
| `ui/attachments.py`、`ui/imaging.py` | 把文件、剪贴板图片、拖放数据变成 `Attachment`（`load_path`、`load_mime`、`load_image`，按内容而不是扩展名判断类型）；图片解码、缩小、压缩、缩略图和 PDF 文字提取（`QImage`、`QtPdf`）；`claim_drag` 等拖放辅助 |
| `ui/attachment_bar.py`、`ui/preview.py` | 输入框上方的卡片条（`AttachmentBar`）和提示条（`NoticeStrip`）；大图预览窗口（`ImagePreview`，输入框和对话页共用） |
| `devtools/replay.py` | 把录制的 JSONL 还原成 SDK 事件并回放（`--replay` 与测试共用）；也接受附件并在回放的用户消息里原样回显 |
| `devtools/driver.py`、`devtools/processes.py` | 像用户一样操作真窗口（实机验证脚本和测试共用；含粘贴图片、粘贴文件、点 📎、拖放）；识别属于本进程的 droid.exe |
| `devtools/samples.py` | 已知内容的样例文件：`make_pdf`（手写的单页 PDF，不需要 PDF 库）、`sample_picture`（红方块、蓝圆和一个词） |
| `scripts/live_check.py`、`scripts/live_noconsole_check.py`、`scripts/live_attach_check.py`、`scripts/live_batch2_check.py` | 实机验证脚本；第二个在 `pythonw.exe` 下运行，带一个窗口监视线程；第三个验证附件；第四个验证改名后的标题、会话控件和上下文显示（`--no-turn` 只截图） |
| `tests/echo_droid.py` | 假 droid：按收到的像素、文本和 PDF 文字作答，也像真 droid 那样存历史；用来不花钱演练 `live_attach_check.py` |

### 关键设计决定

**并发。** `core/` 完全不依赖 Qt，只通过 `add_listener` 回调通知界面。每个会话是一个 `SessionController`：一个 droid 进程、一个 `Transcript`、至多一个消费 task（逐个事件归约）。`send_prompt` 同步切换到 `RUNNING` 并启动消费 task，所以界面上「能不能发送」不会和状态错开，也不会重复发送。界面里的后台工作都走 `ui/tasks.spawn()`，异常不会被吞掉，会出现在状态栏。刷新做了节流：转写页每 100 毫秒至多刷新一次（流式输出只重发正在变的文本块）；标签、会话控件、状态栏合并成 120 毫秒一次，但当前会话的输入框和菜单动作同步刷新；侧边栏的会话列表 400 毫秒合并一次。

**同时打开的会话数。** 默认最多 4 个，正在打开的也占名额。已经满了再开一个时，关闭最久没用的**就绪**会话（LRU），并在状态栏提示 12 秒，例如「已自动关闭最久未使用的会话「…」以腾出位置，可在侧边栏重新打开。」（还没发过消息的会话不承诺能重新打开）；提示在关闭之前就发出，标签消失时它已经在了。正在运行、等你确认、正在打开的会话从不被淘汰；已断开的会话不占名额，也不会被自动关掉（它在等你点「重新连接」）；其余全忙就拒绝并提示。
- 登记名额在一把锁里串行做：新控制器先以「正在打开」的状态进入列表（算占着名额），下一个才开始数。否则两个同时发起的打开会看到同一份「已满」的列表、选中同一个会话去关，然后都登记，上限就被超过了。锁只管登记，慢的打开本身不在锁里。
- 同一个已保存会话被同时打开两次，共用一个控制器，不会起两个 droid。
- 用户已经点了关闭、还没关完的会话，它的名额算作马上就空出来：等它关完再数，而不是另外去淘汰一个。
- 淘汰的原因记在控制器上（`eviction_notice`）并通过 `evicted` 事件通知界面；用户自己关的标签不会被说成「自动关闭」。

**cwd 锁。** `Session.resume()` 没有 cwd 参数，droid 继承本进程的当前目录。所以恢复会话时，在 `SessionManager.cwd_lock`（一个 `asyncio.Lock`）里 `os.chdir(项目)` → `await session.open()` → 还原目录；两个同时恢复的会话不会拿到对方的工作目录。新建会话和读取历史都显式传入 cwd，不需要锁。恢复时先用一个一次性的 droid 进程读历史并关掉它，再 resume，所以不会有两个进程同时持有同一个会话。

**没有控制台时启动 droid。** SDK 里只有 `ProcessTransport.connect` 会启动 droid，它调用 `asyncio.create_subprocess_exec`，不传 `creationflags`。`core/winproc.install()` 在创建 `DroidFactory` 时调用（新建、恢复、读历史、读模型列表四条路径都经过它），把 `droid_sdk.transport` 里的名字 `asyncio` 换成一个替身：替身只改 `create_subprocess_exec` 这一个函数，在调用方没给 `creationflags` 时补上 `CREATE_NO_WINDOW`，其余属性原样转给真的 `asyncio`；SDK 别处和本项目其他代码用的 `asyncio` 不受影响。调用方自己给了 `creationflags`（哪怕是 0）就照用。只在 Windows 生效，可重复调用；如果 SDK 以后不再用 `asyncio` 这个名字，补丁不动手并记一条警告，不会悄悄失效。没有用 `DETACHED_PROCESS`：那样 droid 自己没有控制台，它为 Execute 工具启动的每个控制台程序反而要各开一个窗口；`CREATE_NO_WINDOW` 给 droid 一个没有窗口的控制台，它的子进程共用。

**打断与关闭的顺序。**
- 打断：先把回合标为「正在停止」（按钮立刻禁用），再调用 `session.interrupt()`，然后等消费 task 收到终止事件；从发出停止请求算起一共最多 8 秒（`INTERRUPT_GRACE_SECONDS`）。
  - 8 秒内 droid 没有答复停止请求，或答复了却没有结束这一轮：**换掉它的进程**，而不是只取消消费 task。只取消消费 task，SDK 会以为会话空闲了，droid 却可能还在跑这一轮，下一条消息就发给了一个仍然忙着的进程。换进程的顺序：会话状态**同步**改成「正在连接」（从卡住的进程到新进程之间任何时刻都不是「就绪」，所以输入框不会放行）→ 取消消费 task，让所有待确认过期 → 关闭旧的 session（最多等 15 秒，`RESTART_CLOSE_SECONDS`）→ 用同一个会话 id 重新 `resume`（不重载历史）→ 成功后回到「就绪」，对话里写「停止超时，已重启会话连接」，这一轮标为已中断（「本轮已取消。」）。这一整套放在自己的 task 里，调用方被取消（比如窗口正在关）也不会留下半途的状态。
  - 旧进程 15 秒内没关掉：不再去 resume（否则可能有两个 droid 同时持有这个会话），会话进入「已断开」并写明原因；之后点「重新连接」会先等旧进程关完再恢复。resume 失败同样进入「已断开」，错误文字以「停止超时，重启会话连接失败：」开头。
  - 连接已断的错误进入「已断开」，可以点「重新连接」按同一个 id 恢复。
  - droid 明确返回了别的错误（不是超时）：这一轮退出「正在停止」状态，对话里写「停止失败：…」，会话仍在运行，可以再点一次停止。
- 关闭：`close()` 可以重复调用，所有调用共享同一个收尾 task。顺序是：如果还在打开就最多等 10 秒让它打开完（否则进程会成为孤儿）→ 让所有待确认过期并给等待的处理函数返回「取消」→ **先取消消费 task**（SDK 在回合进行中直接 `close()` 会让消费协程卡住；取消消费 task 会顺带给 droid 发 interrupt）→ `session.close()`（进程已崩溃时它会重新抛出旧错误，这里吞掉）→ 等后台 task → 标记已关闭并从管理器里移除。
- 退出：窗口先隐藏并保存几何，所有会话并发关闭（最多 20 秒），然后才结束事件循环，再释放网页视图和共享的 web profile（Qt 要求先删页面再删 profile）。有会话在运行时，关闭窗口会先确认。

**权限与提问的竞态。** SDK 在后台 task 里调用我们的 `on_permission` / `on_question`。我们把请求登记成对话里的卡片，并返回一个 future，用户点击后才完成它。几种竞态的处理：
- 点击和「回合结束 / 被打断 / Droid 自己结算 / 会话关闭」同时发生：只有仍处于「待确认」的提示才接受答案，晚到的点击返回 `False`，页面提示「这个确认请求已经失效」。
- 回合结束、会话关闭、进程死亡或换进程时仍在等的处理函数，会拿到一个正常的「取消」回答（`CancelPermission` / `CancelQuestion`，作为返回值，不是 `CancelledError`），不会泄漏：SDK 把它转给 droid，连接已断时丢掉。处理函数被 SDK 自己取消时，卡片标为已过期，取消继续往上传。
- 提示的最终状态分三种：`answered`（你点了）、`resolved`（Droid 自己结算，实机上打断时就是这样）、`expired`（回合结束或会话关闭时仍没人处理）。
- `PermissionRequest` 没有 id，`PermissionResolved` 带的 `tool_use_ids` 还可能为空，所以只能按 tool_use_id 的交集或「当前所有待确认」来匹配。
- `proceed_edit`（需要编辑 spec 内容）这个选项本客户端提供不了，按钮不显示。

**转写页。** Python 渲染 HTML 片段，页面脚本只做三件事：应用补丁、把点击转发给 Python、收集 AskUser 表单。展开状态（组、工具、完整输出）保存在 Python 一侧，所以重发或切换标签后仍然保留；工具详情在展开时才渲染；历史按 40 轮分页。页面是锁死的：离线、off-the-record profile、不能导航、不能弹窗、链接只允许 http/https/mailto 并交给系统浏览器、没有内联样式和事件处理器、CSP 是 `default-src 'none'` 加 nonce，另外只多一条 `img-src data:`（给你自己的附件缩略图用，见下一段）；Markdown 里的原始 HTML 被关闭，模型在 Markdown 里写的图片仍然只显示占位文字。

**附件。** 数据流：输入框里是一组 `Attachment`（`core/attachments.py`，不依赖 Qt）→ `MainWindow._on_submit` 先问 `SessionController.attachment_problem`（模型已知不支持图片时在这里拦下：droid 对这种模型会悄悄把图片换成一句说明，看上去像是模型无视了图片）→ `send_prompt(text, attachments)` 同步切到 `RUNNING`，`Transcript.start_turn(text, attachments)` 立刻把带附件的消息放进对话 → 消费 task 调用 `session.stream(text, **stream_options(attachments))`，也就是 `images=[…]` 和 `files=[…]`。三种形态在 SDK 里是：
- 图片：`Image.from_bytes(data, media_type=…)`；SDK 检查文件头，只收 PNG / JPEG / GIF / WebP。
- PDF：`Document(PdfDocumentSource(data=<base64>, parsed_data=<提取出的文字>, name=…, path=…))`。文字用 Qt 的 `QtPdf` 取，最多 40 万字符，给没有原生 PDF 支持的模型用。
- 文本和源码：`Document.from_text(text, name=…, mime="text/plain")`。
- 数据不合格时 SDK 抛 `InvalidAttachmentError`；`attachment_failure` 把它翻成一句人话（「附件无法发送：PDF 超过 3 MB 的上限。」）。

设计决定：
- **限制在发送之前判断。** 数量、总大小、每种文件的上限都由 `admit` 和 `ui/attachments.py` 先判断，用户在输入框上方就看到原因，而不是等 droid 报错。文本上限 1 MiB，故意比 SDK 的 5 MiB 低得多（1 MiB 文本约 25 万 token，比多数上下文窗口还大）；超过 64 MiB 的文件连读都不读。类型按内容判断：图片看文件头，PDF 看 `%PDF`，文本先看扩展名再用解码和控制字符比例验证，没有认得的扩展名但内容是干净的 UTF-8 / UTF-16 也收。
- **图片怎么压缩（`imaging.fit_image`）。** 依次试：原尺寸的无损 PNG（5 MiB 以内、长边 8000 像素以内）；缩到长边 2048 像素的 PNG；缩小后的 JPEG，质量 90 到 40 逐级降；再把 JPEG 缩到 1536、1024、768、512 像素。JPEG 文件（照片）直接走 JPEG；透明背景在 JPEG 里垫白。只有数据真被丢掉（缩小或存成 JPEG）才标「已压缩」；BMP、TIFF 等文件无损转成 PNG 时标「已转为 PNG」；剪贴板里的图片总是重新编码成 PNG，没有被丢数据时不加标记。
- **复制，不移动。** 拖放一律以「复制」接受（`copy_action`）：资源管理器在接受「移动」的投放之后会删掉原文件。只提供「移动」的拖放被拒绝，而且不会漏给会把文件当文本收下、或者导航到文件的控件（`claim_drag`）。
- **拖到对话区。** Qt WebEngine 的页面会自己处理投放。`TranscriptView` 在页面的 `focusProxy()` 上装事件过滤器，抢在页面之前接走拖放，交给输入框去附加；页面本身仍然不能导航，也看不到文件。
- **失败后放回去。** 带附件的消息这一轮失败（`TurnStatus.FAILED`）时，`MainWindow._settle_inflight` 把文字和附件放回输入框（不覆盖用户已经新写的文字；用户已经切到别的标签页，就放进那个会话的草稿和附件队列），并在状态栏提示。草稿和附件队列都按会话保存。
- **对话里的显示。** 用户消息块带 `attachments`。`Transcript` 保存的是 `slim()` 之后的副本：图片留着数据（点开大图要用），PDF 和文本只留名字、大小和类型。缩略图由 Python 生成（`imaging.thumbnail`：长边 320 像素，JPEG；有透明度的保持 PNG；每个附件只做一次），以 `data:` URI 写进 `<img>`；整个页面里只有 `attachments_html` 写 `<img>`。点缩略图发 `data-act="preview"`，Python 打开原生的 `ImagePreview` 窗口（不超过屏幕的 85%），不在页面里放大。
- **服务器回显与 `<system-reminder>`。** droid 会把用户消息回显回来，`Transcript._on_user_message` 把它合并进本地已经显示的那一块（附件以本地的为准，没有本地块才用回显里的）。droid 给带图片的消息另外保存一个文本块 `<system-reminder>Attached image local file paths: …</system-reminder>`，它不是用户写的：`typed_text` 把以 `<system-reminder>` 开头、以 `</system-reminder>` 结尾的文本块去掉，回显和历史还原（`core/history.py`）都用它，所以对话里和会话标题都不会出现这段话。这个坑是实机验证时发现的（里程碑 6）。
- **历史里的附件。** `attachments_from_content` 把已保存消息里的图片块和文档块读回成 `Attachment`。`DroidClient.load_session` 返回的图片是完整数据，所以历史里也有缩略图；PDF 的字节也被还原（磁盘上的 jsonl 里 PDF 的 `data` 是空串，名字和路径在）。droid 保存时会重新编码图片，历史里图片的大小不一定和发送时相同（实机：发送的 PNG 10.6 KB，历史里是 RGBA 的 PNG，11.4 KB）。

**上下文用量（`core/context.py`，批次 2）。** 状态栏显示三个数：**已用**是 Droid 记录的最近一次模型调用的体量（`get_context_breakdown().last_call_compaction_tokens`：输入 + 缓存读取 + 输出，随会话保存，恢复的会话不用等新一轮就有数；`Session.context().used` 只是固定提示的估计，不跟对话走，不用它）；**窗口**是模型的上下文窗口（`Session.context().limit`，claude-*-5-5 是 872000 = 1M 减去 128K 输出；跟着模型走）；**阈值**是 Droid 自动压缩对话的线（`get_context_breakdown().context_budget`：默认 min(250 000, 窗口)，可被用户的每模型 `compaction_token_limit` 覆盖，这批实机上 claude-*-5-5 是 800 000）。状态栏斜线后面是窗口（硬上限、跟着模型走），不是阈值；阈值在悬停提示里。两点要留意：breakdown 不在高层 API 里，要经 `Session._require_client()`（私有，SDK 锁 0.5.0，`tests/test_context.py::test_the_sdk_still_offers_what_the_context_reading_relies_on` 守住这个接口，私有门失效时只少「已用」和阈值，窗口仍在）；读取由 droid 的通知驱动（`Session.on_notification`：`session_token_usage_changed` 和 `session_compacted` 时重读，每轮结束和换模型也重读），压缩后 Droid 可能在一小段时间里还报压缩前的数，所以收到压缩通知先把「已用」标成未知，读到的数和压缩前相同就当它还没更新。

## 4. 完成情况

Phase 1 的范围都已完成：

- 会话：新建、恢复已保存会话（带历史）、关闭、断线重连、多标签（带上限）。
- 项目侧边栏：添加、移除项目，已保存会话列表和状态标记，自动刷新。
- 转写页：折叠的工具组、diff、命令输出、思考、Markdown 与代码高亮、历史分页、「回到最新」。
- 权限卡片（带命令或 diff 预览）与 AskUser 表单，嵌在对话里。
- 停止（按钮、`Esc`、`Ctrl+.`）；退出时干净地收掉所有 droid 进程。
- 会话控件、状态栏、设置持久化、窗口几何、跟随系统的明暗主题。
- 离线回放模式、644 个自动测试（代码评审修复后是 404 个，附件新增 184 个，批次 2 新增 56 个）、四个实机验证脚本。
- 代码评审之后的修复（`docs/phase1-progress.md` 里程碑 5）：不带控制台启动时 droid 不再闪出窗口；停止后 droid 不响应时换掉它的进程再恢复会话；会话上限在并发打开时不再被突破，自动关闭会明确提示；过期的权限和提问返回正常的「取消」回答；含被取消工具的工具组不再显示 `✓`。
- 附件（`docs/phase1-progress.md` 里程碑 6，用户反馈「不能粘贴图片，也不能附加本地文件」）：输入框可以 `Ctrl+V` 粘贴图片和文件、点 📎 选文件、把文件拖到输入框或对话区；图片、PDF、文本作为附件发给模型（`Session.stream(images=, files=)`），超限的图片自动压缩，不支持的二进制文件说明原因并可插入路径；对话和恢复出来的历史里显示缩略图和文件卡片，点缩略图看大图；发送失败时消息和附件放回输入框。实机用 3 个回合验证过图片、文本和 PDF（第 5 节）。
- 批次 2（`docs/phase1-progress.md` 里程碑 7）：应用改名 WittyCode（窗口标题、设置目录、日志、启动文件、任务栏分组 id；旧设置第一次启动时复制过来，不移动不删除；包名和文件夹名不变）；模型 / 推理 / 自主 / 模式从顶部工具栏移到输入框下方的一排小下拉框，跟随 Droid 实际在用的值；状态栏在用量左边显示上下文「已用 / 窗口」，悬停解释含自动压缩阈值；修复了全量测试才暴露的截图测试（窗口和内容未画完就截图，改为等绘制完成并以屏幕像素兜底）。实机用 2 个回合验证（第 5 节）。

## 5. 验证结果

### 自动测试

`pytest tests -q`：**644 passed**（连续 3 次，另加 1 次全部用例逆序收集的运行，约 38 秒）。全部不调用模型。里程碑 4 结束时是 352 个；评审修复新增 53 个，并换掉 1 个过时的（`test_stuck_interrupt_falls_back_to_cancelling_the_turn` 断言的正是旧的「只取消消费 task」行为），净增 52 个，是 404 个；附件又新增 184 个（6 个新测试文件占 179 个，其余在 `test_driver` 等原有文件里）；批次 2 净增 56 个（新的 `test_context.py` 占 38 个，其余在 `test_settings.py`、`test_ui_widgets.py`、`test_ui_window.py`、`test_driver.py` 等原有文件里，含改名迁移和控件下移的改写）。

| 测试 | 内容 |
| --- | --- |
| `test_locator`、`test_settings`、`test_timefmt`、`test_toolinfo`、`test_diffs`、`test_prompts` | 纯函数与数据模型 |
| `test_reducer_fixtures`、`test_reducer_synthetic`、`test_history` | 把所有录制夹具逐个回放进 `Transcript`；构造的边界事件；历史还原 |
| `test_session_manager` | 用可编排的假 Session：并发打开与关闭的竞态、打断与看门狗、权限与提问的各种竞态（过期、关闭、进程死亡时返回的是取消回答，不是异常）、重连、cwd 锁；会话上限与淘汰（淘汰有提示并点名、只淘汰就绪的会话、两个并发打开不超限也不抢同一个、用户正在关的会话让出名额、同一个已保存会话的并发打开共用一个控制器）；停止超时后换进程（droid 不答复、答复了却不结束、旧进程没及时退出则进入已断开且重连时等它退出、resume 失败、中途关闭、调用方被取消、别的错误时可以重试） |
| `test_no_console` | 补丁只在没给 `creationflags` 时加 `CREATE_NO_WINDOW`（显式给的 `0`、`0x200` 等原样保留），别处的 `asyncio` 不受影响，重复安装、非 Windows、SDK 变了时的处理；新建、恢复、读历史、读模型列表四条路径都带标志；用**真的 `pythonw.exe` 子进程**对照：不打补丁时它启动的控制台程序有控制台窗口，打补丁后没有；没有控制台时日志写进文件，文件建不出来也能启动；`pythonw -m desktop` 和 `WittyCode.pyw` 启动失败时的退出码和日志 |
| `test_markdown`、`test_fragments`、`test_presenter`、`test_page` | 渲染；对所有夹具的所有展开状态做「标签和属性白名单」检查；补丁与分页；工具组图标（全部完成才是 `✓`，失败、取消、运行中、等待各有自己的，失败优先于取消）以及每种图标都有样式规则 |
| `test_ui_transcript`、`test_ui_widgets`、`test_ui_window` | 真实的 Qt WebEngine 页面（离屏）：点击、展开、权限和提问表单、含被取消工具的组画出的是 `−` 而不是 `✓`（量页面里算出来的样式）；各个部件；整窗口的流程（新建、发送、停止、停止被忽略时换进程、恢复、关闭、关机、状态栏点名被自动关闭的会话） |
| `test_driver` | 实机脚本用的驱动先在回放和假会话上跑通（含粘贴图片、点 📎、在历史里找附件、说明消息为什么没发出去）；截图在页面画完之前会重试，部件截图漏掉页面时用屏幕像素兜底，两处都拍不到就拒绝存图 |
| `test_context`（38，批次 2） | 上下文用量：`format_tokens` 的写法；状态栏文字和悬停提示（不知道的显示 `—`）；`read_context`（有 / 没有 breakdown、会话不能回答时抛错）；新会话只知窗口、恢复的会话直接有数、回合结束和回合中的模型调用后刷新、换模型后窗口和阈值跟着变、压缩后「已用」立刻变未知且旧数不被当成新数、读取失败不显示过期数字、并发读取合并、换进程后只读新会话、关闭会话停掉正在进行的读取；SDK 私有接口还在（`test_the_sdk_still_offers_what_the_context_reading_relies_on`） |
| `test_attachments`（50） | 附件的规则，不需要窗口：图片按内容而不是扩展名判断；BMP / TIFF 转成 PNG；PDF 带着文字（没有文字层也能附加）；文本和源码按编码读取；二进制、文件夹、空文件、损坏的图片被拒绝并说明原因、保留路径；超限的图片逐级压缩，压不小才拒绝；数量和总大小上限；重名编号；变成 SDK 的 `Image` / `Document`，SDK 拒绝的数据抛 `InvalidAttachmentError`；缩略图和 PDF 文字 |
| `test_attachments_session`（37） | 附件从输入框到 SDK：`stream` 收到的选项名是 `images=` 和 `files=`，没有附件时一个都不传；只有附件时发默认文字；SDK 拒绝时这一轮带着可读的原因失败；已知不支持图片的模型在发送前就被拒绝，未知的不拦；已保存的消息把图片和文档读回来；droid 保存的 `<system-reminder>` 不算用户输入（历史、标题、实时回显）；回放会话接受附件并回显 |
| `test_attachments_render`（26） | 页面里的附件：缩略图和文件卡片的标记；大图先按缩略图尺寸留位置；文件名在任何位置都被转义；只有 PNG / JPEG 的 `data:` 地址才会变成 `<img>`（`javascript:`、`file:`、`https:`、SVG、GIF、带引号的注入都不行）；没有内联脚本和样式；模型 Markdown 里的图片仍然是占位文字；历史里的附件和实时的显示一样 |
| `test_ui_attachments`（40） | 输入框，真的 Qt 部件（离屏）：`Ctrl+V` / `Shift+Insert` 粘贴图片、粘贴资源管理器式的文件；文字照常粘贴，带图片的文字（电子表格的单元格）按文字粘贴；📎 的对话框从项目文件夹开始；拖放以复制接受，只给「移动」的被拒绝（资源管理器才不会删原文件），文字拖放不受影响；卡片的名字、大小、移除、大图；回合运行中排队；队列按会话切换；放回输入框时旧的在前、超限时说明 |
| `test_ui_attachments_window`（21） | 整窗口：页面显示缩略图和文件卡片，CSP 放行图片；恶意文件名只是文字；点缩略图开大图；拖到对话区；粘贴、📎、拖放的附件随消息发出；只有附件时发默认文字；每个会话各自的队列；SDK 拒绝时消息和附件放回输入框，成功时什么都不恢复；模型不支持图片时发送前被拦下，其他文件不受影响 |
| `test_live_attach_script`（5） | 用假 droid（`tests/echo_droid.py`）把 `live_attach_check.py` 的三个阶段完整跑一遍（带和不带追问），以及回合预算和阶段参数的检查 |

### 实机验证

`scripts/live_check.py`，真窗口 + 真 droid.exe（0.251.0，来自桌面版 0.208.0），模型 `claude-haiku-5-5`，自主级别 off。**共用了 6 个实机回合（上限 6，已用完）**，详细记录在 `docs/phase1-progress.md` 的里程碑 4。

| 阶段 | 做了什么 | 结果 |
| --- | --- | --- |
| 0 | 启动、读 69 个模型、开会话、关窗口 | 通过；关窗后没有残留的 droid.exe，机器上其他 droid 进程（桌面版的）没有被动 |
| 1 | 多工具回合：列目录、读文件、创建、编辑；创建和编辑各有一次权限确认，由脚本点击页面里的按钮批准 | 通过：`hello.txt` 最终内容为 `hi there`，4 步折叠成 2 组，展开的编辑行显示 diff |
| 2 | AskUser，选第一个选项 | 通过：卡片一直等到作答，回答送达，回复里用了所选的词 |
| 3 | 命令需要确认，卡片还开着时点「停止」 | 通过：回合以「已中断」结束，卡片消失，0.1 秒回到就绪。第一次跑有 1 项检查失败：我预期提示变成 `expired`，实际是 Droid 自己先结算成了 `resolved`；检查改为两者都接受后，用第 6 个回合重跑了这一阶段，12 项全部通过（提示状态 `resolved`） |
| 4 | 关闭标签（确认 droid 进程消失）→ 点侧边栏恢复 → 追问 | 通过：恢复出同一个会话、历史 3 轮，追问正确答出 `hi there` |
| 5 | 批准一条运行 100 秒的命令，运行中点「停止」 | 通过：1.8 秒回到就绪；droid 在 3 秒内结束了它的 powershell/python 子进程，没有孤儿进程 |

### 实机验证：不带控制台启动与停止超时（代码评审修复）

`scripts/live_noconsole_check.py`，全程由 `.venv\Scripts\pythonw.exe` 运行：进程没有控制台（`stdout`、`stderr` 都是 `None`，控制台窗口句柄为 0，没有进程挂在控制台上），真窗口 + 真 droid.exe，模型 `claude-haiku-5-5`，自主级别 off。监视线程每秒约 20 次枚举可见窗口和本进程之下的进程。**共用了 3 个实机回合（上限 3，已用完；和上面的 6 个分开计数）。**

| 运行 | 做了什么 | 实机回合 | 结果 |
| --- | --- | --- | --- |
| 对照 | `--stages 0 --control`：关掉补丁，只启动、开会话、关闭 | 0 | 「看到了问题」：启动后 0.73 秒（读模型列表的 droid）和 3.16 秒（会话的 droid）各出现一个整屏大小（1752×936）的 Windows Terminal 窗口（类名 `CASCADIA_HOSTING_WINDOW_CLASS`），droid.exe 还各有一个 0×0 的 `PseudoConsoleWindow`。`sandbox/phase1/noconsole/runs/20261029-124256-control.json` |
| 空闲 | `--stages 0`：打补丁，同样的操作 | 0 | 通过：161 次轮询、3409 次窗口检查，0 次违规；会话由 1 个 droid.exe（和它的 conhost.exe）服务；关窗后没有残留的 droid.exe，桌面版的 4 个 droid 进程没被动。`runs/20261029-124320-stages-0.json` |
| 探针 | `--stages 0 --probe-stop`：在空闲会话上丢掉 `droid.interrupt_session` 请求 | 0 | 通过：空闲的 droid 0.00 秒就答复停止请求；被丢掉的请求从不被答复（所以阶段 2 里 droid 对停止请求确实「没有反应」）；0 次违规。`runs/20261029-124332-probe-stop.json` |
| 回合 1 | `--stages 0,1`：`Get-Date; Start-Sleep -Seconds 4`，批准 1 次权限 | 1 | 通过：回合完成，回复「The time PowerShell printed was 29 October 2026 12:36:31. The command exited with code 0.」；期间 droid 启动了 powershell.exe；422 次轮询、8874 次窗口检查，**0 次违规**；见过的子进程：droid.exe ×2、conhost.exe ×8、powershell.exe ×6、git.exe ×1，没有一个有可见窗口。这次运行的报告被下一次覆盖了，数字是跑完时读到的（之后脚本改成每次另存一份） |
| 回合 2、3 | `--stages 2,3`：运行 `Start-Sleep -Seconds 120`，运行中点「停止」，droid 收不到停止请求；重启后追问 | 2 | 25 项检查全部通过（见下）；865 次轮询、18129 次窗口检查，**0 次违规**；这次还见到 cmd.exe、taskkill.exe。`runs/20261029-123752-stages-2-3.json`（同名的 `.log`、`.app-log.txt` 是过程日志和应用日志） |

阶段 2 的细节（来自上面最后一行的报告）：

- 「停止」按下 8 秒后应用日志出现 `droid did not stop within 8 s; replacing its process`；会话状态依次是 `running → opening → ready`，中间没有出现 `ready`；从点停止到回到就绪 12.7 秒；脚本共丢弃了 2 个 `droid.interrupt_session` 请求。
- droid.exe 从 pid 32000 换成 10372；旧的 droid.exe 和它之下的 powershell、conhost 都消失了。
- 对话里有「本轮已取消。」和「停止超时，已重启会话连接」，这一轮标为已中断，输入框又可以发送。
- 追问（英文提示，大意是「不用任何工具：我上一次请求你运行哪条 PowerShell 命令？」），回答里说的是 `Start-Sleep -Seconds 120`：重启后的会话带着重启前的上下文。
- 关窗口后：没有残留的 droid.exe，没有新增的 droid.exe，桌面版的 droid 进程没被动，监视线程见过的所有子进程都已退出。
- pythonw 下没有 stderr，应用日志写进了 `sandbox/phase1/noconsole/droid-desk.log`（里面有换进程的警告）。

「droid 卡住」是模拟的：真的 droid 总是及时答复停止请求（探针里 0.00 秒），没法让它自然卡住，所以在 transport 里丢掉 `droid.interrupt_session` 请求。这验证了应用的换进程流程和真 droid 的重新 `resume`，没有验证一个真的卡住的 droid 是什么样子。

### 实机验证：附件

`scripts/live_attach_check.py`，真窗口（平台是 `windows`，不是离屏）+ 真 droid.exe（0.251.0，来自桌面版 0.208.0），模型 `claude-haiku-5-5`（脚本核对 droid 报告的就是它，否则中止），自主级别 off，会话的工作目录 `sandbox/phase1/attach`，输入文件在 `sandbox/phase1/attach-inputs`。**共用了 3 个实机回合（上限 3，已用完；和前面两组分开计数）。** 报告在 `sandbox/phase1/runs/attach-*.json`，过程日志是 `sandbox/phase1/live_attach_log.txt`。

| 运行 | 做了什么 | 实机回合 | 结果 |
| --- | --- | --- | --- |
| 1 | `--stages a,b` | 2 | 27 项全部通过。阶段 a：往真剪贴板放一张生成的 PNG（640×360：红方块、蓝椭圆、一个词），在输入框按 `Ctrl+V`，出现缩略图卡片「粘贴的图片.png」（10.6 KB）；又像资源管理器那样粘贴 notes.txt、往对话区拖入 invoice.pdf，检查三种卡片，再用「×」移除后两个，只发图片，问「描述这张图」：回复「A red rectangle on the left and a blue ellipse on the right sit above the word "TANGERINE-42" on a pale yellow background.」，对话里的用户消息带着缩略图。阶段 b：点 📎（文件对话框由脚本代答）选 notes.txt 和 invoice.pdf，问暗号和 PDF 里的应付金额：回复 `codeword=MARMALADE-7731; total=1250.00 EUR`（这两个事实只存在于附件里）。实时回显的用户消息文字和输入的完全一样，服务器没有改写它 |
| 2 | `--stages c --no-follow-up`：关标签，从侧边栏恢复这个会话，检查历史 | 0 | 19 项里 **1 项失败**：历史里带图片的那条用户消息，文字以 `<system-reminder>Attached image local file paths: …` 开头，侧边栏里的会话标题也是它（见下）。其余通过：缩略图、两张文件卡片（TXT notes.txt 89 B、PDF invoice.pdf 697 B）、点缩略图开出 640×360 的大图窗口 |
| 3 | 修好之后重跑同一阶段 | 0 | 19 项全部通过 |
| 4 | `--stages c`（带追问） | 1 | 21 项全部通过；在恢复出来的会话里追问，回复 `codeword=MARMALADE-7731; total=1250.00 EUR`：恢复之后上下文（包括 PDF 里的金额）还在 |

四次运行前后的 droid 进程相同（桌面版自己的 4 个，没有被动），没有残留的 droid.exe，也没有出现意外的权限提问。

实机发现的问题和事实：
1. **droid 在带图片的用户消息里另存了一个文本块** `<system-reminder>Attached image local file paths: … temporary copies …</system-reminder>`，顺序是：图片、这段提示、你打的字。旧代码把所有文本块拼在一起，所以历史里这条消息的文字和侧边栏里的会话标题都以这段提示开头。已修：`typed_text`（`core/attachments.py`）去掉整块的 `<system-reminder>`，历史（`core/history.py`）和实时回显（`Transcript._on_user_message`）都用它；测试里的 `attachment_history()` 也改成真 droid 的块顺序。实机重跑通过（运行 3、4）。
2. `DroidClient.load_session` 返回的 PDF 带着字节（磁盘上的 jsonl 里 `data` 是空串；droid 把文件另存在会话目录的 `attachments\` 下，读历史时还原），所以历史里 PDF 卡片能显示大小（697 B）。
3. droid 保存时会重新编码图片：发送的 PNG 是 10.6 KB，历史里是 RGBA 的 PNG，11.4 KB，像素尺寸相同（640×360）。
4. droid 对 `claude-haiku-5-5` 的 `noImageSupport` 是 `False`，所以这个模型收图片；「模型不支持图片」的拦截只有离线测试。

### 实机验证：批次 2（改名、会话控件、上下文显示）

`scripts/live_batch2_check.py`，真窗口 + 真 droid.exe（0.251.0），模型 `claude-haiku-5-5`，自主级别 off，工作目录 `sandbox/batch2/demo`，设置文件 `sandbox/batch2/settings.json`。**共用了 2 个实机回合；另有 3 次 `--no-turn` 运行只截图，不发消息。** 报告 `sandbox/batch2/live_report.json`，过程日志 `sandbox/batch2/live_log.txt`。

| 运行 | 做了什么 | 实机回合 | 结果 |
| --- | --- | --- | --- |
| 1 | 完整运行 | 1 | 11 项里 1 项失败：脚本硬编码了 Haiku 的窗口和阈值，而 Droid 把回合交给了 kimi-k3（见下）；应用行为本身是对的 |
| 2 | 检查改成按 Droid 报告的实际模型核对后重跑 | 1 | 12 项全部通过 |
| 3–5 | `--no-turn`：只启动、开会话、截图 | 0 | 通过（前两次的标题截图混进了别的窗口；第三次起截标题图前把窗口临时置顶，截图干净） |

实机数字：

- 新的 Haiku 会话还没发消息：used=None、window=**872000**、budget=**800000**——和「1M 窗口减 128K 输出」「用户的每模型压缩上限 800000」一致。
- 发了一个回合之后：used=13086、window=**196608**、budget=**196608**、model=**kimi-k3**——Droid 不报错地把回合交给了 kimi-k3，会话设置随后也报告 kimi-k3，模型下拉框跟着变成 kimi-k3；状态栏显示「上下文 13K / 197K」。kimi-k3 的窗口是 196608（192K），阈值等于窗口（窗口小于默认的 250 000，也没有给它配每模型压缩上限）。运行 1 的同口径数字是 used=13119。
- 每次运行结束都没有残留的 droid.exe，机器上其他 droid 进程（Factory 桌面版的）没有被动。

新事实（2026-10-29，droid 0.251.0）：**droid 会把回合交给另一个模型服务而不报错**（账户级行为：请求 claude-haiku-5-5，实际 kimi-k3）；`get_context_breakdown().model_id` 报告实际服务的模型，窗口和阈值跟着它走。「界面显示 Droid 实际在用的模型」这条设计因此在实机上被验证了一次。

截全窗口（带系统标题栏）要从屏幕上读像素，被别的窗口盖住就会把别的窗口拍进去：脚本截标题图之前把窗口临时置顶（`WindowStaysOnTopHint`，不抢输入焦点），截完恢复。

## 6. 已知限制

**没有验证过的**
- 「停止后 droid 不响应」时换进程的流程，在真 droid 上只用「丢掉停止请求」模拟过（见第 5 节）；没有遇到过自然卡住的 droid，所以真实卡住时的样子（比如它还在不在写会话文件）没有看过。旧进程 15 秒内关不掉的那一支只有假 Session 的测试覆盖。
- 没有窗口闪现这一点只在这一台机器上验证过，它的默认终端是 Windows Terminal。监视器同时看 `ConsoleWindowClass`（传统控制台）和 `CASCADIA_HOSTING_WINDOW_CLASS` 两种窗口，但没有在默认终端是传统控制台的机器上跑过。
- 含被取消工具的工具组的 `−` 图标，只在真实的 Qt WebEngine 页面里量过算出来的样式（`::before` 的内容和颜色），没有重新截图；`docs/screenshots/` 里的图是修复之前取的。
- 显示缩放只在 125% 的单显示器上看过（窗口 1320×860 逻辑像素）；其他缩放、多显示器没有试。
- 深色主题有 CSS 和测试，没有做实机截图核对。
- 实机只测了 Haiku 5.5、自主级别 off、Auto 模式；Spec 模式、`high` 自主级别、多个会话同时在跑，都只有离线测试。
- 没有做过长会话下的界面性能测量（只做了历史分页）。
- 附件的真人操作：`Ctrl+V` 用的是真剪贴板，但按键是 Qt 合成的；拖放用的是合成的 `QDropEvent`（发给输入框的文本框和对话页的 `focusProxy()`），在资源管理器里真的拖一个文件进来、经过系统的 OLE 拖放，没有试过；📎 的系统文件对话框由脚本代答，没有操作过真的对话框。脚本结束后恢复你的剪贴板只是尽力而为（文本、网址、文件链接、图片；其他格式不保证）。
- 附件的这些路径只有离线测试，没有用真 droid 走过（实机回合用完了）：超过 5 MiB 的图片压缩后再发送、回合运行中排队、发送失败后消息和附件放回输入框、模型不支持图片时的拦截、droid 拒绝附件时的错误文字（文字来自 SDK 源码里的报错，不是在实机上见到的）。
- PDF 的文字由 Qt 的 `QtPdf` 提取；没有文字层的扫描件只发 PDF 本身。实机里 haiku 5.5 答对了 PDF 里的金额，但分不清它读的是 PDF 本身还是我们附上的 `parsed_data`，所以「没有原生 PDF 支持的模型也能读」没有验证。
- 附件的卡片和缩略图没有在深色主题下、125% 之外的显示缩放下看过；卡片图标的颜色是固定的。
- 读文件和压缩图片在界面线程里同步完成，没有测过很大的图片（几十 MB、上万像素）会让窗口停多久。
- 附件只在这一台机器、droid 0.251.0 上验证过。
- 上下文显示在真实压缩下的表现只有离线测试：实机的 2 个回合没有触发自动压缩（收到压缩通知后「已用」立刻变 `—`、压缩前的旧数不被当成新数，这些用的是假 Session）。「换模型后分母跟着变」实机见过一次，但那是 droid 自己替换模型（haiku → kimi-k3），不是用户手动切换。
- 改名的迁移只用临时文件夹测过：没有动过真实的 `%APPDATA%\droid-desk\`，所以「真实旧设置在第一次启动时被复制」这一下没有实机走过（逻辑同一条代码路径）。

**已知的行为差异**
- 恢复出来的历史看不出某一轮曾被「停止」，也不单独显示 AskUser 的问答块（问答在「提问」工具行里能看到）。
- 打开后从没发过消息的会话，也会留在 Droid 的会话列表里，侧边栏里显示为「New Session」。
- 状态栏里的「积分」是 Droid 上报的 `factory_credits` 原始值，单位没有文档说明（Phase 0 就记过）。
- 重连只能恢复会话本身，不能接上断线时正在跑的那一轮。
- 已断开的会话不占会话名额，也不会被自动关掉；点「重新连接」不检查上限，所以重新连上几个已断开的会话，可能暂时超过 `max_open_sessions`。
- 停止超时换进程之后，这一轮里 droid 已经做完的操作不会被撤销，只是这一轮被标为已中断。
- `core/winproc.py` 依赖 SDK 里 `droid_sdk.transport` 使用 `asyncio` 这个名字；SDK 升级后如果不再这样，应用只记一条警告，不带控制台启动时会回到闪窗口的行为。
- 日志文件不做轮转（默认只记警告和错误，增长很慢）。
- 模型列表只在启动时读取一次，更新要重启。
- 模型写在 Markdown 里的图片只显示占位文字（你自己的附件缩略图例外，页面的 CSP 只为它放行 `data:` 图片）。
- 对话里（实时和历史）PDF 和文本附件只是写着名字和大小的卡片，不保留内容、不能预览；历史里图片的大小是 droid 重新编码之后的，和发送时不同。
- 输入框里还没发送的附件和草稿一样只存在内存里，关掉标签页就丢；每个会话各有一份。
- 文件夹、网页上的图片链接不能当附件（文件夹会说明原因并给出路径）。
- 上下文显示依赖 SDK 私有 API（`Session._require_client().get_context_breakdown()`），SDK 锁 0.5.0；这个门没了的时候只剩窗口一个数（`test_the_sdk_still_offers_what_the_context_reading_relies_on` 会在升级时先发现）。
- 刚压缩过、还没有新的模型调用时，上下文的「已用」显示 `—`（Droid 在一小段时间里还可能报压缩前的旧数，应用把它当作未知）。
- droid 可能把回合交给另一个模型服务而不报错（实机见到 claude-haiku-5-5 → kimi-k3，2026-10-29）；模型下拉框和上下文窗口按 Droid 报告的实际模型显示。

**还没有的功能**
- 斜杠命令、MCP 管理、Spec 模式下编辑规格、设置对话框（droid 路径和会话上限现在只能改 `settings.json`）、会话搜索 / 重命名 / 删除、导出、系统通知、打包成安装程序。

## 7. 下一步

1. 把驱动脚本里验证过的路径做成可选的回归检查（`live_check.py --stages` 已经分阶段，也能单独跑）。
2. 历史视图补全：被停止的轮、AskUser 问答块；隐藏从没发过消息的会话。
3. 设置对话框（droid 路径、会话上限、默认模型、主题）和会话搜索、重命名。
4. 多会话并行时的系统通知（谁在等你确认、谁跑完了）。
5. 深色主题、其他缩放比例的核对；用一个真的卡住的 droid（而不是丢掉停止请求）再验证一次换进程；SDK 一旦提供 `creationflags` 选项，就去掉 `core/winproc.py`。
6. 用长会话夹具（`fixtures/long_session`）做一次界面性能基线，再决定要不要做虚拟列表。
7. 打包与自动更新。
8. 附件：在资源管理器里真的拖一次、`Ctrl+V` 贴一次截图，人工核对；用一个实机回合验证超大图片压缩后的发送；深色主题截图；PDF 没有原生支持时 `parsed_data` 是否被用上。