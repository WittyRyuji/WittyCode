# 自有 Harness 第二次审阅（2026-11-06，修复后）

## 1. 结果

**上一批修复改善了原始复现，但 R1、R2、R6 只能算部分修复；R3 的重开恢复有效，运行中的停止超时/续轮边界仍有缺陷。本轮不修改业务代码，也不把原报告中的所有问题重新算成“新问题”。**

本次重新读取了 Harness、ChatGPT 控制器/HTTP 和新增回归测试，运行了相关测试及全量测试；重点是修复后的边界，不是再做一轮竞品功能清单。

| 编号 | 优先级 | 发现 | 证据 |
| --- | --- | --- | --- |
| F1 | P1 | 末段通配符 + 管道仍能免审批读取保护目录 | 真实目录联接 + PowerShell；仅测试哨兵 |
| F2 | P1 | 临时文件写好后取消，等待 5 秒返回，随后仍替换原文件 | 真实 Edit，确定性挂起临时文件阶段 |
| F3 | P1 | 停止超时后开始新轮，把未配对调用发给模型；保存时轮边界也可能错位 | 真实控制器 + 假流/等待工具；快照检查 |
| F4 | P1 | `git status` 白名单允许执行仓库配置的 fsmonitor 命令 | 独立临时 Git 仓库，真实 Git 和 shell |
| F5 | P1 | 文件路径或 shell cwd 是 UNC 时，审批前就尝试解析网络路径 | 入口 spy 拦截；没有连接真实 SMB |
| F6 | P2 | 纯文字 grep 的 glob 过滤仍在主进程做回溯正则 | 实测事件循环延迟 0.714 秒 |
| F7 | P2 | 停止发生在连接建立前，延迟清理退出后出现的响应不再关闭 | 假 opener + 真实 HTTP 线程/取消代码 |
| F8 | P2 | 当前全量测试不绿：项目规则测试仍要求旧的“不提交” | 全量 2074 passed / 1 failed / 1 skipped |

P1 指需要优先修复的安全/状态一致性问题，不表示无条件触发。各项触发条件和复现边界见第 4 节。没有访问真实凭据、调用真实模型或强杀用户应用。

## 2. 本轮改动

- 新增本报告，在原报告追加复审入口，更新 `docs/handoff.md`。
- 没有修改业务代码、正式测试、前端或依赖；没有改 Git 身份或暂存区。
- 开工时仓库没有 HEAD，已有整仓初始提交的暂存内容。本轮后续只读检查仍显示 `user.name` / `user.email` 未配置；审阅期间另有 `core/memory/launch.py`、AppCore、settings、UI app 等业务文件出现并行修改，未触碰。测试结果针对本次执行时加载的版本，不声称覆盖随后并行编辑的内容；本报告所引用的 Harness/ChatGPT 路径未出现这些未暂存改动。没有把他人暂存内容当作自己的提交。

## 3. 对上一轮结论的修正

| 原项 | 本轮复核结论 |
| --- | --- |
| R1 直接相对路径/目录联接 | 新增回归覆盖的直接路径已拒绝；不能据此说保护目录问题已整体关闭，F1 仍在 `code` 档免审批通过。运行时拼路径在 full 下无法强隔离是已知限制，不作为新发现重复报告。 |
| R2 取消后线程写入 | `_plan_edit` 挂起窗口和“下一轮换 abort”已修；临时文件阶段到 `os.replace` 的窗口仍漏，见 F2。文件锁、临时写和正常成功路径是实质改进。 |
| R3 崩溃后的历史 | 正常重开会补 LOST、标 interrupted，不自动重跑工具；这部分测试通过。不能再说“重开不修复”。残留的是运行中停止超时及快照索引，见 F3。 |
| R5 规则截断 | **本仓库末尾规则已经进入提示**，实测指令正文 22,343 字符，含 `## 规则` 和 `git commit`。祖先文件也会合并；仍未实现按具体目标文件动态加载子目录规则，且大子文件会占满预算、父规则被提示为未加载。这与“已实现完整分层规则体系”不同。 |
| R6 搜索隔离 | 主搜索模式的回溯确实已隔离；glob 过滤的正则没有纳入隔离条件，见 F6。 |
| R8 limit / incomplete | 存档原因和提示已保留；界面/运行时仍把它们映射成 DONE，这是当前测试明确规定的行为，不再把“提示丢了”当成未修。任务验收语义仍是设计待办。 |
| R4 / R7 / R8 其余 / R9 / R10 | 上下文压缩/预算、调度、验收和重试、工具缺口、提供方抽象仍未实现，未发现上一批声称已做。不需要把这些设计项包装成本次新回归。 |

两点措辞纠正：

1. 原报告说“重试可能重复副作用”，不应被理解成控制器存在自动重放工具的路径；当前没有这条路径。风险在于结果未知时，模型或用户再次发起同一操作。新 LOST 提示对此有帮助。
2. “OAuth 令牌不写进环境变量”只能说明该令牌的存放方式，不能推出“继承宿主环境不存在风险”。本轮只给测试进程加一个无敏感内容的环境哨兵，`code` 档的 `Get-Content Env:WITTYCODE_REVIEW_MARKER` 被允许且读出它。没有证明真实 OAuth/API 凭据泄漏，也没有读取真实环境值；宿主其他 API 环境变量仍需单独确定暴露策略。

## 4. 具体发现

### F1 · P1 · 末段通配符和管道仍绕过保护目录【已复现】

位置：`desktop/core/harness/shell.py:337-345, 349-364, 367-384`；`desktop/core/harness/permissions.py:213-225, 228-283`。

`_fixed_part("linked*")` 把末段通配符缩为当前目录；`_wildcard_matches` 只展开“后面还有路径段”的通配符。于是参数被视为指向项目内部，既不被拒绝，也不要求确认。管道传入下一个 cmdlet 的实际路径没有检查。

最小复现：在测试项目内建立 `linked-private` 目录联接，目标是项目外且登记到 `ToolContext.protected` 的目录，里面只有哨兵文本。

```powershell
Get-ChildItem -Recurse linked* | Get-Content
```

结果：`code` 判决 **allow**，shell 实际返回哨兵，且没有工具错误。换成 `Get-ChildItem linked* -Recurse | Select-String REVIEW_SENTINEL` 同样能读到。

对照：`Get-ChildItem -Recurse . | Get-Content` 没有读到该哨兵。因此上一批“从项目根普通递归不跟目录联接”的观察仍然正确，但不能外推到“把联接作为通配符匹配出的起始目录”。

建议：检查实际通配符展开后的目标，无法可靠确定时保守询问/拒绝；不能只检查固定前缀。管道对象和命令副作用也需独立信任边界。长期仍需操作系统级执行隔离，字符串识别不能宣称封死所有路径。

### F2 · P1 · 取消检查在临时写之前，而不在最终提交处【已复现】

位置：`desktop/core/harness/fs_tools.py:852-878, 899-909`；`desktop/core/harness/tools.py:139-156`。

`_write` 先检查 abort，然后 `_temporary_beside` 写临时文件，直接进入 `_replace`；中间没有再次检查。`_replace` 的三次重试也不检查 abort。后面的取消检查只保护原地写回退。

探针让真实 `_temporary_beside` 写完临时文件后等待，随后取消 registry 任务。使用真实的 `THREAD_WAIT_SECONDS=5`，没有缩短这个等待：

- 取消在 **5.016 秒**后返回；abort 已置位，目标仍是 before。
- 释放后台线程后，目标变成 **after**。

所以“线程超过 5 秒后即使回来也写不了”的说明不成立。旧测试主要挂在 `_plan_edit`，还没跨过 `_write` 开头的检查，抓不到这个窗口。

建议：在临时文件完成后和每次重试前检查，并把取消与最终提交的状态转换同步起来。已经进入不可抢占系统调用的提交必须被记录为完成或结果未知，不能一边宣布停止完成、一边允许新轮修改同一资源。仅再加一次无锁检查也不应被表述为绝对安全保证。

### F3 · P1 · 停止超时后仍允许新轮使用旧循环共享状态【已复现】

位置：`desktop/core/chatgpt/controller.py:562-581, 381-408, 609-619, 707-713`；`desktop/core/harness/loop.py:283-299`。

`interrupt` 等宽限时间到后调用 `_end_turn`，控制器进入 READY，但旧 `_turn_task` 可能尚未结束。下一条消息可以立刻启动，继续使用同一份 `saved.items`。只有 `_snapshot` 和重开恢复做配对，**发请求前没有修复或阻止这份运行中历史**。

确定性探针用一个收到取消后仍在清理的假异步工具，将控制器宽限缩为 0.08 秒（模拟真实 8 秒后仍未清理完的同一分支；不是真实卡死模型）：

1. 停止返回时状态 READY，旧循环仍活着。
2. 新轮请求中仍带 `old-call`，没有对应 output。
3. 旧循环随后才追加中断结果；内存顺序变成：旧用户、旧 call、新用户、新回答、旧 output。

还有持久化的第二层问题：`_snapshot` 用 `pair_calls` 挪动/插入输出，却没有重映射 `SavedTurn.start`。对上面的真实可达顺序，快照里新用户从索引 2 移到 3，但第二轮 start 仍为 2，指向 `function_call_output`。重开后轮边界可能错位。

建议：旧执行未结束时不应无条件 READY；或明确切断旧运行的状态所有权。为 Observer 绑定 turn/generation ID，防止旧流写新轮；模型请求和快照分别保证配对及索引一致性。不能只补磁盘历史、让内存继续错序。

### F4 · P1 · `git status` 的“只读”判定忽略仓库配置的执行行为【已复现】

位置：`desktop/core/harness/permissions.py:174, 235-248, 345-348`；`desktop/core/harness/shell.py:81-94`。

触发前提：仓库配置了命令式 `core.fsmonitor`。Git 执行 status 时会调用它，而权限检查只认命令文本 `git status`，不约束 Git 的隐式执行。

在独立临时仓库里，用真实 Create 工具写 `.git/config`，让 fsmonitor 只写一个无害标记文件；再通过真实 shell 工具执行 `git status --short`：

- `.git/config` 的创建/覆盖在 `code` 档被允许。
- `git status --short` 在 `code` 档被允许。
- 标记文件实际生成，说明配置中的命令确实执行了。

不要求用户预先做过一次“本会话允许执行命令”。这绕过的是“code 档执行非只读命令需要确认”的承诺，不是说每个普通仓库的 status 都有恶意副作用。

建议：不要只按 Git 子命令命名认定只读。为免审批 Git 操作禁用/控制外部程序、textconv、fsmonitor 等配置影响；保护 `.git` 中可改变执行行为的配置/钩子；或在真正的隔离执行环境运行。对其他白名单程序也要按实际能力分析。

复现过程说明：最初几种 `.cmd` 路径写法受 Git shell/Windows 引号差异影响，没有生成标记，不能作为成功证据；最终换成配置中的简单 shell 命令后才确认。没有改变本仓库的 Git 配置，也没有提交测试仓库。

### F5 · P1 · UNC 路径在审批前被解析【调用入口已确认，网络后果未实测】

位置：`desktop/core/harness/fs_tools.py:95-109`；`desktop/core/harness/permissions.py:88-96, 135-147`。

命令**参数**的 `resolved_argument` 会提前拦 UNC，但文件工具的 `file_path` 和 shell 的 `cwd` 仍经 `_locate → resolve_path → Path.resolve`。Windows 对 UNC 的真实路径解析可能触发网络访问/身份验证，因此最后返回 ask 已经太晚。

为避免实际联网，探针在 `Path.resolve` 入口拦截 `\\review.invalid\share`，直接记录并抛出 OSError：

- `decide(code, read_file, file_path=UNC)` 返回 ask 之前，发生 **2 次** UNC resolve 尝试。
- `decide(code, shell, cwd=UNC)` 返回 ask 之前，发生 **3 次** UNC resolve 尝试。

这里确认的是“审批前会调用网络路径解析入口”；没有联系 SMB 服务器，没有抓取或泄漏真实 NTLM 数据。上一批 UI 文件链接的 UNC 防线不覆盖这条代理工具链。

建议：在所有工具的公共路径入口，先纯词法检查 UNC/设备路径/网络 provider，再做任何 `resolve/stat/exists`；若支持远程工作区，应有明确的独立授权流程，而不是为问用户先访问一次。

### F6 · P2 · glob 过滤的回溯没有被隔离【已复现】

位置：`desktop/core/harness/fs_tools.py:528-571, 641-644, 672-679`。

是否进子进程只取决于 `pattern` 是否包含正则元字符。然而 `glob_pattern` 会被 `_glob_regex` 转成 Python 回溯正则，并在同一搜索路径内执行。

探针：文件名为 28 个 a 加 `.txt`，内容为 needle；调用 `grep(pattern="needle", glob_pattern="a*" * 12 + "b")`。内容模式是纯文字，所以完全不进入隔离搜索进程。实际耗时 **0.734 秒**，预先安排的事件循环心跳额外延迟 **0.714 秒**。

探针自身放在带 12 秒硬超时的独立 Python 进程里，避免把运行中的应用卡死；进程内部运行的仍是当前 registry 和 grep 实现。当前正则子进程的 10 秒超时不覆盖这条分支。

建议：把过滤器及其展开计算纳入隔离/预算，或者使用不会产生这种回溯的 glob 匹配实现。不能只看正文搜索模式是否含正则。

### F7 · P2 · 连接尚未建立时停止，延迟清理存在空窗【已复现于假连接】

位置：`desktop/core/chatgpt/http.py:140-145, 165-178`。

取消时若 `opened` 为空，会启动 `_drop_later`，只轮询 200 次、每次 50 ms，名义上约 10 秒；连接/读取超时默认却是 120 秒。轮询结束后连接才返回，工作线程没有持久的 cancelled 标记，仍会开始读响应，既不关闭它，也没有消费者再读队列。

用不联网的假 opener 阻塞连接，运行真实取消和延迟清理代码；等清理线程结束后才放开连接。观察：响应打开，**未被关闭，读取线程仍在运行**。在本次机器负载下，清理轮询实际用时 12.31 秒。没有发真实请求，不能宣称实测消耗了模型额度。

建议：用持续有效的取消事件；worker 一旦拿到响应先检查取消状态、立即关闭，并在自身 finally 收尾。不要用有限时长的旁路轮询代替生命周期所有权；同时为队列提供有界背压。

### F8 · P2 · 规则测试的断言已落后于用户约定【真实测试失败】

位置：`tests/test_harness_prompt.py:138-145`。

测试仍要求 `"不提交" in kept`，但当前 AGENTS 明确要求每次改动本地 `git commit`。实际提示保留了规则节及新提交要求，因此这是测试绑定旧文案，而不是 R5 修复失效。

建议：用独立夹具验证“尾部规则未被截掉”，对本仓库的检查验证当前规则节的内容/保留范围，不锁定已废弃的“不提交”。本轮只审阅，没有偷偷改测试让结果变绿。

## 5. 验证

### 全量（实际运行）

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

**2074 passed / 1 failed / 1 skipped，276.66 秒，退出码 1。** 唯一失败为 F8；跳过是 Windows 上无意义的 POSIX 权限位测试。整机 e2e 包含在全量测试内，但本轮没有另做 e2e 循环、真实窗口冒烟或前端构建。

### 定向（实际运行）

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_harness_loop.py tests/test_harness_tools.py tests/test_harness_permissions.py tests/test_harness_shell.py tests/test_harness_toolset.py tests/test_harness_mcp.py tests/test_harness_store.py tests/test_harness_prompt.py tests/test_harness_security_cancel.py tests/test_harness_security_grep.py tests/test_harness_security_links.py tests/test_chatgpt_responses.py tests/test_chatgpt_controller.py tests/test_chatgpt_http.py tests/test_chatgpt_engine.py tests/test_chatgpt_recovery_crash.py tests/test_chatgpt_recovery_history.py -q -p no:cacheprovider
```

**643 passed / 1 failed / 1 skipped，83.76 秒，退出码 1。** 同一个 F8 失败。

离线复现通过临时 Python 程序运行，除 HTTP 的纯内存假对象外，数据都在 `sandbox/` 自动清理目录内。没有把探针加入正式测试；修复时应将这些边界转成回归测试。取消与超时探针是确定性放大时序窗口，不是生产发生率测量。

本轮没有重新联网核对竞品文档，没有调用真实模型，也没有做真实服务对畸形历史的拒绝测试；对照架构来源仍见第一份报告。已重新读取的代码和上述本地行为才是本次结论依据。

## 6. 建议下一步

1. **先修 F1–F5**：不是继续往黑名单加几个字符串，而是把路径预检、隐式程序执行、取消提交和运行状态所有权作为明确合同。
2. **补齐 F6/F7 的剩余生命周期**：搜索模式和过滤器一起隔离；HTTP worker 持久响应取消。
3. **修 F8 并补本轮回归场景**：原始复现过关不能替代对修复后新边界的测试。之后再跑全量。
4. **再推进原 R4/R7 等设计项**：EventLog / PromptView / ArtifactStore 分离、上下文预算/压缩、有限并发和长任务工具。不要为了追求功能数量，先把上述缺陷扩散到子代理。

本轮仅记录问题，全部 F 项尚未修复。未本地提交：现有测试失败，Git 身份也未配置；未改身份、未提交或重置他人的初始暂存内容，未推送。


## 7. 处理结果（2026-11-07，另一次会话）

用户要求“另一份审计日志，需要再 review 修改”。八项逐条先复现、再修，复现都变成了回归测试，并用变异检查确认测试真的在守着。复审正文保持审阅当时的样子，下面是处理结果；机制和限制在 `AGENTS.md` 的「自有 Harness 审阅后的修复」「上下文窗口与压缩」两条里。

| 编号 | 核对结果 | 处理 |
| --- | --- | --- |
| F1 末段通配符 + 管道读保护目录 | **成立**。复现：`Get-ChildItem -Recurse linked* | Get-Content`、`cat linked*`、光秃秃的 `cat *` 在 `code` 和 `full` 都被放行，真实 shell 打出了哨兵。**审阅漏掉的更糟的洞**：联接在项目的下一层时，`cat */linked*/*` 在 **`full` 也读得到**，因为只展开了第一个通配符段 | **已修**：`shell.py` 的 `_wildcard_targets` 展开**每一个**通配符段（含最后一段），每个匹配先解析再列下一层，路过的文件夹也算目标；预算 256 次列目录 / 5000 个目标，用完或被截断就算“未知”（`code` 会问）。任何通配符走到指向受保护文件夹的链接，所有档位直接拒绝；`rg -L` / `--follow` 和 `where /r` 不再算只读。**又补了一个洞（复审也没写，修复的 worker 发现的）**：递归命令会走进项目深处**没有任何参数点名**的链接，`Get-ChildItem -Recurse -Include *.txt | Get-Content`、`findstr /s` 在 `code` 档照样读到哨兵（在这台机器上用真实的嵌套联接复现）。现在“递归列表 + 管道进读内容的命令（`Get-Content` / `cat` / `type` / `Select-String`）”和 `findstr /s` 在 `code` 档要问；只列文件名的递归列表仍然放行。**仍有的限制**：预算之外 `full` 档不检查未知目标，和“运行时才拼出的路径”同一类；指向 UNC 的项目内符号链接在判定时仍会被解析（早就有的） |
| F2 临时文件写好后取消，仍替换原文件 | **成立**。复现：真实 5 秒等待后取消返回，`abort` 已置位、文件还是 before，线程放开后变成 after | **已修**：`tools.py` 每次调用一个 `CommitGuard`（带锁）：`stop()` 对还没开始的写一律拒绝，已经开始的写返回 False 并记一条警告；`fs_tools.py` 里三次改名重试和原地写回退各自是一次“被守卫的提交”。同一个探针现在文件留在 before、没有留下临时文件。**限制**：已经进了 `os.replace` 的写收不回，循环里原有的“结果未知”说明仍然成立 |
| F3 停止超时后旧循环仍占着历史 | **成立**，两层都复现了：新一轮的请求带着没有输出的调用；`_snapshot` 配对后 `SavedTurn.start` 没跟着移 | **已修**：`_Watch` 绑定所属的轮，`ctl._turn is not owner` 时它的一切报告都丢弃；`interrupt()` 放弃一轮时立刻给缺输出的调用补“用户中断了”；`store.paired()` / `pair_in_place()` 配对后同时搬动轮的起点和压缩的边界。回归测试用一个停止后还拖 0.8 秒才收尾的工具 |
| F4 `git status` 白名单忽略仓库配置的执行行为 | **成立**（git 2.50.1 实测）：`.git/config` 和 `.git/hooks/*` 在 `code` 档可无询问地写，配置的 `core.fsmonitor` 在 `git status` 时真的跑了 | **已修**：`shell.py` 的 `_git_env` 给每条命令前置 `GIT_CONFIG_*`，让 `core.fsmonitor`、pager、askPass、gitProxy、alternateRefsCommand、packObjectsHook 无害；`permissions.py` 的 `git_runs_own_programs` 读仓库自己的配置（含 worktree 和 `gitdir:`），配置里指了程序，`code` 档的只读 git 命令要问并点名；写进 `.git` 在 `code` 档要问。**限制**：`diff.external`、textconv / filter、`gpg.program`、`core.hooksPath`、`credential.helper` 没有无害的取值，所以是问而不是中和；系统和全局配置不读；`git lfs install --local` 过的仓库每个会话要问一次 |
| F5 UNC 路径在审批前被解析 | **成立**：七个入口都在判定返回前解析了 `\\host\share`。**审阅漏掉的**：`\\?\C:\…\<受保护>\settings.json` 能读，因为解析保留了 `\\?\` 前缀，受保护检查没认出来 | **已修**：`fs_tools.py` 的 `is_remote_path` 在 `resolve_path` 里、任何解析之前只看文字；`permissions.py` 的 `decide` 对这样的路径参数所有档位都拒绝（MCP 工具也是）。**限制**：命令**文字**里提到 UNC 路径的，`code` 档仍然问、`full` 放行（判定时不解析、不列目录） |
| F6 glob 过滤的回溯没有被隔离 | **成立**：审阅的写法卡事件循环 0.73 秒。**审阅漏掉的**：写 40 次 `{a,b}` 要展开 2^40 种，卡 240 秒以上，而同一段代码在权限判定里也会跑 | **已修**：`_glob_matcher` 是不用正则的线性状态机；`expand_braces` 预算 2000 种展开、1000 个字符，超了就把模式当字面量（什么也匹配不上）。事件循环最多停 0.013 秒，大括号炸弹立即返回。**语义变化**：只有一个路径段内的 `**` 与旧正则不同，新的和 ripgrep 一致 |
| F7 连接未建立时停止，延迟清理有空窗 | **成立**：假连接上，清理线程退出后连接才回来，响应打开、没被关、读线程还在跑 | **已修**：`http.py` 新的 `_StreamCall` 拥有一个请求的生命周期：持久的 `cancelled` 标志，worker 在连接前、拿到响应的那一刻、每行之间、等队列空位时都看它；晚到的响应由 worker 自己不读就关；`_drop_later` 删掉了，不再有会“耐心用完”的旁路线程；队列有界（256 行），消费者停住 worker 就停读。顺手收紧：错误体被截断（`IncompleteRead`）以前会让读者永远等下去，现在是网络错误。**限制**：`opener.open()` 不能被打断，一个永远连不上的连接会占一个守护线程到 120 秒空闲超时，它不持有响应，到的任何东西都被关掉 |
| F8 规则测试断言落后于用户约定 | **成立** | **已修**（上一批）：测试改成断言“## 规则”和“不推送”在保留的提示里 |

另外，原报告的 **R4 上下文压缩**这次也做了（见 `harness-review-2026-11-06.md` 第 7 节和 `AGENTS.md` 的「上下文窗口与压缩」）。起因是用户撞上了：从 GPT-6.1-Sol 换到窗口更小的 GPT-6-Astra，状态栏 317K / 272K，请求照发、不压缩。

**复审之外又补的一处**：F1 的修复者发现，递归命令会走进项目深处**没有任何参数点名**的链接（`Get-ChildItem -Recurse -Include *.txt | Get-Content`、`findstr /s x *.txt`；在这台机器上用真实的嵌套联接复现，哨兵被读出）。这与 F1 同一个威胁模型（例如克隆了一个带指向 `%APPDATA%\WittyCode\chatgpt` 的符号链接的仓库），所以也收了口：“递归列表 + 管道进读内容的命令”和 `findstr /s` 在 `code` 档要问，只列文件名的递归列表仍放行；`rg` 默认不走联接（用真实联接测过），`rg -L` 本来就要问。代价：模型用 `findstr /s` 搜代码会弹一次确认（它有 `grep` 工具可用），三份旧测试把它当“普通只读命令”，已改期望。

**验证**：全量 pytest 2728 passed / 1 skipped；变异 `sandbox/mutate_harness_rereview.py` 94/94（修复者的 85 个加我补的 9 个）、`sandbox/mutate_http_cancel.py` 27/27（F7）；F3 的回归测试在 `tests/test_chatgpt_compaction.py`（用一个停止后还拖 0.8 秒才收尾的工具）。**没有做的**：真实账号上的任何验证（F2 F3 F7 都是用假服务和假连接确定性地放大时序窗口，不是生产发生率）；`full` 档对运行时才拼出的路径和预算之外的通配符仍然挡不住；已经进了 `os.replace` 的写收不回。