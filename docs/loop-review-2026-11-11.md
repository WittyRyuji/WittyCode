# WittyCode 自有代理循环与本地 harness：怎么跑的，和审阅（2026-11-11）

范围：`core/harness/`（循环、工具、权限、压缩、存档、提示）、`core/chatgpt/controller.py` 和 `engine.py`（一个会话的生命周期）、`core/llm/`（各家接口的适配器，含这次新做的 Bedrock）。以前的几次审阅（`harness-review-2026-11-06.md`、`harness-rereview-2026-11-06.md`、`harness-review-2026-11-08.md`）管的是安全和压缩的正确性，这一份看的是整体流程和健壮性，**以读代码为主**；标了「已验证」的才是跑过的。

## 1. 一轮对话是怎么走的

```
输入框 ──> AppCore ──> ChatGPTController（一个会话一个，状态 READY / RUNNING）
                          │ send_prompt：hook（记忆、技能）→ 起一个任务 _run_turn
                          ▼
                       AgentLoop.run(view)            view = [摘要] + 日志尾部
                          │ 每一步：
                          │  1. 把用户中途插进来的话（steer）放进历史
                          │  2. Observer.prepare：估算 ≥ 窗口×阈值就压缩（compact.py）
                          │  3. 请求：stream(request) → 一串中立事件
                          │  4. 事件边来边交给 Observer（_Watch：写页面、写日志、记用量）
                          │  5. 有工具调用：逐个 check → 要问就等用户 → run → 结果裁剪 → 追加 function_call_output
                          │  6. 没有工具调用（且没有新的 steer）= 这一轮结束；最多 80 步
                          ▼
              stream = ChatGPTEngine.stream
                          ├─ ChatGPT 套餐：Responses API，OAuth 令牌（accounts.py，快到期才续）
                          └─ API 模型：ApiBackend → chat.py / anthropic.py / responses_api.py / bedrock.py
```

- **循环只认一种语言**：Responses 风格的 `items` 加中立事件（`TextDelta … Finished`）。每家接口一个适配器，把请求翻成它的线格式、把它的流翻回事件，所以循环、工具、压缩、存档不知道谁在回答。
- **两份列表**：`SavedSession.items` 是日志（页面和文件靠它，只增不减），模型读的是视图（摘要 + 压缩点之后的日志）。循环往视图里加的东西经 `_Watch.history_grew` 同步追加到日志。
- **不变量**（`loop.py` 文档字符串，有测试）：历史里每个 `function_call` 在下一次请求之前都有 `function_call_output`（被停时补「用户中断了」）；工具不会把异常抛进循环，失败就是结果；模型写的参数先校验是 JSON 对象。
- **`/goal`** 不在循环里：`AppCore` 在一轮结束后看最后一行标记，决定再发一条消息开下一轮（所以每一轮都是一个完整的 `_run_turn`）。
- **`/compact`、自动压缩**：`compact.py` 找切点、渲染旧部分、用同一个模型写摘要；失败时自动压缩退回「用户说过什么」的清单，手动压缩则整件事失败、对话不动。

## 2. 本地 harness：工具怎么在本机跑

| 工具 | 怎么跑 | 限制 |
| --- | --- | --- |
| `read_file` `list_dir` `glob` `edit_file` `create_file` | 线程里同步执行（`asyncio.to_thread`），被停时设 `abort`，写入用 `CommitGuard`（停在写之前就不写，写已开始就写完） | 读有大小/行数上限；写是临时文件加 `os.replace`；项目外要问 |
| `grep` | **单独的 Python 子进程**（`python -c …`），请求和答案走临时 JSON 文件；父进程到时间就杀 | 超时、结果行数、单行字符数 |
| `shell` | Windows PowerShell 5.1；命令放在环境变量 `WITTYCODE_SHELL_COMMAND` 里，由固定的一行引导脚本取出并清掉；输出合并、UTF-8 | 超时（默认 120 秒，最多 600），停止时 `taskkill /F /T` 杀整棵进程树，只留头尾各 64 KiB |
| `todo_write` `skill` `ask_user` | 内存里完成，不碰文件系统（`skill` 读技能文件夹） | 每档权限都放行；Plan 模式也行 |
| MCP 工具 | 最小的 stdio MCP 客户端（`mcp.py`），共享记忆就是这样接进来的 | 服务器自称无害的才免问；Plan 下只放行无害的 |

- **权限**（`permissions.py`，约 830 行）：三档 `ask / code / full`；只读命令白名单（还要查 Git 的配置会不会让它运行仓库自己的程序）；受保护路径（应用自己的数据文件夹）任何档都拒；项目外的路径要问；解析不了的参数宁可问；`/plan` 把一切会改东西的调用直接拒绝而不是询问。
- **没有操作系统沙箱**：权限靠路径解析和命令文本判断，挡得住误操作和常见绕法，挡不住有心的攻击者。这一点 README 和 `shell.py` 都写明了。
- **结果回到模型之前**：先按 60,000 字符头尾各留一段，再按下一次请求剩下的窗口分摊（`tool_room`）；同一个调用连续第 3 次起，结果后面加一段提醒。

## 3. 值得肯定的地方（审阅时确认的）

- 取消是状态而不是事件：`_StreamCall.cancelled` 一直为真，晚到的连接会被工作线程自己关掉，没有谁的耐心会先耗尽。
- 停止一轮的时候，没来得及跑的调用都补上输出；停止超时时 `pair_in_place` 立刻配好，下一轮不会带着没输出的调用。
- 存档每次写都是临时文件加 `os.replace`；快照前 `paired()` 保证落盘的历史是请求得出去的；压缩点和轮边界跟着配对一起搬。
- 一个会话的旧循环被丢下以后（`_Watch.owner`），它之后的所有报告都被丢弃，不会写进新的一轮。
- 令牌续期被 `asyncio.shield` 保护：用户中途停止一轮，也不会把刚轮换的刷新令牌丢掉。

## 4. 审阅发现

严重程度是对用户体验和成本的影响，不是安全。

| # | 问题 | 证据 | 影响 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| L1 | **请求失败从不重试**：429、5xx、连接断了、流还没开始就被截断，一律让这一轮失败（以前只对「上下文太长」重试一次） | `loop.py:_step` 只处理 `is_overflow` | 长任务做到第 40 步遇到一次限流就死；`/goal` 会因此停（「这一轮出错了」）；Bedrock 的 `ThrottlingException` 很常见 | 重试：1.5 / 4 / 10 / 25 秒（带抖动，服务器给了 `Retry-After` 就听它的，上限 60 秒），告诉用户；额度用完（`insufficient_quota`、`subscription_sharing_*`）、密钥无效、请求本身有问题不重试；回答已经开始（有事件到了）不重试 | **已修**（`errors.is_transient`、`AgentLoop._step`，`_Watch.notice` 现在真的写进对话；`test_harness_loop` 8 个、`test_chatgpt_controller` 1 个、变异见 `mutate_bedrock.py`） |
| L2 | 套餐令牌只在快到期时续；服务器说 401（令牌被提前作废）时不会强制续一次再试，直接说「请重新登录」 | `engine.stream` → `tokens.access_token()` 只看时间 | 偶发的「要你重新登录」，其实续一次就好 | 401 且这个账号的刷新令牌还在时，强制续一次、重发一次 | 未做 |
| L3 | 同一步里的多个工具调用**逐个**执行，哪怕全是互不相干的只读调用（读 5 个文件） | `loop.py:_run_calls` 的 `for` | 慢，尤其是 grep 子进程和大目录；模型常一次发好几个读取 | 连续的、权限为「放行」的 `read` 类调用并发（`asyncio.gather`），结果仍按调用顺序追加；要问用户的和会改东西的保持串行 | 未做 |
| L4 | 流中途失败：页面已经显示了的半截回答**不在历史里**，模型下一次看不到它写过什么 | `_read` 在流结束后才 `history.extend(kept)`；失败时 `_turn_failed` 只在整轮什么都没发生时回滚 | 用户回「继续」，模型会从头再写一遍 | 失败时把已完成的 `ItemDone`（整条消息 / 整个调用）先提交，再标记失败；半条文字加一个「（被截断）」后缀 | 未做，需要小心配对不变量 |
| L5 | `/goal` 没有总预算：最多 20 轮、每轮最多 80 步，最坏 1,600 次请求；重试加上之后只会更久 | `goal.ROUNDS`、`MAX_STEPS` | 按量计费的 API 模型上可能花很多钱 | 给目标加总步数（或总 token）上限，在提示里显示已用多少；或让用户在 `/goal` 里写预算 | 未做 |
| L6 | 连接超时和读超时是同一个 120 秒 | `http.py` 的 `STREAM_IDLE_SECONDS` 同时给 `urlopen(timeout=…)` | 网络不通时，每次尝试要等 2 分钟才失败（有了重试，一轮最多 8 分钟才放弃） | 连接 15–20 秒，读保持 120 秒 | 未做 |
| L7 | 重复调用提醒只认**连续**相同的调用 | `loop.py:reminder` | 模型在 A、B 两个调用之间来回转圈，不会被提醒 | 看最近 6 个调用里同一个键出现的次数；或同一个命令失败 3 次 | 未做 |
| L8 | 没有请求级的诊断：时间、状态码、重试次数、用量、请求 id 只在失败时进日志的一行里 | `WittyCode.log` 只有 WARNING | 用户说「卡住了 / 慢」，没法判断是模型慢、重试了还是工具慢 | `--debug` 时记录每次请求的模型、耗时、状态、用量、请求 id（不记正文和密钥） | 未做 |
| L9 | 图片每一步都随历史重发，估算固定按 1,500 token | `compact.estimate_item` | 带图的长任务成本高，估算可能偏 | 压缩时把旧图片换成一句话；或按尺寸估算 | 未做 |
| L10 | `ChatGPTController`（约 1,170 行）和 `AppCore`（约 3,250 行）职责过重 | 文件长度 | 改一处要读很多；`_run_turn` 里混着技能发现、提示构建、存档、压缩 | 把压缩、存档、审批各拆成小对象；`AppCore` 按领域（会话、模型、设置、记忆）分文件 | 不急 |
| L11 | 技能文件夹和插件根目录里的东西**免问可读** | `ToolContext.readable`、`permissions._readable` | 插件根里别的技能、命令也能被读（只读，不能写；要运行脚本仍然要批准） | 只放行已发现的技能文件夹，插件根只放行该插件的 `skills/` | 可以收窄 |
| L12 | 新做的 Bedrock 路径**没有对着真实 AWS 跑过** | 见下 | 签名和事件流是按文档和 botocore 的结果核对的，不等于真机 | 拿一个 Bedrock API Key 或一份能用的 profile 打几次请求（探针 `sandbox/probe_bedrock_real.py` 待写） | 待用户给凭据 |

## 5. Bedrock（这次新做）

- **走哪条路**：条目的 `provider` 决定线格式——`bedrock-converse` 用 ConverseStream；`anthropic` 加 `bedrock` 块用 InvokeModelWithResponseStream（Anthropic 自己的 Messages 格式，包在事件流里）；`openai` 加 `bedrock` 块用 Bedrock 的 OpenAI 兼容端点（Responses）。三种都不经过 Droid。
- **认证**（照 Factory 文档的 `bedrock` 块）：`environment-api-key` 读 `AWS_BEARER_TOKEN_BEDROCK`，作为 Bearer 发送；`credentials-command` 跑 `awsCredentialExport`，要求打印带 `Credentials` 对象的 JSON；`aws-credentials` 走标准链——指定了 `awsProfile` 就只看那个 profile，否则环境变量（`AWS_ACCESS_KEY_ID` …）、再 `AWS_PROFILE` / `default`；profile 里可以是密钥、`credential_process`、IAM Identity Center（SSO）；找不到时跑 `awsAuthRefresh` 再试一次。没写 `authMethod` 时依次试 Bearer 变量、标准链、导出命令、登录命令。区域按 `awsRegion`、`AWS_REGION`、`AWS_DEFAULT_REGION`、profile 的 `region`，**不设默认值**。
- **签名**：自己实现的 SigV4（标准库，约 40 行），模型 ID 里的冒号在 URL 里是 `%3A`、在规范请求里是 `%253A`。已用 botocore 的 `SigV4Auth` 对同一请求、同一密钥、同一时间做过对照：三种情形（带冒号的 ID、ARN 加会话令牌加元数据头、空体）签名逐位一致；另有 AWS 文档的 `get-vanilla` 用例和签名密钥用例。
- **事件流**：AWS 的二进制帧（前导长度 + CRC、头、载荷、消息 CRC），任意切分都能拼起来，CRC 错或长度不对就报「响应流损坏」；`exception` 帧按名字映射到 HTTP 状态（`throttlingException`→429 …），所以也走重试。
- **其它**：`cachePoint` 只发给已知支持的模型（Claude 3.5 Haiku、3.7、4 及以后，Nova），别的模型会拒绝这个字段；`maxTokens` 用户没写就不发；Claude 开推理时才发 `thinking`，并保证 `maxTokens` 大于预算；`reasoningContent`（含签名、被遮蔽的内容）原样带回同一个来源；`requestMetadata` 按各线格式的方式发（Converse 在体里、InvokeModel 在签名的头里、OpenAI 端点不发），并检查 AWS 的限制（16 项、键 1–256、值 ≤256、字符集）。
- **没做 / 限制**：SSO 登录过期时**不自动刷新**（AWS CLI 的刷新令牌是单次的，我们用了又不写回，会把用户从 CLI 里踢出去）——错误里告诉用户运行 `aws sso login`，或在条目里配 `awsAuthRefresh`；`role_arn`（换角色）的 profile 不支持，错误里指向 `credentials-command`；设置页的「测试连接」对 Bedrock 仍是「保存后在会话里试」；OpenAI 兼容端点的默认地址（`bedrock-mantle.<region>.api.aws/v1`）和它的签名服务名按文档写，**不确定**，可以用 `bedrockBaseUrl` 改。

## 6. 这次动了什么（审阅之外的实现）

`core/llm/aws.py`、`core/llm/bedrock.py`、`HttpClient.stream_chunks` 和 `body=`、`ChatGPTError.transient / retry_after / reworded`、`errors.is_transient`、`AgentLoop` 的重试、`ProviderStore.bedrock_plan`、`ApiModelSpec.bedrock / thinking`、`backend._bedrock` 和 `bedrock_failure`；测试 `test_llm_aws`（27）、`test_llm_bedrock`（42）、`test_llm_bedrock_backend`（20）、`test_harness_loop` 加 8 个、`test_chatgpt_controller` 加 1 个；变异 `sandbox/mutate_bedrock.py`。
