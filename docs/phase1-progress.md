# WittyCode Phase 1 进度记录

> 本文件在每个里程碑后增量更新，用于防止工作丢失。最终说明见 `docs/phase1.md`。

## 里程碑 0：调研与依赖（已完成）

- 读完 Phase 0/0b 结论文档、spike 脚本、SDK 0.5.0 源码（Session / RunStream / InteractionDispatcher / ProcessTransport / discovery / low_level.load_session）。
- 关键补充发现（文档里没有写到的）：
  - `ToolProgress.update.full_output` 是**累计的完整输出**（`content`/`text` 才是 2 行滚动尾部），进度面板可以直接显示 `full_output` 的尾部。
  - 隐藏消息前缀实际为 `todo-suggest-...`（不是 `todo-sug-`），过滤时用前缀 `todo-sug` 匹配。
  - 权限回调 / 提问回调由 SDK 协议层以后台 task 运行；`client.close()` 会取消这些 task，所以 UI 的等待 future 会收到 `CancelledError`。
  - `RunStream` 被取消时会自动发送 interrupt（`_release(interrupt=True)`），所以「先取消消费 task 再 close」是安全的。
  - `PermissionRequest` 没有 request_id，`PermissionResolved` 的 `tool_use_ids` 可能为空 → 只能按 tool_use_id 交集或「当前所有待审批」来匹配。
  - SDK 构造 `ProcessTransport` 时 cwd 显式传入，`Session.resume()` 的 cwd 来自 `Path.cwd()`（因此按要求用 asyncio.Lock + os.chdir 串行化 resume）。
- 新增依赖（装在 .venv）：markdown-it-py 4.2.0（含 mdurl 0.1.2）、Pygments 2.21.0、pytest 9.2.0（含 pluggy 1.7.0、packaging 26.3、iniconfig 2.3.1、colorama 0.4.6）。
## 里程碑 1：接手与核心复查（已完成）

- 接手时磁盘状态：`desktop/core/*`、`devtools/replay.py` 已写好，`desktop/ui`、`web/`、`tests/` 不存在；`__main__.py` 引用了尚未创建的 `desktop.ui.app.main`。
- 复查结论（用 `fixtures/*.jsonl` 逐个回放进 `Transcript` 验证，输出符合预期）：stream_turn / ask_user / interrupt / interrupt_running_tool / errors_handlers / multiedit / hooks / long_waits 的归约结果都正确；`history.json` 经 `FactoryDroidMessage.model_validate` 后可直接喂 `load_history`。
- 发现并要修的问题：
  1. `SessionController._consume` 在 `RunFailure` 时会重复追加错误块（归约器已经追加过）。
  2. `reconnect()` 会再次 `load_history`，历史轮会重复插入。
  3. `close()` 与 `_open()` 竞态：打开过程中关闭，之后 `_open` 仍把控制器置为 READY（进程泄漏）。
  4. 历史加载与 resume 并发启动两个 droid 进程；改为先加载历史（并关闭一次性进程），再 resume，避免两个进程同时持有同一会话。
  5. `DroidConnectionError`（"Transport not connected"）也应当视为连接已断开，走「重新连接」。
  6. 死亡会话的旧 `Session` 对象没有关闭。
- 设计决定（UI）：转写页由 Python 渲染 HTML 片段（markdown-it + Pygments），JS 只做补丁应用/折叠/表单；展开状态保存在 Python 侧，工具详情懒渲染；每个活动组默认折叠成一行 `N 步 · 读取 x · 编辑 y`，回合结束后在末尾给出 `用时 … · N 步`，助手文字始终完整可见。
- 已修复（上面 1-6 全部；另外）：`session_manager.py` 重写——`RunFailure` 不再重复报错；重连不再重载历史；`close()` 与 `_open()` 竞态用 `_close_requested` + `_opening` future 解决（close 等打开完成再释放进程）；先加载历史（一次性进程）再 resume；`DroidConnectionError`/`SessionClosedError`/`SessionNotOpenError` 也进入 DEAD；死亡的旧 Session 在后台关闭；模型列表缓存；`controller.usage` / `live_settings()`；多选答案（`answer_question` 接受 `str | list[str]`）；停止按钮的看门狗（8 秒没有终止事件则取消消费 task）；`close_all()` 并发关闭并带超时。
- 新增 `tests/`：`support.py`（夹具回放与合成事件构造）、`fakes.py`（可编排的假 Session / 假工厂）、`test_locator/settings/timefmt/toolinfo/diffs/prompts/reducer_fixtures/reducer_synthetic/history/session_manager.py`。`pytest tests` = 207 passed（约 1.3 秒，无真实模型调用）。

## 里程碑 2：渲染层与网页（已完成，Qt 之外的部分）

- `desktop/render/markdown.py`：markdown-it-py（关闭原始 HTML）+ Pygments（`tok-` 类名，无内联样式）；只保留 http/https/mailto 链接，图片一律变成占位文字；表格列对齐改成 class（CSP 会拒绝内联 style）。深色代码配色挂在 `html[data-theme="dark"]` 下，页面主题由 Qt 调色板显式设置，不依赖 `prefers-color-scheme`（避免原生控件与网页不一致）。
- `desktop/render/fragments.py`：纯字符串 HTML 片段（用户消息、助手文字、活动组、工具行与详情、diff、思考、权限卡片、AskUser 表单、页脚、错误/提示）。所有动态文本经过 `escape` 或 markdown 渲染；没有内联 style / 事件处理器，行为全部挂在 `data-act` 上。
- `desktop/render/presenter.py`：`Transcript` → 页面补丁。靠节点 `version` 与展开状态签名决定重发什么；流式 token 只重发对应文本块和页脚；`kids` 补丁让页面按 id 对齐子节点；历史按 40 轮分页（「显示更早的 N 轮」）；`encode_ops` 把大补丁切成约 250 KB 的块。展开状态（组 / 工具 / 完整输出）保存在 Python 侧，所以重发或切换会话后仍然保留。
- `desktop/render/page.py` + `desktop/web/{index.html,app.css,app.js}`：离线单页，CSS / QWebChannel 客户端 / 页面脚本全部内联，CSP 只允许带 nonce 的内联块（`default-src 'none'`）。JS 只做补丁应用、点击分发、AskUser 表单收集、复制代码、回到最新；页面与 Python 不一致时（保留节点缺失）会请求整页重发。
- 测试：新增 `test_markdown.py`、`test_fragments.py`（含对全部录制夹具、全部展开状态的「标签/属性白名单」结构检查）、`test_presenter.py`、`test_page.py`。`pytest tests` = 290 passed（无真实模型调用）。

## 里程碑 3：Qt 界面与窗口测试（已完成）

- `desktop/ui/`：`app.py`（命令行、QApplication、qasync 事件循环、关闭收尾）、`main_window.py`（侧边栏 | 标签页 + 转写页 + 输入框；工具栏；状态栏；菜单与快捷键）、`transcript_view.py`（QWebEngineView + QWebChannel，共享 profile，节流刷新）、`composer.py`、`sidebar.py`、`settings_bar.py`、`session_tabs.py`、`status_line.py`、`theme.py`、`tasks.py`。
- 离线回放模式 `python -m desktop --replay fixtures/stream_turn.jsonl --speed 20` 端到端验证过：新会话、Enter 发送、停止按钮、两个权限卡片经页面点击批准、状态栏「等待你的确认」、最终用量显示、`window.grab()` 截图、干净退出。
- 联调中发现并修复的问题：
  1. 第一个标签页从不触发「已选中」（`addTab` 先把它设为当前，此时控制器还没挂上），导致页面栈不切换、视图一直不可见 → `SessionTabs.add_controller` 静默添加后再统一发出选中信号。
  2. 输入框「能否发送」被状态栏的 120 ms 合并刷新拖后 → 当前会话的输入框与菜单动作同步刷新，其余界面仍然合并刷新。
  3. 工具栏选择「自主级别 = HIGH」「模式 = Spec」时静默无效：`_apply_setting` 里的字典一次性对所有字段做枚举转换，`Mode("high")` 抛出 ValueError 被吞掉 → 改为按字段惰性转换。
  4. 关掉最后一个标签页后，输入框与状态栏要等 120 ms 才恢复成「没有会话」→ 立即刷新。
  5. 打开会话失败（进程起不来 / 打开过程被取消）会泄漏一个控制器和一个会话名额 → `SessionController._abandon` + `SessionManager._opened`。
  6. 监听器抛异常会破坏会话 → 控制器与管理器的 `_notify` 捕获并记录。
  7. `format_credits`：几个积分的小额不再四舍五入成整数。
- 测试环境教训：在协程里调用 `QApplication.processEvents()` 会在嵌套事件循环中重入另一个任务的第一步，asyncio 会拒绝，那个任务从此既不运行也无法取消，拖死 `loop` fixture 的收尾。测试改用 `await asyncio.sleep()` 让 Qt 处理延迟删除，`conftest.loop` 对取消加了 5 秒上限。
- 新增 `tests/test_ui_transcript.py`（真实网页）、`tests/test_ui_widgets.py`、`tests/test_ui_window.py`（22 个：侧边栏列表、新建/发送/停止、恢复历史且不重复打开、打开失败、死亡与重连、会话上限与 LRU 淘汰、关闭标签与空页面、草稿、工具栏、状态栏用量、待确认标记、项目增删与持久化、关闭窗口确认、关机收尾）。`pytest tests` = 342 passed（连续 3 次，约 10 秒，无真实模型调用）。
## 里程碑 4：驱动脚本与实机验证（已完成）

- `desktop/devtools/driver.py`（`WindowDriver`）：像用户一样操作真窗口——往输入框打字并按 Enter、点击页面里的权限卡片按钮和 AskUser 表单、点击侧边栏的会话行、按「停止」、截图。截图用 `window.grab()`；页面区域像空白时改取屏幕像素，仍然空白就拒绝保存。`approve_until_done` 就是「脚本化的权限批准钩子」。`max_turns` 是发送回合数的硬上限。`devtools/processes.py` 用 Toolhelp32 进程表判断哪些 droid.exe 属于本进程，只按父子关系，不按命令行匹配。
- `scripts/live_check.py`：真窗口（不是离屏）+ 真 droid.exe；模型 `claude-haiku-5-5`，工作目录 `sandbox/phase1/demo`，临时设置文件，自主级别 off。按阶段运行（`--stages 0..5`），回合数和会话 id 记在 `sandbox/phase1/live_state.json`，所以「最多 6 个实机回合」的上限跨多次运行都有效；每个阶段后写 `live_report.json`，输出追加到 `live_log.txt`。
- 离线先行：driver 的每个动作先在 `tests/test_driver.py`（ReplayFactory / FakeFactory，不调用模型）里跑通，再用于实机。
- 实机运行记录（`sandbox/phase1/live_log.txt`；**共 6 个实机回合，上限 6，已用完**）：

  | 运行 | 阶段 | 实机回合 | 结果 |
  | --- | --- | --- | --- |
  | 1 | 0（只启动和关闭） | 0 | 9 项通过；但「模型列表已加载」的等待条件恒为真（见下） |
  | 2 | 0 | 0 | 9 项通过：69 个模型约 4 秒加载完，新会话约 9 秒就绪，关窗后没有残留 droid.exe |
  | 3 | 1 多工具回合 | 1 | 15 项通过：创建、编辑两个权限卡片经页面点击批准，`hello.txt` 内容为 `hi there`，4 步折叠成 2 组，展开的编辑行显示 diff |
  | 4 | 2、3 提问与打断 | 2 | 14 项通过，**1 项失败**（见下） |
  | 5 | 4 恢复 | 1 | 14 项通过：冷恢复、关标签（droid 进程消失）、再点侧边栏恢复、历史 3 轮、追问答出 `hi there` |
  | 6 | 5 打断运行中的命令 | 1 | 12 项通过：1.8 秒回到就绪，droid 在 3 秒内结束了它的子进程树 |
  | 7 | 3（检查改动后重跑） | 1 | 12 项通过，0 项失败：打断时提示状态为 `resolved`、卡片消失，0.12 秒回到就绪；关窗后没有残留 droid.exe，桌面版的 droid 进程没被动 |

- 截图（`docs/screenshots/`，都用 `window.grab()` 从真窗口取得，已逐张查看）：`phase1-permission-pending.png`（待确认的创建文件卡片）、`phase1-turn-summary.png`（折叠摘要 + 可见回答 + 用时脚注）、`phase1-tool-diff.png`（展开的工具组和 diff）；另有 `phase1-permission-pending-2.png`、`phase1-askuser.png`、`phase1-interrupted.png`、`phase1-running.png`、`phase1-stopped.png`、`phase1-resumed.png`。其中 `phase1-interrupted.png` 是运行 7 重新截的（上面是恢复出来的历史，最底下是刚被停止的回合，页脚标着「已停止」），其余来自运行 3 到 6。
- 实机联调中发现并处理的问题：
  1. 阶段 0 的「模型列表已加载」等待条件恒为真：列表到达之前，工具栏就已经显示「当前模型」这一项。改为等 `SettingsBar.has_models()`（新增的小方法）。
  2. 回合刚结束就读页脚，读到的还是「正在生成回复…」（页面落后一个刷新周期）。脚本改为等页面出现非 live 的页脚再读；截图本身有 0.5 秒的等待，所以截图是对的。
  3. 阶段 3 的检查期望被打断时的权限提示变成 `expired`，实际是 Droid 自己先结算了它（`resolved`），卡片同样消失。检查改为「已结算且卡片消失」，两种状态都接受。改动后用最后一个实机回合重跑了阶段 3（运行 7）：12 项通过，0 项失败，提示状态为 `resolved`。
  4. `QWidget.grab()` 在真窗口里能抓到 WebEngine 的内容：运行 6 的报告里，启动、运行中、停止后三张截图的 `method` 都是 `widget`，页面区域有 78–92 种颜色（要求至少 4 种）；运行 7 的报告（`sandbox/phase1/runs/20261029-012822.json`）里启动和打断后两张也是 `widget`，分别有 79 和 82 种颜色。运行 6 及更早的报告被后一次覆盖了，没有留下 `method` 记录（之后每次运行另存一份到 `sandbox/phase1/runs/`）。屏幕像素的回退没有观察到被实机触发，只有 `tests/test_driver.py` 里的测试覆盖它。
- 实机观察（写进限制与下一步）：
  - 打断时 Droid 自己会结算待确认的提示，随后回合以「已中断」结束；停止等待确认中的回合 0.1 秒就回到就绪，停止正在运行的命令 1.8 秒。
  - 被停止的命令：powershell / conhost / python 子进程在 3 秒内消失，不会留下孤儿进程。
  - 历史视图（恢复会话）看不出某一轮是被「停止」的，也不显示 AskUser 的问答块（只在「提问」工具行里）。
  - 打开后从没发过消息的会话也会留在 Droid 的会话列表里，侧边栏里显示为「New Session」。
  - 状态栏的「积分」是 Droid 上报的 `factory_credits` 原始值，单位没有文档说明（Phase 0 就记过）。
- 测试：新增 `tests/test_driver.py`（10 个，含截图回退与拒绝空白截图）。

## 里程碑 5：代码评审修复（已完成）

评审提出 4 个问题（第 4 个有两处），逐个修复，每个都配了会在旧逻辑上失败的测试。`pytest tests -q`：352 → **404 passed**（连续 3 次，约 19 秒；新增 53 个，换掉 1 个过时的：`test_stuck_interrupt_falls_back_to_cancelling_the_turn` 断言的正是旧的「只取消消费 task」行为）。另外用了 **3 个实机回合**（模型 `claude-haiku-5-5`，上限 3，已用完）；它们单独计数，记在 `sandbox/phase1/noconsole/state.json`，和里程碑 4 的 6 个无关。保留工具调用的显示方式不变：每个活动组仍折叠成自己的一行，组之间是助手文字，回合末尾是用时和步数。

### 修复 1：没有控制台时 droid.exe 闪出控制台窗口

- **原因**：SDK 里只有 `ProcessTransport.connect` 会启动 droid，它调用 `asyncio.create_subprocess_exec` 而不传 `creationflags`。`pythonw.exe`（或指向 `.pyw` 的快捷方式）没有控制台，Windows 就为控制台程序 droid.exe 新开一个控制台窗口，droid 为 Execute 工具启动的 PowerShell 共用它。
- **改动**：
  - `desktop/core/winproc.py`（新）：`install()` 把 `droid_sdk.transport` 里的名字 `asyncio` 换成替身，替身只改 `create_subprocess_exec`，在调用方没给 `creationflags` 时补上 `CREATE_NO_WINDOW`（给了就照用，哪怕是 0）；只在 Windows 生效，可重复调用；SDK 不再按预期使用 `asyncio` 时不动手并记一条警告。不用 `DETACHED_PROCESS`：那样 droid 自己没有控制台，它启动的每个控制台程序反而各开一个窗口。
  - `core/session_manager.py`：`DroidFactory.__init__` 调用 `winproc.install()`，新建、恢复、读历史、读模型列表四条路径都经过它。
  - `ui/app.py`：新增 `apply_settings_option()`（`--settings` 在配置日志之前生效）和 `log_file_path()`；没有 `stderr` 时日志写到设置文件旁边的 `droid-desk.log`，文件建不出来也不影响启动；没有控制台时，启动期的错误（如找不到回放文件）写进日志。
  - `droid-desk.pyw`（新）：快捷方式的目标，用 `.venv\Scripts\pythonw.exe` 运行；启动失败时用消息框说明（版本守卫的文字或异常回溯）再退出。
  - README 和 `docs/phase1.md` 去掉了「pythonw 下没验证过」的说明，补上启动方式和快捷方式的写法。
- **自动测试**（`tests/test_no_console.py`，17 个）：补丁只在没给 `creationflags` 时加标志（显式给的 `0`、`0x200`、`0x08000200` 都原样保留）；别处的 `asyncio` 属性和普通的 `asyncio.create_subprocess_exec` 不受影响；重复安装、非 Windows、SDK 变了的处理；四条启动路径都带标志；用真的 `pythonw.exe` 子进程对照（不打补丁的子进程有控制台窗口，打补丁后没有）；没有控制台时日志写进文件、文件建不出来也能启动；`pythonw -m desktop` 与 `droid-desk.pyw` 带不存在的 `--replay` 文件时退出码 2、原因写进 `droid-desk.log`。
- **验证**：
  - 把 `winproc.install` 改成什么都不做（`sandbox/fixes/disable_winproc.py`）后，其中 7 个测试失败。
  - 对照用的子进程（不打补丁，用 `SW_HIDE` 启动，免得跑测试时屏幕上闪窗口）确实有控制台窗口句柄；打补丁后的子进程没有。跑这些测试期间没有出现新的控制台窗口（`sandbox/fixes/check_test_flash.py`：0 个）。
  - `droid-desk.pyw` 的失败路径：`pythonw -S droid-desk.pyw`（没有 site-packages）弹出消息框，内容含 `ModuleNotFoundError: No module named 'PySide6'`，退出码 1（`sandbox/fixes/check_pyw_failure.py`）。
  - 实机：见下面的「实机运行」。

### 修复 2：停止超时之后状态不对

- **原因**：旧的 `interrupt()` 在 8 秒内等不到终止事件，就取消消费 task、把这一轮标为已取消。SDK 随即认为会话空闲，而 droid 可能还在跑这一轮，用户的下一条消息就会发给一个仍然忙着的进程。`session.interrupt()` 本身也没有超时：droid 不答复停止请求时，这一步会一直挂着。
- **改动**（`core/session_manager.py`、`core/transcript.py`）：
  - `interrupt()`：停止请求加等待结束共用一个 8 秒的期限（`INTERRUPT_GRACE_SECONDS`）。超时（`asyncio.TimeoutError` / `RunTimeoutError`）或答复了却没结束这一轮 → `_restart_connection()`；连接已断 → 已断开；其他 `DroidError` → `Transcript.clear_stopping()`（新增）并写「停止失败：…」，会话仍在运行，停止按钮恢复可用。
  - `_restart_connection()`：**同步**把状态改成 `OPENING`（消费 task 的完成回调只会把 `RUNNING` 改回 `READY`，所以从卡住的进程到新进程之间任何时刻都不是 `READY`），再在自己的 task 里（`asyncio.shield`，调用方被取消也不会留下半途状态）取消消费 task、让待确认过期、关闭旧 session（最多 15 秒，`RESTART_CLOSE_SECONDS`）、用同一个会话 id `open_resume(load_history=False)`；成功后写「停止超时，已重启会话连接」。
  - 旧进程 15 秒内没关掉 → `_give_up()`：已断开、可重连，不去 resume（避免两个 droid 同时持有这个会话）；`reconnect()` 会先等旧进程关完。resume 失败 → 已断开，错误以「停止超时，重启会话连接失败：」开头。
- **自动测试**：`tests/test_session_manager.py` 11 个（换进程而不是提供一个可能仍忙的会话；resume 失败留下已断开且能重连的会话；待确认被结算、没有处理函数残留；droid 不答复停止请求，两种形式；别的错误不改变状态且可重试；旧进程退出之后才 resume；旧进程没及时退出则已断开、重连时等它；换进程途中关闭不留进程；替换进程还在启动时关闭；调用方被取消不丢弃换进程）和 `tests/test_ui_window.py` 2 个（整窗口：换进程期间界面从不让人给一个可能仍忙的会话发消息；resume 失败留下已断开的标签和「重新连接」）。竞态窗口用假 Session 已有的 `close_delay`（关闭时的延迟）拉宽。
- **验证**：
  - 在旧逻辑上跑新测试（旧 `interrupt()` 在 `sandbox/fixes/check_old_2.py` 里重建）：11 个中 10 个失败或挂起（`never answers` 挂住），只有「待确认被结算」一个在旧逻辑上也能过，它是回归保护。
  - 实机（`--stages 2,3`，见下）：droid 收不到停止请求时，8 秒后应用日志出现 `droid did not stop within 8 s; replacing its process`，状态 `running → opening → ready`；旧 droid.exe（pid 32000）连同它的 powershell、conhost 一起消失，新 droid.exe 是 10372；从点停止到回到就绪 12.7 秒；对话里有「本轮已取消。」和「停止超时，已重启会话连接」；重启后的会话答出了重启前请求的命令。

### 修复 3：会话上限的竞态与无声的淘汰

- **原因**：`_register` 先数已打开的会话，再（可能要很久地）关闭一个空闲的，最后才登记，数和登记之间没有互斥。两个同时发起的打开会看到同一份已满的列表、选中同一个受害者、都登记，上限被超过；同一个已保存会话被同时打开两次时会起两个 droid。淘汰本身也没有明确的提示：界面只能在会话关闭之后推断「这是自动关闭的」。
- **改动**：
  - `core/session_manager.py`：`SessionManager._register_lock`（一个 `asyncio.Lock`）串行化登记；`_register()` 返回实际占着名额的控制器（`open_saved` 的重复打开经 `find()` 共用同一个）；新控制器以 `OPENING` 进入列表，下一个才开始数，正在打开的也算占名额；循环：有空位就登记；已经有一个被用户点了关闭、还没关完的 → 等它关完再数；否则淘汰最久没用的 **`READY`** 会话；没有可淘汰的就抛 `SessionLimitError`。正在运行、等待确认、正在打开的会话从不被淘汰；**已断开的会话不再被自动淘汰**（它在等用户点「重新连接」）。`_evict()` 先设置 `eviction_notice`、记日志、发 `evicted` 事件，再关闭受害者；`eviction_message()` 点名会话，没发过消息的会话不承诺能重新打开。
  - `ui/main_window.py`：收到 `evicted` 事件就在状态栏显示这条提示 12 秒（`EVICTION_NOTICE_MS`）；删掉 `_on_closed` 里推断出来的提示，所以用户自己关的标签不会被说成「自动关闭」。
- **自动测试**：`tests/test_session_manager.py` 7 个（淘汰有提示并点名；没发过消息的会话不承诺能重新打开；忙的会话从不被淘汰、也没有提示；两个并发打开不超限；并发打开各自淘汰自己的空闲会话；用户正在关的会话让出名额而不是另外淘汰一个；同一个已保存会话的并发打开共用一个控制器）和 `tests/test_ui_window.py` 2 个（状态栏点名被淘汰的会话并说明去哪里重新打开；自己关标签不会说成自动关闭）。
- **验证**：旧 `_register` 在 `sandbox/fixes/check_old_3.py` 里重建，新测试中的 5 个在旧逻辑上失败（淘汰提示、两个并发打开不超限、各自淘汰自己的、让出名额、同一个会话共用控制器）。没有做实机验证（上限和淘汰只涉及登记名额的逻辑，用假 Session 并发地覆盖）。

### 修复 4a：过期的权限/提问不再用异常结束

- **原因**：`_expire_prompt`（回合结束、会话关闭、进程死亡、换进程时调用）对还在等的处理函数 `future.set_exception(CancelledError())`。处理函数因此抛出 `CancelledError`，SDK 的适配层和协议层都把它当作任务被取消（它不是 `Exception`，不会被 `except Exception` 接住，也不会走「回一个取消」的回退），于是 droid 那条待答的请求得不到任何回应；正常返回的 `CancelPermission` / `CancelQuestion` 才会作为响应发给 droid。
- **改动**（`core/session_manager.py`）：模块函数 `_cancel_response(request)`（提问返回 `CancelQuestion`，权限返回 `CancelPermission`）；`_expire_prompt` 改成 `future.set_result(_cancel_response(request))`，`_settle_expired` 复用同一个函数。处理函数被 SDK 自己取消时仍然把卡片标为已过期并继续往上抛取消。
- **自动测试**（4 个）：`test_expiring_a_pending_prompt_returns_a_cancel_answer_instead_of_raising[permission|question]`，`test_session_close_and_process_death_answer_open_prompts_with_a_cancel[close|death]`。
- **验证**：旧的 `_expire_prompt` 在 `sandbox/fixes/check_old_4a.py` 里重建，4 个都以 `CancelledError` 失败；新代码上全部通过。

### 修复 4b：含被取消工具的活动组显示 ✓

- **原因**：活动组的图标只区分「运行中 / 等待确认 / 有失败 / 其余」，有被取消工具的组落到了 `✓`，看上去像是都成功了。
- **改动**：`render/fragments.py::activity_html`：有被取消的工具时加 class `has-cancel`，图标按最坏结果取：`wait > spin > warn（有失败）> cancel（有取消）> ok`；`web/app.css`：`.gicon.cancel::before { content: "\2212" }`（琥珀色 `−`）、`.activity.has-cancel .gtext` 用警告色，且排在 `.has-error` 之前，所以失败仍然盖过取消。摘要里本来就写「N 个已取消」，没有改。
- **自动测试**（10 个）：`tests/test_fragments.py` 9 个（只有全部完成才是 `✓`；有取消的组得到取消图标和摘要；失败优先于取消；运行中和等待的组保持原来的标记；`test_every_group_icon_has_a_style_rule[ok|warn|cancel|wait|spin]` 保证每个图标都有样式规则）；`tests/test_ui_transcript.py` 1 个，在真实的 Qt WebEngine 页面里量：含被取消工具的组，`::before` 的内容是 `−` 而不是 `✓`，颜色也不同。
- **验证**：上面的测试。没有重新截图（`docs/screenshots/` 里的图是修复之前的）。

### 实机运行（`scripts/live_noconsole_check.py`，全程 `pythonw.exe`）

进程没有控制台（`stdout`、`stderr` 为 `None`，控制台窗口句柄为 0）；真窗口 + 真 droid.exe；监视线程每秒约 20 次枚举可见窗口和本进程之下的进程，凡是 droid.exe、控制台宿主或 droid 子进程的可见窗口，以及启动前不存在的控制台窗口（不论属于谁）都算违规。结果详见 `docs/phase1.md` 第 5 节；这里列流水：

| 运行 | 内容 | 实机回合 | 结果 |
| --- | --- | --- | --- |
| 1 | 探针（`sandbox/fixes/probe_parent.py`，不是验证脚本）：pythonw 下启动一个控制台子进程，分别用未打补丁的 SDK、打补丁的 SDK、`SW_HIDE` 对照 | 0 | 未打补丁的子进程有控制台窗口句柄；打补丁后句柄为 0；对照（`SW_HIDE`）有句柄 |
| 2 | `--stages 0 --control`（补丁关掉） | 0 | 看到问题：启动后不到 1 秒（读模型列表的 droid）和 3 到 4 秒（会话的 droid）各出现一个整屏大小的 Windows Terminal 窗口（`CASCADIA_HOSTING_WINDOW_CLASS`，1752×936），droid.exe 另有 0×0 的 `PseudoConsoleWindow`。窗口属于 `WindowsTerminal.exe`，不是 droid.exe，所以监视器不能只看 droid 自己的窗口，要看所有新出现的控制台窗口 |
| 3 | `--stages 0 --probe-stop` | 0 | 空闲 droid 0.00 秒答复停止请求；被丢掉的请求从不被答复，所以阶段 2 里 droid 对停止确实没有反应 |
| 4 | `--stages 0,1` | 1 | `Get-Date; Start-Sleep -Seconds 4`，批准 1 次权限，回复含 `29 October 2026 12:36:31`；422 次轮询、8874 次窗口检查、0 次违规；见过 droid.exe、conhost.exe、powershell.exe、git.exe，都没有可见窗口 |
| 5 | `--stages 2,3` | 2 | 25 项检查全部通过：换进程（见修复 2）、追问答出重启前的命令、865 次轮询、18129 次窗口检查、0 次违规；关窗后没有残留的 droid.exe，桌面版的 4 个 droid 进程没被动 |
| 6、7、8 | 补跑三次不发消息的运行，把报告留在磁盘上：`--stages 0 --control`、`--stages 0`、`--stages 0 --probe-stop` | 0 | 全部通过；对照里再次看到整屏的 Windows Terminal 窗口（0.73 秒和 3.16 秒），其余两次 0 次违规 |

- 运行 2 到 5 的报告，除运行 5 外都被下一次运行覆盖了（`report.json` 只保留最近一次）；运行 4 的数字是跑完时读到的。运行 5 的报告、过程日志和应用日志留在 `sandbox/phase1/noconsole/runs/20261029-123752-stages-2-3.*`，运行 6 到 8 的报告也在 `runs/` 里。之后脚本每次跑完都会另存一份（和里程碑 4 的教训一样）。
- 「droid 卡住」是模拟的：脚本在 transport 里丢掉 `droid.interrupt_session` 请求（共丢了 2 个），没法让真的 droid 自然卡住。
- 3 个实机回合：运行 4 一个，运行 5 两个。`sandbox/phase1/noconsole/state.json` 记着 `turns_used: 3`。
- 回合预算的保护：用完之后再跑 `--stages 1`，脚本以退出码 2 退出，报告里是「enough live turns are left: 0 left, 1 needed; nothing was started」，没有启动 droid（运行前后机器上的 droid 进程相同），`turns_used` 仍是 3（`runs/20261029-125942-refused.json`）。

### 过程中发现的问题

1. 默认终端是 Windows Terminal 时，没有补丁的窗口属于 `WindowsTerminal.exe`（`CASCADIA_HOSTING_WINDOW_CLASS`），不是 droid.exe，也不是 `ConsoleWindowClass`。监视器因此同时认这两种窗口类，并且不只看 droid 自己的窗口。
2. 脚本的 `report.json` 每次运行都被覆盖，早先运行的结果因此没有留下。已改成每次另存一份到 `runs/`，并补跑了不发消息的几次，把对照和空闲运行的报告留在磁盘上。
3. 已断开的会话不占会话名额，点「重新连接」也不检查上限（用假 Session 验证过：上限 1，一个会话断开后再开一个，重连前者就是 2 个已打开）。这不在评审范围内，也不是这次改出来的，没有处理，记在 `docs/phase1.md` 的已知限制里。
4. `.pyw` 双击会用 Windows 关联的解释器，通常没有 `.venv` 里的包；所以文档里要求快捷方式的目标写成 `<项目>\.venv\Scripts\pythonw.exe <项目>\WittyCode.pyw`。

### 没有验证 / 新增的风险

- 换进程的流程在真 droid 上只用「丢掉停止请求」模拟过，没有遇到自然卡住的 droid；旧进程 15 秒内关不掉的那一支只有假 Session 的测试。
- 没有窗口闪现只在这一台机器上验证过，它的默认终端是 Windows Terminal；传统控制台宿主没有试，监视器认 `ConsoleWindowClass`，但没有在那样的机器上跑过。
- `winproc` 依赖 `droid_sdk.transport` 里使用 `asyncio` 这个名字；SDK 升级后若不再这样，补丁会记一条警告并不动手，不带控制台的启动方式就会重新闪窗口。
- 已断开的会话不再被自动淘汰（行为改动）。已断开的会话本来就不占名额，所以重连可以让打开的会话数暂时超过上限（见上，没有处理）。
- 修复 3（会话上限）没有做实机验证，只用假 Session 并发地覆盖。
- `interrupt()` 遇到超时以外的 `DroidError` 时，现在会撤销「正在停止」并写「停止失败：…」，之前这种情况不处理（新增行为，有测试）。
- 取消图标没有重新截图，只有页面里算出来的样式的测试。
- `tests/test_no_console.py` 里有几个测试要真的启动 `pythonw.exe`，不是 Windows 或没有 `pythonw.exe` 时跳过。

## 里程碑 6：附件（已完成）

用户反馈：「不能粘贴图片，也不能附加本地文件」。做的是：输入框可以 `Ctrl+V` 粘贴截图和文件、点 📎（提示「附加文件」）选文件、把文件拖到输入框或对话区；图片、PDF、文本作为附件发给模型；对话和恢复出来的历史里显示缩略图和文件卡片；CSP 只多放开 `img-src data:`。保持工具调用的显示方式不变。`pytest tests -q`：404 → **588 passed**（连续 3 次，约 33 秒；新增 184 个）。另外用了 **3 个实机回合**（`claude-haiku-5-5`，上限 3，已用完；单独计数，记在 `sandbox/phase1/live_attach_state.json`，和前面几组的回合无关）。

### 做了什么

核心（不依赖 Qt）：
- `core/attachments.py`（新）：`Attachment`（`kind` 是 `image` / `pdf` / `text`；`slim()` 是对话里保留的精简副本）、`Rejection`、`Thumbnail`；限制常量（`MAX_ATTACHMENTS=10`、`MAX_TOTAL_BYTES=20 MiB`、`MAX_IMAGE_BYTES=5 MiB` 和 `MAX_PDF_BYTES=3 MiB`（取自 SDK 的 `MAX_ATTACHMENT_BYTES`、`MAX_PDF_ATTACHMENT_BYTES`）、`MAX_TEXT_BYTES=1 MiB`、`MAX_SOURCE_BYTES=64 MiB`、`MAX_PARSED_PDF_CHARS=400000`）；`DEFAULT_PROMPT = "请查看附件。"`；`sniff_image`、`is_pdf`、`decode_text`（UTF-8、UTF-16 带 BOM、本地编码；控制字符超过 2% 当二进制）、`unique_name`、`admit`（数量和总大小）、`describe_rejections`、`insertable_path`；`to_sdk`、`stream_options`（变成 `Session.stream` 的 `images=` / `files=`）；`attachments_from_content`、`typed_text`、`is_system_reminder`（读回已保存的消息）。
- `core/session_manager.py`：`SessionController.send_prompt(text, attachments=())`（只有附件时发 `DEFAULT_PROMPT`）、`attachment_problem`（模型已知 `no_image_support` 时在发送前拒绝）、`SessionManager.model_info`；`_consume` 把附件交给 `session.stream`；`attachment_failure` / `friendly_failure` 把 `InvalidAttachmentError` 翻成「附件无法发送：…」。
- `core/transcript.py`：`UserBlock.attachments`、`Transcript.attachment(id)`、`start_turn(text, attachments)`、`new_history_turn(…, attachments)`；`_on_user_message` 把服务器回显合并进本地已显示的消息。`core/history.py`：把保存的图片块和文档块读回来；用户文字用 `typed_text`。

界面：
- `ui/imaging.py`（新，不需要窗口）：解码（带 EXIF 旋转）、`fit_image`（无损 PNG → 缩到 2048 的 PNG → JPEG 逐级降质量 → 更小的 JPEG）、`thumbnail`（320 像素的 `data:` URI）、`pdf_text`（`QtPdf`，最多 40 万字符，首次用到才导入）。
- `ui/attachments.py`（新）：`load_path` / `load_paths` / `load_mime` / `load_image` 把文件、剪贴板图片、拖放数据变成 `Attachment` 或 `Rejection`（原因和路径）；拖放辅助 `copy_action`、`accept_drag`、`claim_drag`、`wants_attachments`。
- `ui/attachment_bar.py`（新）：`AttachmentBar`（自动换行的卡片条）、`AttachmentChip`（缩略图或带类型的图标、名字、大小、×）、`NoticeStrip`（被拒绝的原因，带「在提示词中插入路径」）。`ui/preview.py`（新）：`ImagePreview` 大图窗口。
- `ui/composer.py`：`PromptEdit` 接管粘贴和拖放（`insertFromMimeData`、`canInsertFromMimeData`、`dragEnterEvent` 等；文字照常粘贴）；📎 按钮；`add_mime` / `add_paths`、`attachments()`、`set_attachments`、`restore_attachments`、`remove_attachment`、`preview_attachment`、`offer`、`insert_offered_paths`；只有附件也能发送。
- `ui/transcript_view.py`：缩略图缓存、`data-act="preview"` 打开 `ImagePreview`、在页面的 `focusProxy()` 上接拖放（`attachmentsDropped`）。
- `ui/main_window.py`：`_on_submit`（先 `attachment_problem`）、`_queued`（每个会话各自的附件队列）、`_inflight` 和 `_settle_inflight`（失败后把文字和附件放回输入框，`RETRY_NOTICE`）、`_on_dropped`。
- `render/fragments.py`：`attachments_html`（整个页面里唯一写 `<img>` 的地方，只认 PNG / JPEG 的 `data:` 地址）、`user_html` 带附件；`render/presenter.py`：`thumbnail` 回调；`web/index.html`（CSP 加 `img-src data:`）、`web/app.js`（`preview` 动作）、`web/app.css`（`.att` 样式）；`tests/htmlcheck.py`：标签白名单里图片默认不允许。

开发和验证用：
- `devtools/replay.py`：回放会话接受 `images=` / `files=`，并在回放的用户消息里原样回显。`devtools/driver.py`：`paste_image`、`paste_files`、`attach_with_button`、`drop_files`、`attached`、`send`，以及合成拖放的 `drag_enter`、`drop_data`、`file_mime`。`devtools/samples.py`：`make_pdf`（手写的单页 PDF，不需要 PDF 库）、`sample_picture`。
- `scripts/live_attach_check.py`（实机脚本，三个阶段，回合预算，模型核对，剪贴板恢复）；`tests/echo_droid.py`（假 droid：按收到的像素、文本和 PDF 文字作答，也像真 droid 那样存历史）。

### SDK 的选项（`droid-sdk 0.5.0`）

- `Session.stream(prompt, *, images: Sequence[Image], files: Sequence[Document], …)`；没有附件时一个都不传（`stream_options` 只返回用到的）。
- `Image.from_bytes(data, media_type=…)`：检查文件头，只收 PNG / JPEG / GIF / WebP，单张不超过 `MAX_ATTACHMENT_BYTES`（5 MiB）。
- `Document.from_text(text, name=…, mime="text/plain")`；PDF 是 `Document(PdfDocumentSource(data=<base64>, parsed_data=<文字>, name=…, path=…))`，不超过 `MAX_PDF_ATTACHMENT_BYTES`（3 MiB）。
- 不合格的数据抛 `InvalidAttachmentError`（消息里写着原因，如 `Attachment exceeds the 5242880-byte limit`）。

### 关键决定

- **限制先在输入框这一侧判断**，用户当场看到原因，不等 droid 报错；文本上限 1 MiB 比 SDK 的 5 MiB 低得多，因为 1 MiB 文本约 25 万 token。
- **类型按内容判断**（文件头），不按扩展名；其他二进制文件不发，改为提示并提供「在提示词中插入路径」，让模型自己用工具读。
- **拖放一律以复制接受**：资源管理器接受「移动」之后会删掉原文件。
- **缩略图由 Python 做成 `data:` URI**，页面里只有一处写 `<img>`，CSP 因此只多一条 `img-src data:`；大图用原生 `QDialog`，不在页面里放大，页面不用多任何脚本能力。模型写的 Markdown 图片仍然是占位文字。
- **模型已知不支持图片就在发送前拒绝**：droid 对这种模型会悄悄把图片换成一句说明，看上去像模型无视图片。
- **失败后放回去**：带附件的消息这一轮失败，文字和附件回到输入框，重试只要按一次 Enter。

### 联调与实机中发现的问题

1. **（实机）droid 在带图片的用户消息里另存了一个文本块** `<system-reminder>Attached image local file paths: … temporary copies …</system-reminder>`，顺序是：图片、这段提示、你打的字。旧代码把所有文本块拼起来，历史里这条消息的文字和侧边栏里的会话标题都以这段提示开头（实机阶段 c 的 1 项检查失败）。修复：`typed_text` 去掉整块的 `<system-reminder>`，历史和实时回显都用它；测试里的 `attachment_history()` 改成真 droid 的块顺序，并加了 6 个测试（提示不算用户输入、只有提示的消息不算一轮、实时回显保留用户文字、会话标题取用户文字、渲染出的 HTML 里没有「system-reminder」）。
2. PySide6：把捕获了 `QDialog` 的 lambda 连到信号上，窗口随父窗口一起删除时进程崩溃。改用绑定方法，并连 `finished` 而不是 `destroyed`（`destroyed` 到来时下一个窗口已经存进去了，会把它忘掉）。
3. PySide6：`QDragEnterEvent` 用 `QPointF` 构造会让进程崩溃，只有（已弃用的）`QPoint` 构造可用；`devtools/driver.drag_enter` 里压掉了这条 `DeprecationWarning`。
4. 只提供「移动」的拖放会冒泡到 `TranscriptView` 里的 Qt WebEngine（它会当成导航或文本），资源管理器随后会删原文件：`claim_drag` 对这种拖放也吞掉事件并明确拒绝。Qt WebEngine 自己消化投放事件，所以拖到对话区要在 `focusProxy()` 上装事件过滤器。
5. `QUrl.toLocalFile()` 在 Windows 上返回正斜杠路径（`D:/x/y.txt`）；`QFileDialog` 的起始目录要用 `str`，不能是 `Path`，所以 `Composer.start_dir` 是 `str`。
6. 事实：`load_session` 返回的 PDF 带着字节，虽然磁盘上的 jsonl 里 `data` 是空串；droid 把它另存在会话目录的 `attachments\` 下。图片被 droid 重新编码（发送的 PNG 10.6 KB，历史里是 RGBA 的 PNG，11.4 KB）。`noImageSupport` 对 `claude-haiku-5-5` 是 `False`。

### 先演练，再实机

实机回合只有 3 个，所以脚本先对着假 droid 演练：`tests/echo_droid.py` 按收到的像素（红、蓝、文字）、文本和 PDF 文字作答，也像真 droid 那样存历史（包括 `<system-reminder>` 块和 PDF 字节的还原）；`tests/test_live_attach_script.py` 在离屏窗口里把三个阶段完整跑一遍。另外在真显示器上用这个假 droid 演练了一次（39 项检查全部通过），并逐张看过当时的截图，确认输入框卡片、对话里的缩略图和文件卡片、历史、大图窗口的外观。之后才用真 droid：脚本在开始前检查回合预算（不够就什么都不启动）、核对 droid 报告的模型。

### 实机运行（`scripts/live_attach_check.py`，真窗口 + 真 droid.exe 0.251.0）

| 运行 | 做了什么 | 实机回合 | 结果 |
| --- | --- | --- | --- |
| 1 | `--stages a,b` | 2 | 27 项通过。阶段 a：`Ctrl+V` 粘贴生成的 PNG（还粘贴了文本文件、往对话区拖入 PDF，再用 × 移除），问图里有什么，回复「A red rectangle on the left and a blue ellipse on the right sit above the word "TANGERINE-42" on a pale yellow background.」。阶段 b：📎 选文本和 PDF，回复 `codeword=MARMALADE-7731; total=1250.00 EUR` |
| 2 | `--stages c --no-follow-up` | 0 | 19 项里 1 项失败：历史文字以 `<system-reminder>` 开头（问题 1） |
| 3 | 修复后重跑同一阶段 | 0 | 19 项通过：历史里的缩略图和两张文件卡片（TXT notes.txt 89 B、PDF invoice.pdf 697 B）、点缩略图开出 640×360 的大图窗口 |
| 4 | `--stages c`（带追问） | 1 | 21 项通过；恢复出来的会话答出 `codeword=MARMALADE-7731; total=1250.00 EUR`，上下文（包括 PDF 里的金额）还在 |

报告：`sandbox/phase1/runs/attach-*.json`（4 份），过程日志 `sandbox/phase1/live_attach_log.txt`，`live_attach_report.json` 是最近一次。四次运行前后的 droid 进程相同（桌面版自己的 4 个，没有被动），没有残留的 droid.exe，没有意外的权限提问。

### 自动测试

6 个新测试文件共 179 个：`test_attachments`（50，规则和限制）、`test_attachments_session`（37，从输入框到 SDK、保存的消息、回放）、`test_attachments_render`（26，页面标记、转义、CSP 之下只有 PNG / JPEG 的 `data:` 才是 `<img>`）、`test_ui_attachments`（40，输入框的粘贴、拖放、卡片、队列）、`test_ui_attachments_window`（21，整窗口）、`test_live_attach_script`（5，对假 droid 演练实机脚本）；`test_driver` 里新增了驱动的附件动作。内容分条见 `docs/phase1.md` 第 5 节。

### 没有验证 / 新增的风险

- `Ctrl+V` 的按键、拖放事件是 Qt 合成的（剪贴板是真的）；在资源管理器里真的拖一个文件进来没有试过；📎 的系统对话框由脚本代答。
- 超过 5 MiB 的图片压缩后发送、回合运行中排队、发送失败后放回输入框、模型不支持图片时的拦截、droid 拒绝附件时的错误文字：只有离线测试（实机回合用完了）。
- PDF 的 `parsed_data` 是否被没有原生 PDF 支持的模型用上：没有验证（实机的 haiku 5.5 答对了金额，但分不清它读的是 PDF 还是文字）。
- 深色主题、其他显示缩放下的卡片和缩略图没有看过。
- 读文件和压缩图片在界面线程里同步做，没有测过很大的图片会停多久。
- 只在这一台机器、droid 0.251.0 上验证过。`DroidClient.load_session` 把 PDF 字节还原、`<system-reminder>` 文本块，都是 droid 的行为，升级之后可能变。

## 里程碑 7：改名 WittyCode、会话控件下移、上下文显示、测试修复（已完成）

用户要求：应用改名 WittyCode；模型 / 推理 / 自主 / Auto-Spec 像 Codex、Claude 桌面版那样收成输入框下方的一排，去掉顶部工具栏；状态栏显示上下文用量。另外修掉评审时发现的、只有全量跑才失败的截图测试。`pytest tests -q`：588 → **644 passed**（连续 3 次，另加 1 次逆序收集，约 38 秒；净增 56 个）。另外用了 **2 个实机回合**（`claude-haiku-5-5`；另 3 次 `--no-turn` 运行只截图、不发消息；单独计数，`sandbox/batch2/` 和前几组的回合无关）。

### 做了什么

- **改名**（包名 `desktop`、文件夹 `droid-desk` 有意不变）：`ui/main_window.py` 的 `APP_TITLE`；`ui/app.py` 的 AppUserModelID（`WittyCode.app`）和日志名（`WittyCode.log`）；`WittyCode.pyw`（新；`droid-desk.pyw` 保留，原样转去运行它，改名前做的快捷方式不受影响）；`_pyguard.py`、`web/index.html` 的标题文字。`core/settings.py`：设置在 `%APPDATA%\WittyCode\settings.json`，`migrate_legacy_settings` 在第一次启动时把 `%APPDATA%\droid-desk\settings.json` **复制**过来——先写成 `.migrating` 再改名（半路死掉的复制不会留下半份、被当成「已有设置」），新位置已有设置就不动，旧文件不删不改（旧版本照样能用），显式 `--settings` 时从不迁移。测试全部用临时文件夹，不碰真实 `%APPDATA%`。
- **会话控件下移**：`ui/settings_bar.py`（新：`Chip` 是宽度跟随「标题 + 当前值」文字的下拉框，点击打开列表但不抢输入框焦点；`SettingsBar` 四个下拉框，模型列表带计费倍率和禁用原因，值用 `show_values` 一次性设置且不发信号）；`ui/composer.py`（`set_controls` 把控件行放进输入框的框里、文字正下方）；`ui/main_window.py`（顶部工具栏删除；`_choose` / `_apply_setting` / `_refresh_controls`：当前会话就绪或运行中显示并作用于它的实时设置，否则显示新会话的默认值；Droid 报告的值优先——它可能替换模型）。
- **上下文显示**：`core/context.py`（新：`ContextState`、`read_context`、`format_tokens`；三个数的来源见 `docs/phase1.md` 第 3 节）；`core/session_manager.py`（`SessionController.context` 与 `refresh_context`：每轮结束、换模型、droid 的 `session_token_usage_changed` / `session_compacted` 通知时重读，读取合并成至多一次在飞加一次排队；压缩后「已用」先标未知，读到的数和压缩前相同就当还没更新）；`ui/status_line.py`（上下文标签在用量左边，`上下文 13K / 197K`，不知道的显示 `—`，悬停逐项解释含自动压缩阈值）；`ui/main_window.py`（`context` 事件走 120 毫秒的 chrome 刷新）。测试 `tests/test_context.py`（38 个，含守住 SDK 私有接口的 `test_the_sdk_still_offers_what_the_context_reading_relies_on`）、`tests/fakes.py`。
- **截图测试修复**：`tests/test_driver.py::test_a_capture_that_misses_the_page_is_taken_from_the_screen_instead_or_refused` 单独跑过、全量跑失败——页面报告「就绪」后还有几十毫秒内容没画到屏幕上，截图的时机依赖前面测试留下的状态。修复在 `devtools/driver.py`：截图量页面区域的颜色数，部件截图漏掉 WebEngine 表面时用屏幕像素兜底，空白的截图在 `paint_timeout` 内重试，两处都拍不到就拒绝存图；测试本身先 `wait_painted` 再用猴子补丁制造「漏拍」。修完后全量 3 次加逆序 1 次都过。
- **实机脚本**：`scripts/live_batch2_check.py`（新；核对标题、控件跟随 Droid 报告的模型、新会话的窗口和阈值、回合后的「已用 / 窗口」，截三张图；`--no-turn` 只截图不发消息；按 Droid 报告的实际模型核对数字，模型被替换时改查一致性）。

### 关键决定

- 设置迁移**只复制一次**：移动或删除旧文件都会让旧版本丢设置；复制则两边都能用，代价只是各占一份。
- 状态栏斜线后面放**窗口**而不是压缩阈值：窗口是硬上限、跟着模型走（切模型时用户想看的就是它）；阈值在使用中其实更相关，放在悬停提示里，连同「已用相当于阈值的百分比」。
- 「已用」用 Droid 记录的最近一次模型调用（`last_call_compaction_tokens`），不用 `Session.context().used`：后者只是固定提示的估计，不跟对话走。这个数随会话保存，恢复的会话不用等新一轮。
- 读取由 droid 的通知驱动而不是定时轮询；读取请求合并，界面事件风暴不会变成请求风暴。

### 实机发现的问题和事实

1. **（实机）droid 会把回合交给另一个模型服务而不报错**：请求 `claude-haiku-5-5` 的回合实际由 `kimi-k3` 服务（2026-10-29，droid 0.251.0，账户级行为；本机同期的 Factory 会话也跑在 Kimi K3 上）。会话设置随后报告 kimi-k3，模型下拉框跟着变；`get_context_breakdown().model_id` 报告实际服务的模型，窗口（196608）和阈值（196608，窗口小于默认的 250 000）都跟着它。第一次实机运行因此有 1 项检查失败——脚本硬编码了 Haiku 的数字；改成「模型是请求的才核对具体数字，否则核对一致性」后重跑通过。
2. 实机数字：新 Haiku 会话发消息前 used=None、window=872000、budget=800000（和「1M − 128K 输出」「用户的每模型压缩上限 800000」一致）；一个回合后（kimi-k3 服务）used=13086（运行 1：13119）、window=196608、budget=196608，状态栏显示「上下文 13K / 197K」。
3. 截全窗口（带系统标题栏）要从屏幕上读像素，窗口被别的窗口盖住时会把别的窗口拍进来（前两次 `--no-turn` 运行的标题图都废了）：现在截标题图前把窗口临时置顶（`WindowStaysOnTopHint`，不抢输入焦点），截完恢复。

### 验证

- `.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`：**644 passed**（连续 3 次，约 38 秒；另用 `pytest_collection_modifyitems` 逆序收集跑了 1 次，也全过）。
- 实机：`scripts/live_batch2_check.py` 运行 2 全部 12 项通过（1 个回合）；`--no-turn` 运行 3 次各 7 项通过（0 回合）。报告 `sandbox/batch2/live_report.json` 与 `live_report_screenshots.json`。

### 没有验证 / 新增的风险

- 真实压缩下的上下文显示只有离线测试（实机 2 个回合没触发压缩）；用户手动切换模型后分母跟随也只有离线测试（实机见过的是 droid 自己替换模型）。
- 迁移只用临时文件夹测过；真实的 `%APPDATA%\droid-desk\` 没有被碰过，「真实旧设置在第一次启动时被复制」没有实机走过。
- 上下文显示依赖 SDK 私有 API（`Session._require_client()`），SDK 锁 0.5.0，有测试守住；这个门没了的时候只剩窗口一个数。
- 改名后旧的日志 `%APPDATA%\droid-desk\droid-desk.log` 不会搬到新位置（只迁移设置）；`WittyCode.pyw` 的启动消息框和 `droid-desk.pyw` 的转发没有实机点过（`test_no_console.py` 用真的 `pythonw.exe` 覆盖了两个 `.pyw` 的启动和日志路径）。