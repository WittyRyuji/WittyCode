# Phase 0b 健壮性验证报告(WittyCode)

验证对象:未来基于 PySide6 + Qt WebEngine + Factory Droid Python SDK 的桌面客户端。
本文件汇总 10 个健壮性验证项(spike)的结果。所有 spike 脚本位于 `spikes/robust_*.py`,
录制文件位于 `fixtures/robust/`,沙箱产物位于 `sandbox/robust/`。

## 0. 验证环境与口径

- droid-sdk 0.5.0;droid.exe 0.251.0(`%LOCALAPPDATA%\Factory\app-0.208.0\resources\bin\droid.exe`);
  Python 3.11.9;Windows 11(10.0.26200);PySide6 6.12.0;qasync 0.28.0。
- 默认请求模型 `claude-haiku-5-5`,reasoning low,autonomy off,沙箱内权限自动批准。
- 例外:长等待(第 2 项)的两个回合,因验证期间账户默认模型被改为 `kimi-k3`,
  droid 端下发 `SettingsUpdated` 把会话模型切到 kimi-k3,这两个回合实际由 kimi-k3 执行
  (其 tool_use id 为 `call_...` 风格而非 `toolu_...`)。此现象本身即第 3 项的重要发现。
- **实时模型回合总数:26**(明细:长等待 2、错误 4、钩子 1、并发 9、清理 4、多编辑 1、
  静默命令探针 1、WebChannel 4)。超出约 20 的预算 6 个,主要用于 WebChannel qrc 问题复跑
  与静默命令泄漏的追加探针。

## 1. 结果总表

| # | 验证项 | 结论 | 关键证据 |
|---|--------|------|----------|
| 1 | 多会话内存/进程模型 | PASS | 每 Session 一个 droid.exe(python 直接子进程);常驻 RSS ≈270–283 MB/会话;9/9 回合成功 |
| 2 | 权限/提问长时间挂起 | PASS | 异步 handler 挂 600 s,期间 0 条流事件、无超时无取消;恢复后 RunSuccess(总 ~618 s) |
| 3 | 错误传播 | PASS | 各错误路径形状完整;重要发现:无效模型 ID 不报错、静默替换为账户默认模型 |
| 4 | 中断/关闭时子进程清理 | PASS(Windows) | droid 自建 Job Object;close/中断/硬杀宿主均无泄漏(含静默命令) |
| 5 | 工具输出流式更新 | PASS | ToolProgress ~1.0 s 节奏、内容为最近 ~2 行滚动尾部;ToolResult 为完整输出 |
| 6 | 多文件编辑 | PARTIAL | Edit×2 全流程验证(权限逐文件串行、diff 可重建);haiku 无 ApplyPatch 工具,未验证 |
| 7 | 应用更新时运行中会话 | PASS | 运行中 exe/目录可重命名且会话不受影响;删除被 WinError 5 阻止;close 后锁释放 |
| 8 | factory_credits 语义 | PASS(文档级) | FSC:按原始输入/输出 token 计、含缓存折扣;每模型倍率(haiku-5-5 = 0.04×) |
| 9 | QWebChannel 双向桥 | PASS | 窗口+离屏两种模式:JS→Python echo 成功;TextDelta 实时推页、拼接结果与回合文本一致 |
| 10 | SDK 会话在桌面端可见 | PARTIAL | 磁盘结构与桌面会话完全一致(同目录规范、version=2);待用户目测确认 |

## 2. 各项细节与对 UI 的影响

### 1) 内存与进程模型(fixtures/robust/concurrency.json)

- 进程模型:**每个 Session 恰好一个 droid.exe,是 Python 进程的直接子进程**
  (9 个会话的 droid ppid 均为 spike 的 python PID)。无共享服务进程。
- 单会话常驻:droid.exe RSS 270–283 MB、私有字节 396–560 MB。
- 活动峰值(执行 Execute 时):进程树 = droid + powershell×2 + conhost×2 共 5 个进程,
  树峰值 RSS ~450–461 MB/会话,树峰值私有字节 ~573–651 MB/会话。
- Python 宿主自身峰值 RSS 仅 48.8 MB(9 会话、376 个采样点)。
- 空闲 5 分钟(5 会话):RSS 279→270 MB、私有 478→417 MB,**无增长,无空闲泄漏**。
- 关闭:5/5 会话 close() 后进程立即消失(首次轮询 0 s 即不可见),无 close 错误。
- 并发延迟:1 会话回合 17.6 s;3 并发 19.0–21.6 s;5 并发 21.1–25.4 s(约慢 1.2–1.45×)。
  open() 2.2–6.7 s。5 并发总墙钟 34.2 s(串行约需 130 s),并行吞吐约 3.8×。
- **UI 影响**:内存预算按 ~300 MB/会话常驻 + ~500 MB/活动会话峰值;并行 5 个以内体验良好。

### 2) 权限/提问长时间挂起(fixtures/robust/long_waits_{perm,ask}.jsonl)

- 异步 permission handler 与异步 question handler 各挂起 600 s:
  **SDK 与 droid 均无超时、无取消;等待窗口内流上 0 条事件(无心跳/保活消息)**。
- 恢复后回合正常完成 RunSuccess,总时长 617.9 s / 618.2 s(等待窗口 14.3→614.3 s / 18.6→618 s)。
- 异步 handler(协程)确认可用。
- **UI 影响**:确认对话框**不需要超时倒计时**;但等待期间流完全静默,UI 要自己维持
  "等待用户"状态(不能依赖任何周期性事件);若此时 close(),见第 4 项的挂住注意事项。

### 3) 错误传播(fixtures/robust/errors_summary.json、errors_*.jsonl、multiedit.jsonl)

- `cwd` 不存在 / 是文件 → `open()` 本地抛 `InvalidWorkingDirectoryError`(不产生进程)。
- 可执行文件缺失 → `DroidConnectionError`("Droid executable was not found",带 cwd/exec_path);
  指向非可执行文件 → `DroidConnectionError`(WinError 193)。
- **无效模型 ID(`definitely-not-a-model-0b`)不产生任何错误**:droid 静默改用账户默认模型,
  回合 RunSuccess。实际的模型只通过 `SettingsUpdated` 事件体现。
- 回合中杀 droid 进程(模拟崩溃):流抛出 `DroidProcessError`(message + exit_code=1);
  之后 `session.close()` 会**再次抛出同一个错误**(sticky error,需调用方捕获)。
- 权限 handler 抛异常 / 返回非法 PermissionResponse(如选了不在 options 里的行为):
  `ErrorEvent(DroidClientError, "Permission interaction handler failed")` →
  `PermissionResolved(cancel, tool_use_ids=[])` → `ToolResult is_error=True,
  "Error: Tool execution cancelled by user"` → **回合仍以 RunSuccess 结束**(文件未创建)。
- 提问 handler 抛异常:同上,ErrorEvent 文案为 "Question interaction handler failed"。
- 工具自身失败(读不存在的文件):**只有** `ToolResult is_error=True,
  content="Error: Failed to read file"`,无 ErrorEvent,回合继续正常推进。
- **UI 影响**:`RunSuccess` ≠ 用户意图达成;需结合 ErrorEvent 与 ToolResult.is_error 判断。
  模型显示必须以 `SettingsUpdated` 为准,不能以请求值为准。崩溃后 close() 要 try/except。

### 4) 中断/关闭时的子进程清理(fixtures/robust/proc_cleanup_summary.json、proc_probe_summary.json)

- **机制发现:droid.exe 启动即把自身放入 Windows Job Object(IsProcessInJob=true),
  其 Execute 工具产生的 powershell/conhost 子孙也都在该 job 中**。因此任何路径下 droid 退出,
  内核都会收割整棵树。
- (a) 回合中(`ping -n 600` 运行)close():1.42 s 完成,整树消失,无泄漏。
- (b) `stream.interrupt()` → RunInterrupted(1.26 s),随后 close() 仅 26 ms。
- (c) 子 Python 被 TerminateProcess 硬杀(无 job):droid 因 stdin EOF 自行退出,job 收割工具树,无泄漏。
- (d) 子 Python + 我方 Job Object(KILL_ON_JOB_CLOSE):同样无泄漏。
- (e) 追加探针——**静默命令**(`Start-Sleep -Seconds 600`,无任何输出,排除"管道断裂致死"解释):
  硬杀宿主后 4 s 内 powershell 全部消失,仍无泄漏。
- 附带发现 1:对有活动回合的会话调用 close(),该回合的消费协程会**挂住**(30 s 无结果,
  最终由流超时兜底)。**UI 在 close 时必须自行取消消费 task**。
- 附带发现 2:Windows PID 复用很快(本测试中约 1 分钟内复用;一个已关闭会话的 PID 随后
  变成 WmiPrvSE.exe)。用 PID 判断存活要留意。
- **UI 影响**:Windows 上**无需**在桌面端自建 job/进程清理兜底,close() 即可;崩溃场景也不泄漏。
  POSIX(非 Windows)未验证。

### 5) 工具输出流式更新(fixtures/robust/concurrency_n1_s1.jsonl)

- `ToolProgress` 事件按 ~1.0 s 节奏增量到达(实测间隔 0.97–1.02 s)。
- 其 `content`/`update.text` **不是全量**,而是最近输出的滚动尾部(观测为最后 ~2 行,
  如 "tick 3\r\ntick 4\r");最终 `ToolResult.content` 才是完整输出。
- **UI 影响**:命令面板把 ToolProgress 渲染为"最新输出尾部",在 ToolResult 到达时整体替换为完整内容;
  不要做 append-only 拼接。

### 6) 多文件编辑(fixtures/robust/multiedit.jsonl)

- claude-haiku-5-5 的工具集中**没有 ApplyPatch**(fixtures/tools.json 无此条目),模型选择
  在**同一个 AssistantMessage 里发两个 ToolUse(Edit)**。
- 权限请求**按文件逐个串行**到达:批准 a.txt → 执行 → 再请求 b.txt。每个 EditAction 含
  `file_path / file_name / old_content / new_content / confirmation_type:"edit" /
  tool_use.input.{file_path,old_str,new_str}`——**可在弹窗里预渲染完整 diff**。
- Edit 的 ToolResult content 是 JSON 字符串:
  `{success, file_path, diffLines:[{type:"added"|"removed", content, lineNumber:{old|new}}],
  linesAdded, linesRemoved}`——事后可按文件重建 diff。
- **UI 影响**:逐文件 diff 视图可行(事前 old/new content、事后 diffLines 双通道);
  取消某一个文件的权限只取消该 action,另一文件不受影响。
- 未验证:ApplyPatch 的事件形状(需支持该工具的模型,如 GPT 系)。

### 7) 应用更新时的运行中会话(fixtures/robust/app_update.jsonl)

- 在假应用根目录下做更新模拟:复制 exe(310.6 MB,0.13 s)即可得到完整可用的 droid(0.251.0);
  参数化的 `bundled_candidates(root=...)` 在新目录出现后正确切换 0.208.0→0.209.0。
- 会话运行中:**删除运行中的 exe 被系统阻止(WinError 5)**;**重命名 exe 文件成功**;
  **重命名整个 app 目录成功**;运行中的会话完全不受影响(目录重命名后 RPC 仍正常)。
- `session.close()`(37 ms)后 exe 可删除(文件锁释放)。
- **UI/更新器影响**:更新流程应是"先复制/就位新版本 → 下次启动(或下次 open())切换",
  不要试图删除正在运行的旧版本;**每次 open() 都重新解析 exe 路径**(按目录扫描选最新)。
  运行中的会话不会被更新操作打断。

### 8) factory_credits 语义(文档级)

- `factory_credits` = Factory Standard Credits(FSC),由原始输入/输出 token 计算并含缓存折扣
  (docs.factory.com/api-reference/analytics);每个模型有倍率表(docs.factory.com/models,
  haiku-5-5 = 0.04×);计费上套餐采用滚动速率限制(5h/7d/30d),Extra Usage 为美元计价
  (docs.factory.com/pricing/individuals)。
- 观测量级:简单回合 150–810 credits;大型桌面会话 3.5M。credits 是细粒度单位而非 token。
- **UI 影响**:适合做"本次会话累计消耗"显示;要表达"贵不贵"需结合模型倍率,不能跨模型比 token。

### 9) QWebChannel 双向桥(fixtures/robust/qwebchannel_summary_{windowed,offscreen}.json、qwebchannel.png)

- qasync + QWebEngineView + QWebChannel,窗口模式与 `QT_QPA_PLATFORM=offscreen` 离屏模式均通过:
  JS→Python(`bridge.echo("hello-from-js")` 返回 "pong:hello-from-js");
  Python→JS(回合 3 个 TextDelta 实时 `runJavaScript` 推入页面,页面拼接结果与回合最终文本完全一致)。
- 页面加载 26–63 ms。
- **坑**:页面 baseUrl 为 http(s) 时,`<script src="qrc:///qtwebchannel/qwebchannel.js">`
  会被 Chromium 拦截(本地资源不允许被远程页面加载)。解法:Python 侧用
  `QFile(":/qtwebchannel/qwebchannel.js")` 读出内容并**内联**进 HTML(spike 已实现)。

### 10) SDK 会话在桌面端的可见性(分析,待目测)

- 对比 `~/.factory/sessions/-<项目路径>-sandbox-robust-.../`(SDK)与
  `~/.factory/sessions/-<项目路径的上一级>/`(桌面):两者结构完全相同——
  `<id>.jsonl`(首条 `session_start`,字段一致:id/title/owner/organizationId/version=2/cwd/hostId)
  + `<id>.settings.json`。
- 差异仅在元数据:SDK 会话的 settings.json 带 `tags:[{name:"sdk",metadata:{language:"python",
  version:"0.5.0"}}]`,消息 `userMessageSource:"sdk"`(桌面为 "desktop";子代理会话带 "subagent" 标签)。
- 结论:SDK 会话**应当**出现在对应工作区文件夹的会话列表中。
- **请用户确认**:打开 Factory 应用 → 打开工作区 `<项目根>\sandbox`
  (以及 `sandbox\robust\web` 等子目录)→ 查看会话列表中是否出现 spike 会话标题,
  例如 "webchannel webchannel webchannel webchannel webchannel"、"Hook test execution" 等。

## 3. 事件/错误形状速查(供 UI 事件映射)

| 场景 | 形状 |
|------|------|
| handler 异常/非法返回 | `ErrorEvent{error_type:"DroidClientError", message:"Permission/Question interaction handler failed"}` → `PermissionResolved(cancel, tool_use_ids=[])`(空数组)→ `ToolResult is_error=True "Error: Tool execution cancelled by user"` → `RunSuccess` |
| 工具自身失败(如读不到文件) | 仅 `ToolResult is_error=True, content="Error: ..."`,回合继续 |
| droid 进程被杀/崩溃 | 流抛 `DroidProcessError(message, exit_code=1)`;`close()` 重复抛同一错误 |
| 无效模型 ID | 无错误;`SettingsUpdated{settings.model=...}` 指示实际模型 |
| 主动中断 | `stream.interrupt()` → `RunInterrupted(subtype="interrupted")`,droid 保活可继续 |
| 权限请求(Edit) | `EditAction{file_path, file_name, old_content, new_content, confirmation_type:"edit", tool_use.input{file_path,old_str,new_str}}` |
| Edit 结果 | ToolResult content JSON:`{success, file_path, diffLines[{type,content,lineNumber}], linesAdded, linesRemoved}` |
| 命令流式输出 | `ToolProgress{tool_use_id, tool_name, update{type:"status", text=滚动尾部}}` ~1/s;完整输出在 ToolResult |
| 钩子 | `HookExecution started/completed(exit_code)`;SessionStart 钩子有执行(磁盘日志)但无流事件 |
| 隐藏消息(渲染需过滤) | `UserMessage` id 前缀:`context-`、`todo-sug-`、`__internal__system-reminder__` |
| 运行时模型/设置变化 | `SettingsUpdated` 可随时到达(账户默认模型变化时),含 model/reasoning_effort/mode/autonomy |

## 4. 未验证项 / 遗留问题

1. ApplyPatch 的事件形状(haiku 工具集无此工具;需 GPT 系模型)。
2. `SettingsUpdated`/模型静默替换的确切语义(触发时机、是否持久到会话)。
3. SessionStart 钩子无对应流事件(磁盘日志确认已执行;流上只见 UserPromptSubmit/PreToolUse/PostToolUse/Stop)。
4. POSIX(Linux/macOS)下硬杀宿主的进程清理(Windows 依赖 droid 自建 Job Object 已验证)。
5. 5 会话以上并发、以及 handler 挂起超过 600 s 的行为(均为观测点而非上限证明)。
6. 桌面 app 会话列表的最终目测(第 10 项,需用户操作)。
7. `close()` 活动会话导致回合消费协程挂住的根治(SDK 层是否应主动唤醒,留待 SDK 反馈)。

## 5. 产物清单与重跑方式

脚本(`spikes/`,均依赖只读的 `_common.py` 与新增的 `_robust_common.py`):

| 脚本 | 覆盖项 | 实时回合 |
|------|--------|----------|
| `robust_long_waits.py` | 2 | 2 |
| `robust_errors.py` | 3 | 4 |
| `robust_hooks.py` | 3(钩子) | 1 |
| `robust_concurrency.py` | 1、5 | 9 |
| `robust_proc_cleanup.py` | 4(a–d) | 4 |
| `robust_proc_probe.py` | 4(e 静默命令) | 1 |
| `robust_multiedit.py` | 6 + 3 补例 | 1 |
| `robust_qwebchannel.py` | 9 | 4 |
| `robust_app_update.py` | 7 | 0 |

重跑(仓库根目录):

```powershell
cd '<项目根>'
.\.venv\Scripts\python.exe spikes\robust_concurrency.py --ticks 8 --idle 300
.\.venv\Scripts\python.exe spikes\robust_proc_cleanup.py
.\.venv\Scripts\python.exe spikes\robust_multiedit.py
.\.venv\Scripts\python.exe spikes\robust_qwebchannel.py            # 窗口模式
$env:QT_QPA_PLATFORM='offscreen'; .\.venv\Scripts\python.exe spikes\robust_qwebchannel.py
```

环境变量:`WITTYCODE_MODEL`(默认 claude-haiku-5-5)、`WITTYCODE_DROID_EXECUTABLE`(覆盖 exe 路径)。
录制输出全部在 `fixtures/robust/`(44 个文件;每个 .jsonl 多数配有 .raw.jsonl 原始流)。
密钥扫描已通过(fk-/sk-ant-/Bearer/JWT/私钥模式均无命中;仅误报 `factory_api_key_env_set` 字段名)。
所有 spike 记录的 PID 已确认退出,无残留进程。