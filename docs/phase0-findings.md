# WittyCode Phase 0 验证结论：Droid Python SDK（Windows）

> 日期：2026-10-28。下面的结论都来自本机实测（`spikes/` 脚本和 `fixtures/` 里的录制）。标注「未验证」的条目没有实测证据，只是推断或待办。

## 0. 结论摘要

- Droid Python SDK 0.5.0 在 Windows 11 + Python 3.11.9 上可以直接驱动 Factory 桌面版自带的 `droid.exe`（0.251.0），并沿用现有登录，不需要 `FACTORY_API_KEY`。
- 计划内的能力全部跑通：模型列表、流式事件（含 partial）、审批回调、AskUser 回调、中断、会话列表与恢复、qasync + Qt WebEngine 集成。没有遇到需要修改 SDK 的阻塞问题。
- 对 UI 设计最关键的事实：
  1. 工具卡片必须以 `tool_use_id` 为键。同一个 id 的 `ToolCallDelta` 会出现多次：第一条 `input={}`（只有工具名），后续是**逐字段增长的完整快照**；并行工具调用的 delta 会交错到达。
  2. `ToolCall` 和 `AssistantMessage` 来自同一条 `create_message` 通知（先 `ToolCall`，后 `AssistantMessage`），此时输入已经定稿；之后才是执行、审批、`ToolProgress`、`ToolResult`。
  3. 文件编辑的 diff 拿得到：审批请求里的 `EditAction.old_content/new_content`（整个文件改前/改后内容），以及 `Edit` 工具结果 JSON 里的 `diffLines`。
  4. Thinking 文本是**成批**到达的（一个 thinking 块的所有 `ThinkingDelta` 在 1 ms 内送达），不能当作实时进度；实时状态要看 `WorkingStateChanged(thinking)`。
  5. 中断时会出现竞态：审批回调仍可能被调用、`PermissionResolved(cancel, tool_use_ids=[])`、同一个 `tool_use_id` 收到两条 `ToolResult`。UI 必须幂等处理。
  6. `Session.resume()` 不会在流里重放历史，高层 API 也不提供历史；低层 `droid_sdk.low_level.DroidClient.load_session()` 能拿到完整历史（已验证，不消耗模型调用）。

## 1. Spike 结果

| 步骤 | 脚本 | 结果 | 证据 |
|---|---|---|---|
| 2 | `spikes/find_droid.py` | 通过 | exit 0；选中 `app-0.208.0\resources\bin\droid.exe`，`--version` = 0.251.0；`WITTYCODE_DROID_EXECUTABLE` 覆盖生效，指向不存在的文件时 exit 1；排序自测：app-1.0.0 > app-0.210.0 > app-0.210.0-beta.1 > app-0.208.0 > app-0.99.0 |
| 3a | `spikes/list_models.py` | 通过 | exit 0；70 个模型（1 个 disabled：gpt-6.1-sol-ultrafast，需要组织开通），耗时 1.48 s；选用 `claude-haiku-5-5`（倍率 0.04，目录中最低） |
| 3b | `spikes/stream_turn.py` | 通过 | exit 0；RunSuccess，25.3 s；用到 LS / Read / Create / Edit / TodoWrite；2 次审批（Create、Edit）；`hello.txt` = `"hi there\n"` |
| 3c | `spikes/ask_user.py` | 通过 | exit 0；QuestionRequest：1 题 3 个选项，回答第一个 "Hello"；回复 "Hello there, I hope your day is going well." |
| 3d | `spikes/resume.py` | 通过 | exit 0；`list_sessions(cwd=sandbox)` 返回 3 条（4.8 ms），目标会话在列；resume 打开 1.63 s；回答提到 `hello.txt` 及内容 "hi there" |
| 3e | `spikes/interrupt.py` | 通过 | exit 0；首个工具事件（`ToolCallDelta`）后调用 `interrupt()`，0.09 s 后得到 `RunInterrupted`；同一会话的后续轮回复 "ok" |
| 3e 补充 | `spikes/interrupt.py --trigger ToolProgress,ToolResult --name interrupt_running_tool --no-follow-up` | 通过 | exit 0；在 shell 命令运行中中断，1.11 s 后 `RunInterrupted` |
| 3f | `spikes/qasync_smoke.py` | 通过 | exit 0；真实窗口：WebEngine 加载、JS 往返正常；1 个 `TextDelta`（"pong"）经 `runJavaScript` 追加到页面后读回 "pong"。`QT_QPA_PLATFORM=offscreen` 下 `--no-turn` 也通过 |
| 额外 | `spikes/history_probe.py` | 通过 | exit 0；低层 `load_session` 返回 13 条历史消息（不调用模型） |

## 2. 版本与环境

| 项 | 值 |
|---|---|
| OS | Windows 11（`platform.platform()` = `Windows-10-10.0.26200-SP0`） |
| Python | 3.11.9（`.venv`，由 `py -3.11 -m venv .venv` 创建） |
| droid-sdk | 0.5.0（依赖 pydantic 2.14.1） |
| PySide6 | 6.12.0（PySide6_WebEngine 6.12.0.140） |
| qasync | 0.28.0 |
| droid.exe | `C:\Users\me\AppData\Local\Factory\app-0.208.0\resources\bin\droid.exe`，`--version` = 0.251.0 |
| 模型 | `claude-haiku-5-5`（Haiku 5.5，Anthropic，倍率 0.04；支持 off/low/medium/high/xhigh/max，默认 medium）。大多数 spike 用 `reasoning=low`；`stream_turn` 用 `high` 以便录到 thinking |

`fixtures/models.json` 里有完整模型目录（含 `token_multiplier`、`supported_reasoning_efforts`、`disabled_reason`）。另一个同为 0.04 倍率的是 `gpt-6-luna`。

## 3. Windows 上哪些可用、哪些有坑

可用（均实测）：

- `Runtime(executable=...)` 指定自带的 `droid.exe`；`list_models`、`Session`、`Session.resume`、`list_sessions`、`session.list_tools`、`session.context()`、`session.interrupt()` 全部正常。
- Python 3.11 在 Windows 上默认的 Proactor 事件循环可以正常创建子进程管道；qasync 在 Windows 上的 `QEventLoop` 实际是 `QIOCPEventLoop`（基于 Proactor），在 Qt 事件循环里跑 SDK 也正常。
- WebEngine 在真实窗口（platform `windows`）和 `QT_QPA_PLATFORM=offscreen`（platform `offscreen`）下都能初始化并执行 JS。

坑（都有绕过办法）：

- **控制台编码**：stdout 被管道重定向时，Python 默认用 ANSI 代码页，模型输出里出现非 ANSI 字符就会报错。spike 里统一 `sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")`（见 `spikes/_common.py` 的 `setup_console()`）。GUI 程序不受影响，但日志文件要显式写 UTF-8。
- **`Session.resume()` 没有 `cwd` 参数**：droid 子进程继承 Python 进程的当前目录（SDK 内部 `Path.cwd()`）。会话自己的工作目录会从存档恢复（实测 `session.cwd` = sandbox），但为保险 `resume.py` 先 `os.chdir(sandbox)`。桌面客户端里应在恢复前切换到会话目录，或者确认子进程 cwd 不影响行为（未验证）。
- **关闭进程方式**：SDK 关闭时先发 `close_session` RPC，再对子进程 `send_signal(SIGTERM)`；在 Windows 上这等于 `TerminateProcess`，立即结束，不做优雅退出。实测 `session.close()` 只要 24–31 ms，之后 resume 正常，说明会话已落盘。如果关闭时还有 Execute 启动的 PowerShell 子进程在跑，它们可能变成孤儿进程（未验证）。
- **LS 工具在 Windows 上返回 PowerShell `Get-ChildItem` 的表格文本**（CRLF 换行），UI 需要用等宽字体原样显示。
- **Execute 命令是 PowerShell 语法**（例如 `Write-Output 1; Start-Sleep -Seconds 3`），因为 droid 注入的系统上下文写明了 "Windows PowerShell 5.1"。
- **用户全局配置会进入 SDK 会话**：工具目录、技能列表、子代理列表、系统信息、日期、工作区状态通过隐藏的上下文消息注入（见第 9 节）。`~/.factory/AGENTS.md` 的规则也对 SDK 会话生效：模型在 resume 轮的 thinking 里直接引用了它（具体注入位置未确认）。
- **安装体积和下载**：PySide6 的 WebEngine wheel 有 131.7 MB，本机下载速度约 190 kB/s；第一次前台安装预计会超过工具调用的 15 分钟上限，于是中止，改为后台安装后成功（已下载完的 wheel 走了 pip 缓存）。安装后 `.venv` 共 687 MB，其中 PySide6 占 647 MB。

失败：没有遇到 SDK 层面的失败（没有异常、没有 `RunFailure`）。

## 4. 如何传入自定义 executable

```python
from droid_sdk import Runtime, Session, list_models
from find_droid import find_droid   # spikes/find_droid.py

runtime = Runtime(executable=find_droid())          # 也支持 args=[...], env={...}
models = await list_models(runtime=runtime, cwd=sandbox)
async with Session(cwd=sandbox, model="claude-haiku-5-5", runtime=runtime, ...) as session: ...
session = Session.resume(session_id, runtime=runtime, interactions=...)
# 低层：ProcessTransport(exec_path=str(find_droid()), cwd=str(sandbox))
```

- SDK 实际启动的命令：`<exe> exec --input-format stream-jsonrpc --output-format stream-jsonrpc [runtime.args...]`。
- 子进程环境 = `os.environ` + `runtime.env` + SDK 自己加的 `FACTORY_UPSTREAM_CLIENT_TYPE=sdk`、`FACTORY_UPSTREAM_SDK=python/0.5.0`；只有设置了 `FACTORY_API_KEY`（或传了 `api_key=`）时才会把 key 放进环境变量，不会放进命令行参数。
- 每个 `Session` 对应一个独立的 `droid.exe` 进程；`list_models()` 和低层 `load_session` 各自也会起一个一次性进程。
- `find_droid()` 的解析顺序：`WITTYCODE_DROID_EXECUTABLE`（必须是存在的文件）→ `%LOCALAPPDATA%\Factory\app-*\resources\bin\droid.exe` 中版本最新的一个（按数字逐段比较，同版本的预发布版排在正式版之前）→ PATH 上的 `droid`。
- 建议 Phase 1 每次打开会话时重新解析路径，而不是启动时解析一次：桌面版更新后旧的 `app-x.y.z` 目录可能被删除（未验证）。

## 5. 认证

- 环境里没有 `FACTORY_API_KEY`。自带的 droid.exe 直接用了本机已有的登录（`~/.factory` 下的凭据）：`list_models` 返回了 70 个模型，8 次模型轮次都按预期结束（其中 2 次是按设计中断）。SDK 本身不读取 keyring，读取由 droid 完成。
- SDK 会对子进程 stderr 中疑似密钥的内容做脱敏；录制的 fixture 也做了扫描（`fk-`、`sk-`、Bearer、JWT、apiKey 等字段均为 0 命中）。
- 副作用：droid 会把会话持久化到 `~/.factory/sessions/-<项目路径>-sandbox/`（这是 resume 和 list 所必需的）。本次 spike 新建了 6 个会话（id 见第 13 节）。它们是否会出现在 Factory 桌面版的会话列表里没有检查（未验证）。
- SDK 创建的会话带 `sdk` 标签（`session.settings.tags` 里是 `{name: "sdk", metadata: {language: "python", version: "0.5.0"}}`），用户消息带 `userMessageSource: "sdk"`。

## 6. 事件类型与顺序（工具调用折叠 UI 的依据）

### 6.1 实际观察到的事件

高层流（`include_partial_messages=True`）里出现过：`WorkingStateChanged`、`UserMessage`、`SessionTitleUpdated`、`TokenUsageUpdate`、`ThinkingDelta`、`ThinkingComplete`、`ToolCallDelta`、`ToolCall`、`AssistantMessage`、`ToolProgress`、`ToolResult`、`PermissionResolved`、`TextDelta`、`TextComplete`、`McpStatusChanged`、`RunSuccess`、`RunInterrupted`。

没有出现过：`ErrorEvent`、`HookExecution`、`SettingsUpdated`、`SessionWorkingDirectoryChanged`、`McpAuthRequired/Completed`、`RunFailure`。

底层通知（`session.on_notification`，见 `fixtures/*.raw.jsonl`）：`droid_working_state_changed`、`create_message`、`session_title_updated`、`session_token_usage_changed`、`thinking_text_delta`、`thinking_text_complete`、`tool_call`、`tool_execution_phase_changed`、`tool_progress_update`、`tool_result`、`permission_resolved`、`assistant_text_delta`、`assistant_text_complete`、`agent_turn_completed`、`mcp_status_changed`。其中 **`tool_execution_phase_changed`（`queued` → `executing`，每个工具一条）高层流不提供**，需要时只能自己订阅原始通知。

### 6.2 一轮内的顺序

以 `fixtures/stream_turn.jsonl` 为例（时间是相对本轮开始的秒数，连续的 delta 已合并）：

```text
0.021 WorkingStateChanged -> streaming_assistant_message
0.178 UserMessage id=context-<turnId> content=[]      # 隐藏上下文，仅会话第一轮出现，UI 必须忽略
0.194 UserMessage id=<turnId>  (用户 prompt)
0.214 WorkingStateChanged -> thinking
1.709 TokenUsageUpdate (全 0)
1.805 SessionTitleUpdated "Execute file operations sequence"
--- 第 1 次 LLM 调用：message_id = M1 ---
4.784 ThinkingDelta x9 (M1, block 0)                  # 9 条在 0.5 ms 内到达
7.188 ThinkingComplete (M1, block 0, duration=2.404s)
7.188 WorkingStateChanged -> streaming_assistant_message
7.188 ToolCallDelta TodoWrite {}        7.189 ToolCallDelta LS {}
7.290 ToolCallDelta TodoWrite {todos}   8.668 ToolCallDelta LS {directory_path}
8.668 ToolCallDelta Read {}             8.668 ToolCallDelta Read {file_path}
8.693 ToolCall TodoWrite / ToolCall LS / ToolCall Read          # 输入已定稿
8.693 AssistantMessage M1 [Thinking, ToolUse(TodoWrite), ToolUse(LS), ToolUse(Read)]
8.693 WorkingStateChanged -> executing_tool
8.714 ToolResult TodoWrite   8.718 ToolResult Read   9.000 ToolResult LS   # 完成顺序 != 调用顺序
9.018 WorkingStateChanged -> streaming_assistant_message
9.020 WorkingStateChanged -> thinking
--- 需要审批的一步（Edit）---
15.383 ToolCallDelta TodoWrite x2
15.775 ToolCallDelta Edit x4: {} -> {file_path} -> {file_path, old_str} -> {file_path, old_str, new_str}
16.168 ToolCall TodoWrite / ToolCall Edit
16.168 AssistantMessage M3 [ToolUse(TodoWrite), ToolUse(Edit)]
16.168 WorkingStateChanged -> executing_tool
16.185 ToolResult TodoWrite
16.196 WorkingStateChanged -> waiting_for_tool_confirmation
16.197 [on_permission 回调] EditAction -> proceed_once
16.198 WorkingStateChanged -> executing_tool
16.198 PermissionResolved proceed_once [Edit 的 tool_use_id]
16.204 ToolResult Edit (JSON，含 diffLines)
--- 最后一步：只有文本 ---
25.030 WorkingStateChanged -> streaming_assistant_message
25.030 TextDelta x10 (M5, block 0, 共 92 字符)
25.245 TextComplete (M5, block 0)
25.287 AssistantMessage M5 [Text]
25.311 TokenUsageUpdate
25.311 RunSuccess
```

Execute 带进度的一步（`fixtures/interrupt_running_tool.jsonl`）：

```text
5.458 ToolCall Execute ; AssistantMessage [Thinking, ToolUse(Execute)]
5.458 WorkingStateChanged -> executing_tool
5.471 WorkingStateChanged -> waiting_for_tool_confirmation
5.472 [on_permission 回调] ExecuteAction -> proceed_once
5.473 WorkingStateChanged -> executing_tool ; PermissionResolved proceed_once
5.486 ToolProgress Execute update.type=status text="" terminal_id=<uuid>
（此处调用 interrupt）
5.494 ToolResult Execute is_error=True "Error: Tool execution cancelled by user"
6.568 ToolResult Execute is_error=True "Error: Tool execution cancelled by user"   # 同一 id 第二条
6.594 RunInterrupted
```

### 6.3 对折叠 UI 的具体含义

- **以 `tool_use_id` 为键**。第一条 `ToolCallDelta` 就带工具名，可以立刻建卡片；之后每条 delta 的 `input` 是到目前为止已解析的完整对象（按字段增长，不是 JSON 字符串片段），直接整体替换即可。
- **`ToolCallDelta` 没有 `message_id`**。工具卡片属于哪条 assistant 消息，要等 `AssistantMessage`（其 `content` 里的 `ToolUseBlock.id`）才能确定。`ThinkingDelta`/`TextDelta` 带 `message_id` 和 `block_index`。
- 同一条 assistant 消息里 thinking、text、tool_use 是不同的 block：resume 轮中 thinking 是 block 0、正文是 block 1，二者 `message_id` 相同。渲染时用 `(message_id, block_index)` 作为键。
- **`ToolCall` 与 `AssistantMessage` 同时到达**，输入与最后一条 `ToolCallDelta` 一致，可以作为「输入定稿」信号。
- 观察到的状态机：`ToolCallDelta`（输入生成中）→ `ToolCall`（定稿）→ [`waiting_for_tool_confirmation` + 审批回调 → `PermissionResolved`] → `ToolProgress`（仅 Execute）→ `ToolResult`。`WorkingStateChanged` 是会话级状态，不区分具体工具；需要逐个工具的 queued/executing 时，订阅原始通知 `tool_execution_phase_changed`。
- 并行工具调用会同时执行，`ToolResult` 的顺序可能和调用顺序不同（Read 比 LS 先完成）。
- `ToolResult.tool_name` 是 SDK 根据之前的 `ToolCall`/`ToolCallDelta` 补上的。
- **Thinking 成批到达**：各次运行中一个 thinking 块的所有 delta 都在约 1 ms 内送达，`ThinkingComplete.duration` 有时只有 0.001 s（实际思考了数秒）。思考中的指示要靠 `WorkingStateChanged(thinking)`。`reasoning=low` 时 Haiku 5.5 的 thinking 时有时无：`stream_turn_reasoning_low`、`ask_user`、`qasync_smoke` 三轮完全没有（`thinking_tokens=0`，没有任何 `ThinkingDelta`），`interrupt` 和 `interrupt_running_tool` 两轮有（244 和 129 个 thinking token）。UI 不能假设每一步都有 thinking 块。
- `TextDelta` 每片 1–32 个字符；约 100–150 个字符的回复会分成 9–12 片。按 SDK 文档，实时渲染用 delta，持久化用完整消息，二者不要拼接。
- `TokenUsageUpdate` 是**整个会话的累计值**（resume 后也包含之前的轮次）；`RunResult.usage` 是**本轮增量**。每轮第一条 `TokenUsageUpdate` 经常全为 0。
- `McpStatusChanged` 可能出现在第一轮流的开头（它和会话打开存在竞态）。
- `SessionTitleUpdated` 每个会话 0–2 次：qasync 会话先收到 prompt 原文作为标题，再收到 LLM 生成的 "Pong"；ask_user 会话一次也没有，存档标题就是截断的 prompt。
- 隐藏上下文消息 `UserMessage(id="context-<turnId>", content=[])` 只出现在会话第一轮；SDK 丢掉了 `visibility` 字段，只能按 id 前缀或空内容过滤。

## 7. 工具名、输入和结果

本次实际用到的工具：`LS`、`Read`、`Create`、`Edit`、`TodoWrite`（显示名 "Plan"）、`Execute`、`AskUser`。`session.list_tools()` 共返回 33 个工具，包含输入、结果、进度的 JSON Schema（`fixtures/tools.json`），Phase 1 可以据此生成渲染器。

| 工具 | 输入 | 结果 `ToolResult.content` |
|---|---|---|
| LS | `{directory_path, ignorePatterns?}` | 字符串：PowerShell `Get-ChildItem` 表格，CRLF |
| Read | `{file_path, offset?, limit?, line_numbers?, image_quality?}` | 字符串（文件内容）；schema 允许 text/image/document 块数组 |
| Create | `{file_path, content}` | 字符串化 JSON：`{"success":true,"file_path":"..."}` |
| Edit | `{file_path, old_str, new_str, change_all?}` | 字符串化 JSON：`{"success":true,"file_path":"...","diffLines":[{"type":"removed","content":"hi","lineNumber":{"old":1}},{"type":"added","content":"hi there","lineNumber":{"new":1}}],"linesAdded":1,"linesRemoved":1}` |
| Execute | `{summary, command, timeout?, riskLevel}` | 字符串；取消时为 `"Error: Tool execution cancelled by user"`，`is_error=True` |
| AskUser | `{questionnaire}`：`"1. [question] ...\n[topic] ...\n[option] ...\n[option] ..."` | 字符串：`"1. [question] Which greeting word do you prefer?\n[answer] Hello"` |
| TodoWrite | `{todos}`：`"1. [in_progress] ...\n2. [pending] ...\n3. [completed] ..."` | `"TODO List Updated"` |

**diff 是否可得：可以**，有三个来源：

1. 执行前：审批请求里的 `EditAction.old_content` / `new_content` 是整个文件改前和改后的内容（实测 `"hi\n"` → `"hi there\n"`），可以本地用 difflib 生成 diff；`CreateFile.content` 是新文件内容。
2. 执行后：`Edit` 的结果 JSON 里有 `diffLines`（`added`/`removed`/`unchanged`/`context`，带新旧行号）以及 `linesAdded`/`linesRemoved`。
3. 输入里的 `old_str`/`new_str`（片段级）。

注意：如果 autonomy 设置成自动批准编辑（LOW 及以上），就不会有审批请求，只剩来源 2 和 3。`ApplyPatch`（OpenAI 系模型使用）本次没有出现，对应的 `ApplyPatchAction.files[].old_content/new_content` 未验证。另外，`Create` 会在文件末尾补换行：输入 `"hi"`，落盘是 `"hi\n"`。

Execute 的进度：只观察到一条 `ToolProgress(update.type="status", text="", terminal_id=<uuid>)`，之后就被中断了。schema 里有 `text`、`fullOutput`、`exitCode`、`signal`、`terminalId` 字段，说明支持流式终端输出，但**命令运行过程中是否持续推送输出未验证**。

## 8. PermissionRequest 与 QuestionRequest 的形状

在 `Autonomy.OFF` 下，只读工具（LS、Read、TodoWrite）不触发审批；`Create`、`Edit`、`Execute` 会触发。AskUser 不走 `on_permission`，直接调用 `on_question`（等待期间状态为 `waiting_for_tool_confirmation`）。三类审批提供的选项完全相同：

```json
"options": [
  {"label": "Yes, allow", "value": "proceed_once"},
  {"label": "Yes, and always allow low impact commands (file edits and read-only commands)", "value": "proceed_always"},
  {"label": "No, cancel", "value": "cancel"}
]
```

spike 只选 `proceed_once`，从未选会持久化的选项。`proceed_always` 的标签显示它会把自动批准范围提升到 low impact，但效果未验证。

CreateFile：

```json
{"_type": "CreateFile",
 "tool_use": {"id": "toolu_0127...", "name": "Create", "input": {"file_path": "D:\\...\\sandbox\\hello.txt", "content": "hi"}},
 "file_path": "D:\\...\\sandbox\\hello.txt", "file_name": "D:\\...\\sandbox\\hello.txt",
 "content": "hi", "confirmation_type": "create"}
```

EditAction：

```json
{"_type": "EditAction",
 "tool_use": {"name": "Edit", "input": {"file_path": "D:\\...\\hello.txt", "old_str": "hi", "new_str": "hi there"}},
 "file_path": "D:\\...\\hello.txt", "file_name": "hello.txt",
 "old_content": "hi\n", "new_content": "hi there\n", "confirmation_type": "edit"}
```

ExecuteAction：

```json
{"_type": "ExecuteAction",
 "tool_use": {"name": "Execute", "input": {"summary": "Print number 1 then pause", "command": "Write-Output 1; Start-Sleep -Seconds 3", "riskLevel": "low"}},
 "full_command": "Write-Output 1; Start-Sleep -Seconds 3",
 "command": "Write-Output, Start-Sleep",
 "extracted_commands": ["Write-Output", "Start-Sleep"],
 "impact_level": "low", "risk_level_reason": null, "confirmation_type": "exec"}
```

字段上的坑：

- `ExecuteAction.command` 不是命令本身，而是逗号连接的命令名列表；显示要用 `full_command`。
- `CreateFile.file_name` 是完整路径，`EditAction.file_name` 是文件名，两者不一致。
- `associated_session_ids` 为空；`PermissionResolved` 带 `request_id`、`tool_use_ids`、`selected_option`。

QuestionRequest 及回复：

```json
{"tool_call_id": "toolu_01Xav...",
 "questions": [{"index": 1, "topic": "Greeting", "question": "Which greeting word do you prefer?",
                "options": ["Hello", "Hi", "Hey"], "multi_select": null}]}
{"cancelled": false, "answers": [{"index": 1, "question": "Which greeting word do you prefer?", "answer": "Hello"}]}
```

- `index` 从 1 开始；单选题的 `multi_select` 是 `null`，多选题未测。
- AskUser 工具说明要求模型不要提供「自定义答案」选项，由 UI 自己提供。
- spike 的回调都是同步、立即返回的。真实 UI 需要用 async 回调等待用户操作（SDK 文档支持）；长时间等待是否有超时未验证。

## 9. 会话 id、列表与恢复

- `session.id` 在 `open()` 之后可用，和 `RunResult.session_id` 一致。
- `list_sessions(cwd=sandbox)` 只读本地文件，4.8 ms，返回 `SavedSession(id, title, owner, message_count, modified_at, created_at, cwd, is_favorite)`，按修改时间倒序。按 cwd **精确匹配**：对父目录查询返回 0 条。Windows 上会话目录名是 `-<项目路径>-sandbox`（盘符冒号被去掉，路径分隔符变成连字符），SDK 的计算和 droid 实际写入的一致。
- `Session.resume(id, runtime=..., interactions=...)`：打开耗时 1.63 s；模型（claude-haiku-5-5）、reasoning（high）、autonomy（off）和 cwd 都从存档恢复；模型确实记得上下文（回答了 `hello.txt` 和内容 "hi there"）。
- 恢复后的流**不重放历史**，只包含新一轮的消息；恢复会话的第一轮也没有 `context-` 消息。
- 高层 API 不暴露历史。已验证的替代办法：`droid_sdk.low_level.DroidClient(transport=ProcessTransport(...))` → `load_session(session_id=...)` → `result.session.messages`。本次返回 13 条线格式消息（role 分别为 user、assistant、tool；工具结果放在 `role="tool"` 的消息里，按 `tool_use_id` 关联），耗时 2.05–2.47 s（含启动进程）。第一条是 `visibility="llm_only"` 的上下文消息，里面有 6 个文本块（工具目录、技能、子代理、系统信息、日期、工作区状态）；`fixtures/history.json` 中这部分文本已省略。结果里还有 `pending_permissions`、`pending_ask_user_requests`、`is_agent_loop_in_progress`，可以用来恢复「中断前正在等待审批」的状态（未验证）。

## 10. 中断

- `session.interrupt()` 放在单独的 task 里调用（模拟停止按钮），流会以 `RunInterrupted`（`subtype="interrupted"`、`text=""`、保留 `messages` 和 `usage`）结束；底层 `agent_turn_completed.reason = "cancelled"`。会话仍然可用：同一会话的后续轮成功回复 "ok"。
- 情形 A（`fixtures/interrupt.jsonl`）：在第一条 `ToolCallDelta`（`input={}`）时中断。模型这次响应仍然完整到达（`ToolCall` + `AssistantMessage`），Execute 进入 `waiting_for_tool_confirmation`，**审批回调仍然被调用**（spike 回答了 `proceed_once`），但 droid 已经取消：`PermissionResolved(selected_option=cancel, tool_use_ids=[])`，随后两条 `ToolResult(is_error=True)`：`"Error: Tool execution cancelled by user"` 和 `"Error: Tool execution cancelled: Batch cancelled"`，最后 `RunInterrupted`。从调用 `interrupt()` 到结果约 0.09 s。
- 情形 B（`fixtures/interrupt_running_tool.jsonl`）：命令 `Write-Output 1; Start-Sleep -Seconds 3` 开始运行（收到第一条 `ToolProgress`）时中断。8 ms 后收到第一条取消 `ToolResult`，1.08 s 后收到第二条（同一 id），1.11 s 后 `RunInterrupted`，早于命令自然结束的 3 s。PowerShell 子进程在操作系统层面是否被结束未验证。
- 对 UI 的要求：点击停止后，已经弹出的审批对话框要能被 `PermissionResolved`/`RunInterrupted` 关闭；`ToolResult` 要按 `tool_use_id` 幂等（重复到达时保留第一条或最后一条，二者文本可能不同）。

## 11. qasync + Qt WebEngine

- `PySide6.QtWebEngineWidgets` 必须在创建 `QApplication` 之前导入。`qasync.QEventLoop(app)` 在 Windows 上是 `QIOCPEventLoop`，可以在 Qt 主线程里直接跑 droid 子进程的 asyncio 管道 I/O；`Session.open`、流式迭代、审批回调在 Qt 循环中都正常。
- 真实窗口（实测带模型轮次）：`setHtml` 后 `loadFinished=True`（0.03 s）；`runJavaScript("1+1")` 回调得到 `2.0`（JS 数字回到 Python 是 float）；`TextDelta` 通过 `page().runJavaScript("appendDelta(...)")` 追加，结束后 `readOut()` 读回 `"pong"`；整个进程 exit 0。
- `QT_QPA_PLATFORM=offscreen`：`--no-turn` 模式下 WebEngine 加载和 JS 往返正常（platform 为 `offscreen`）；offscreen 下跑模型轮次没有做，以节省额度。
- `runJavaScript` 只用于冒烟测试。Phase 1 计划用的 QWebChannel 未验证。

## 12. 延迟（本机实测）

| 操作 | 耗时 |
|---|---|
| `list_models()`（一次性进程） | 1.48 s |
| 新会话 `open()` | 1.44–3.85 s（6 次） |
| `Session.resume(...).open()` | 1.63 s |
| `session.list_tools(include_schemas=True)` | 0.02 s |
| `list_sessions(cwd=...)` | 4.8 ms |
| 低层 `load_session`（含启动进程） | 2.05–2.47 s |
| `session.close()` | 24–31 ms |
| `stream()` 开始到第一个事件 | 3–21 ms |
| 用户消息回显（`UserMessage`） | 约 0.2 s |
| 第一个模型可见输出（Thinking/ToolCallDelta/Text） | 2.9–9.1 s |
| Read / LS（PowerShell）/ 审批后的 Create、Edit | 约 20 ms / 约 0.3 s / 约 6 ms |
| 审批回调返回到 `PermissionResolved` | < 1 ms（同步回调） |
| `interrupt()` 到 `RunInterrupted` | 0.09 s（等待审批时）/ 1.11 s（命令运行中） |
| 整轮 | 5.4 s（AskUser）到 25.3 s（5 次 LLM 调用、8 次工具调用） |

用量：每次 LLM 调用都会读约 17.5k 个缓存 token 的系统上下文。每轮的 `factory_credits` 在 93 到 832 之间（原始数值，单位没有文档说明，未验证）。

## 13. 遇到的问题与绕过

| 问题 | 绕过 |
|---|---|
| PySide6 下载慢，前台安装会超过工具调用时限 | 中止后改为后台安装；已下载完的 wheel 走 pip 缓存 |
| 管道输出编码报错风险 | `sys.stdout.reconfigure(encoding="utf-8")` |
| `Session.resume` 没有 cwd | 恢复前 `os.chdir(会话目录)` |
| 高层 API 拿不到历史 | 低层 `DroidClient.load_session()`（`spikes/history_probe.py`） |
| `tool_execution_phase_changed`、消息 `visibility` 被高层流丢弃 | `session.on_notification(...)` 订阅原始通知；或按 `context-` id 前缀过滤 |
| `ExecuteAction.command` 不是真实命令 | 使用 `full_command` |
| 中断后审批回调仍被调用、`ToolResult` 重复 | UI 状态机做幂等；以 `PermissionResolved`/`RunResult` 为准 |
| `reasoning=low` 时 thinking 时有时无 | 需要稳定录到 thinking 时用 high（`stream_turn.py` 的默认值） |
| 第一次 stream_turn 没有覆盖 Edit | 在 prompt 里加了 Edit 步骤后重录；第一次的录制保存为 `stream_turn_reasoning_low.*` |

spike 新建了 6 个会话（都在 sandbox 工作区下，id 略）。

## 14. 未验证事项与 Phase 1 风险

未验证：

- Execute 运行过程中是否通过 `ToolProgress` 持续推送 stdout（只看到第一条 status 事件）。
- `ApplyPatch` 和多文件编辑的形状（OpenAI 系模型）。
- 审批或提问长时间等待用户时的行为与超时；async 回调。
- `ErrorEvent`、`RunFailure`（例如无效模型 id）、`HookExecution`、`SettingsUpdated` 的实际形状。
- 关闭会话时 Execute 的子进程是否被清理（`TerminateProcess` 不会结束子进程树）。
- 桌面版更新删除旧 `app-*` 目录时，正在运行的会话会怎样。
- 每个 `droid.exe` 进程的内存占用，以及多个会话并发时的表现。
- `factory_credits` 的单位。
- spike 会话是否出现在 Factory 桌面版的会话列表中。
- offscreen 模式下跑模型轮次；QWebChannel 桥接。
- Spec 模式、compact、rewind、fork 都没有测试。

对 Phase 1 的影响和建议：

1. **事件归约器**：tool 卡片按 `tool_use_id` 归约，状态为「输入生成中 → 定稿 → 待审批 → 运行中 → 完成/出错/取消」，要能处理重复和乱序；文本和 thinking 按 `(message_id, block_index)` 归约；`AssistantMessage` 到达后再把工具卡片挂到对应消息下。用 `fixtures/*.jsonl` 回放做单元测试。
2. **审批与提问 UI**：用 async 回调等待一个由 UI 完成的 `asyncio.Future`；选项按 `request.options` 动态渲染（使用 label）；编辑审批直接展示 `old_content → new_content` 的 diff；对话框必须能被 `PermissionResolved` 或 `RunInterrupted` 从外部关闭。
3. **Thinking 展示**：用 `WorkingStateChanged` 显示「思考中」，thinking 文本成批到达后放进可折叠块。
4. **历史与会话列表**：列表用 `list_sessions(cwd)` 或 `all_workspaces=True`；打开历史会话时先用低层 `load_session` 取消息（过滤 `visibility="llm_only"`），再 `Session.resume()` 交互。也可以考虑自己保存一份 transcript。
5. **进程与路径**：每个会话一个 `droid.exe`，每次打开会话时重新解析 exe 路径；退出应用时逐个 `await session.close()`。
6. **上下文和成本**：用户全局的 AGENTS.md 规则、技能、子代理等会进入每个会话（每次调用约 17.5k 个缓存 token）。可以评估 `SessionConfig(disable_builtin_skills=True)`、`restrict_tools`/`disabled_tools`、`system_prompt` 对体验和成本的影响。
7. **打包**：PySide6 全量 647 MB，发行版需要裁剪 Qt 模块（未评估）。
8. **线程模型**：全部放在 qasync 循环（Qt 主线程）上；回调和 UI 代码不能阻塞事件循环。

## 15. 文件清单与重跑方法

所有命令都在项目根目录下用 venv 的 Python 运行：`.\.venv\Scripts\python.exe spikes\<脚本>.py`。

| 文件 | 说明 | 模型轮次 |
|---|---|---|
| `spikes/find_droid.py` | 查找最新的自带 droid.exe，支持 `WITTYCODE_DROID_EXECUTABLE` 覆盖 | 0 |
| `spikes/_common.py` | 公共代码：路径、Runtime、JSONL 录制器与通用序列化（dataclass/pydantic/enum/Path，脱敏）、时间线、sandbox 审批策略、默认模型（可用 `WITTYCODE_MODEL`、`WITTYCODE_REASONING` 覆盖） | - |
| `spikes/list_models.py` | 列出模型并写 `fixtures/models.json` | 0 |
| `spikes/stream_turn.py` | 3b；默认 `--reasoning high`，`--name` 改 fixture 名，同时写 `fixtures/tools.json` | 1 |
| `spikes/ask_user.py` | 3c | 1 |
| `spikes/resume.py` | 3d；默认从 `fixtures/stream_turn.jsonl` 读取会话 id，也可 `--session-id` | 1 |
| `spikes/interrupt.py` | 3e；`--trigger`（逗号分隔：any、ToolCallDelta、ToolCall、ToolProgress、ToolResult）、`--name`、`--no-follow-up` | 2（加 `--no-follow-up` 为 1） |
| `spikes/qasync_smoke.py` | 3f；`--no-turn` 只测 WebEngine；设置 `QT_QPA_PLATFORM=offscreen` 可无窗口运行 | 1（`--no-turn` 为 0） |
| `spikes/history_probe.py` | 额外：低层 `load_session` 取历史，写 `fixtures/history.json` | 0 |
| `fixtures/stream_turn.jsonl`、`.raw.jsonl` | 3b 录制（high reasoning，含 Edit） | |
| `fixtures/stream_turn_reasoning_low.jsonl`、`.raw.jsonl` | 第一次 3b 录制（low reasoning，无 thinking，无 Edit，prompt 为原始版本） | |
| `fixtures/ask_user.jsonl`、`.raw.jsonl` | 3c 录制 | |
| `fixtures/resume.jsonl`、`.raw.jsonl` | 3d 录制（含 `ListSessions` meta） | |
| `fixtures/interrupt.jsonl`、`.raw.jsonl` | 3e 录制（中断轮 + 后续轮） | |
| `fixtures/interrupt_running_tool.jsonl`、`.raw.jsonl` | 3e 补充录制（命令运行中中断） | |
| `fixtures/qasync_smoke.jsonl` | 3f 录制 | |
| `fixtures/models.json`、`fixtures/tools.json`、`fixtures/history.json` | 模型目录、工具目录（含 schema）、历史消息 | |

录制格式：每行一个 JSON 对象，形如 `{"seq", "t"（距录制开始的秒数）, "turn", "turn_t"（距本轮开始的秒数）, "source": "stream" | "handler" | "meta", "type": 类名, "data": 字段}`。dataclass 和 pydantic 对象带 `_type`，枚举存其值，`timedelta` 存秒数，`datetime` 存 ISO 字符串；`signature`、`thought_signature`、加密内容等不透明数据替换为 `<elided N chars>`；密钥类字段名和疑似密钥的字符串会被替换为 `[REDACTED]`（本次没有命中）。`source="handler"` 的记录是审批或提问回调的请求、策略判断和回复，按发生时刻插在流事件之间。`*.raw.jsonl` 是原始线格式通知（camelCase）。

注意：重跑 `stream_turn.py` 会覆盖 `fixtures/stream_turn.*`，并生成新的会话 id，`resume.py` 和 `history_probe.py` 会随之使用新会话。

本次共用了 8 次模型轮次，全部使用 `claude-haiku-5-5`：stream_turn 2 次、ask_user 1 次、resume 1 次、interrupt 2 次（中断轮 + 后续轮）、interrupt_running_tool 1 次、qasync_smoke 1 次。