# GPT Plan 与 Devin Opus 缓存命中率差异诊断

日期：2026-11-12。范围：自有 Harness 的请求构造、历史重放、系统提示、压缩和用量归一，以及 Devin 的用量适配。**仅诊断，不修改运行逻辑。**

## 结论

自有 Harness 有可复现的协议保留缺口和前缀稳定性风险，不能直接把较低命中率解释成「GPT 本来就这样」。但本轮没有读取用户真实会话，也没有做同模型、同输入的真实 A/B，**不能把 GPT 与 Opus 的百分比差距全部归因于 Harness，或声称某项改动能提升多少命中率。**

正常、同配置、未压缩的工具续步和下一回合，原输入保持追加式增长，系统提示和工具声明一致；加密推理也有保留。没有复现「每一步都重写全部历史」。

## 已确认的实现行为

| 项目 | 代码与离线证据 | 对缓存结论的边界 |
| --- | --- | --- |
| **助手消息的 `phase` 被丢弃** | `desktop/core/chatgpt/responses.py:197-213` 的 `history_item` 只重建消息的 type、role、content。模拟服务返回 `phase: commentary` 和 `final_answer` 后，下一请求里该字段已消失。 | 官方要求手动重放时保留原 `phase`，否则可能把中间进度当成最终回答。这是明确的协议缺口；本轮未测出它对服务端缓存或推理保留的具体影响，不能当成已证实的唯一缓存根因。 |
| **新回合重建顶层系统提示** | `desktop/core/chatgpt/controller.py:473-522` 每回合重新发现技能并调用 `build_instructions`；`desktop/core/harness/prompt.py:236-287` 将日期、权限、模式、技能、MCP 说明和项目规则放在历史之前。已复现规则修改、增加技能、Plan/Auto 切换导致 `instructions` 改变；跨日日期变化早于项目规则。 | 条件发生时，原前缀从首个变化处之后不再保持一致，可能损失大量历史复用；不是每一步或每回合必然变化。跨天也可能同时遇到服务端缓存到期，不能只归因于日期。 |
| **思考档位直接改请求级参数** | `controller.py:449-518` 与 `responses.py:183-189` 在新回合使用新的 `reasoning.effort`，不追加 `configuration_update`。 | 官方说明该参数可能改变隐藏前缀；支持的 GPT-6 模型可用追加式更新。ChatGPT Plan 对相关新字段的实际接受情况未验证，不能只凭通用 API 文档直接全面启用。 |
| **本地压缩改写历史开头** | `desktop/core/harness/compact.py:1-27`、`summary_item` / `view_of` 使用 `[摘要] + 日志尾部`；离线确认第一条历史发生替换。摘要请求另用自己的指令和无工具请求，并计入该对话用量。 | 压缩后缓存重建是正常代价，不等于压缩有错；应比较总输入、延迟和成本，而不只比较命中率。官方还说明自定义压缩改变前缀可能导致后续旧推理项被丢弃，本轮未读取真实 `input_transformations` 验证。 |
| **未分类新版 Responses 的 Cache Write** | `Usage.of` 读取 `cached_tokens`，未读取 `input_tokens_details.cache_write_tokens`。官方示例的总输入 12,000、缓存读 4,000、缓存写 7,000 被归为 In 8,000、Cache Read 4,000、Cache Write 空。 | 应归为 In 1,000、写 7,000、读 4,000；但现行命中率的分母仍是 12,000，读命中率仍为 1/3。因此这是统计分类缺口，**不是读命中率被算低的解释**。没有确认真实套餐响应是否返回了该新字段。 |

## 没有支持的归因

### 1. 不能说「没传 `prompt_cache_key` 就没有缓存」

`Shape.cache_key` 默认关闭，这一事实属实。但本次实际读取的 OpenAI 官方缓存文档明确区分模型：

- 支持模型默认有提示缓存，不以 `store: true` 为前提。
- GPT-5.6 及更新模型由服务端自动处理缓存路由，`prompt_cache_key` 可用于隔离缓存核算，但不是优化缓存所必需。
- 更早模型可通过稳定的键优化相关请求的缓存路由。

所以不能把「默认没传缓存键」直接定成当前 GPT 新模型命中率低的根因，更不应为了修缓存改成 `store: true` 或给 HTTP 套餐请求添加 `previous_response_id`；套餐文档仍要求 `store: false`、`stream: true`、自行携带历史。

### 2. 没有发现缓存读取量被简单重复相加或漏减

Responses 的 `input_tokens` 含缓存读取量，`Usage.tokens()` 将其拆为非缓存 In 与 Cache Read；Devin 的 `usage_call` 也先减掉上游含在输入里的缓存读取量。相同的合成总输入和缓存读数字归一后完全一致。相关账本测试也通过。

前端当前公式是：

```text
Cache 命中率 = 累计 Cache Read / (累计 In + 累计 Cache Write + 累计 Cache Read)
```

不含输出 token，是**整个对话、所有计入调用的输入 token 加权比率**，不是最后一次请求或当前所选模型的命中率。冷启动、模型切换、本地摘要调用和压缩后的首个请求都会影响累计值；换模型不会把累计用量清零。

### 3. Opus 与 GPT 的百分比不是同条件 Harness A/B

Claude 支持 `cache_control` 自动缓存或显式断点，OpenAI 新旧模型的断点与保留策略也不同。Devin 内部的实际请求、断点和路由不在本仓库的 ACP 适配层里；本轮没有读取或验证它的实现。不能据此宣称 Devin 一定用了某种 TTL、缓存策略，或者一定比自有循环少耗多少额度。

## 建议的最小修复顺序（尚未实施）

1. **完整保留可重放的协议字段**：先补助手消息 `phase` 的解析、存档、续步及重开回归，不对旧记录猜填阶段，也不盲目回传所有服务端字段。
2. **稳定前缀**：将会话稳定的基础提示与后续规则更新分开；模式、权限、技能和项目规则的变化以追加 developer 消息传达，同时让本地权限检查立即保持正确。不能靠冻结旧规则来换缓存。
3. **按模型和套餐能力处理请求更新**：评估 `configuration_update`；补读服务端提供的 Cache Write，但保持总量、旧存档和命中率兼容。不把通用 API 的所有缓存选项无条件加到套餐请求里。
4. **验证真实收益后再下结论**：用同一 GPT 模型、相同推理档位和相同输入做稳定前缀对照；逐请求比较输入/缓存读/缓存写/耗时/压缩与配置变化。诊断记录仅存数字和不可逆指纹，不存正文、令牌、真实路径或账号信息；真实调用次数须先明确。

以上是针对本次问题的建议，不是已承诺或已完成的实现。

## 验证

- 本机模拟 Responses 服务实跑 7 个请求，**14 项离线检查全部确认预期观察**：正常指令/工具/旧输入稳定、加密推理保留、助手 phase 丢失、默认缓存键、规则/技能/模式/思考档位/日期变化、Cache Write 分类缺口、Devin 读取量归一一致、读比率不变、压缩改首条历史。
- 现有相关测试 **188 通过，31.76 秒**：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_chatgpt_responses.py tests/test_chatgpt_controller.py tests/test_harness_loop.py tests/test_harness_prompt.py tests/test_harness_compact.py tests/test_usage.py tests/test_devin_items.py -q -p no:cacheprovider
```

- 公开文档 7 页实际下载均为 HTTP 200。通用 API 文档不等于 ChatGPT Plan 每项能力都已实测。
- **真实模型调用 0 次**；未读取用户真实会话、登录凭据或用户设置。未改业务代码和界面；全量测试、真实服务端命中率/推理保留、同模型 Harness A/B、修改后的收益均**未验证**。

## 公开依据（本次实际读取）

- [OpenAI Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)：默认缓存、新旧模型的缓存键差异、前缀稳定性、请求级配置变化、新版缓存写字段。
- [OpenAI Keep the prefix unchanged](https://developers.openai.com/api/docs/guides/preserved-reasoning)：追加式更新、前缀变化与推理丢弃、自定义压缩的权衡。
- [OpenAI Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)：加密推理重放、原样保留 assistant phase、思考档位配置更新。
- [ChatGPT Plan Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)：HTTP 无状态请求要求与不支持字段。
- [ChatGPT Plan Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)：公共 Responses 端点与流式完成条件。
- [Claude Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)：`cache_control`、自动缓存和显式断点；不是 Devin 内部实现证据。

---

# 复核与修复计划（2026-11-12，第二轮）

用户：「查证一下现在工程的 Harness 问题，给出修复方向计划」。本节复核上文、补上它漏掉的官方规则，并给出分阶段计划。**仍然没有改运行逻辑，真实模型 0 次，没有读用户的真实会话。** 与上文不一致的地方以本节为准。

## 复核结论

1. 上文五项实现行为全部属实（旧的 14 项离线观察重跑仍成立）。
2. **没有发现每一步或每个回合必然破坏前缀的地方。** 同配置的续步和续轮，按官方规则可以复用上一个请求的全部输入（离线推演 100%）。Harness 的损失是**事件触发**的：下面 C、D 两类事件每发生一次，整段对话按新输入重算一次；按官方规则，较新的模型还会丢弃其后的推理项。
3. 上文漏了官方文档对 GPT-5.6 及更新模型的几条关键规则（断点位置、写入只在断点、前缀变化后丢推理、`reasoning.context`、官方缓存诊断、服务端压缩）。它们改变了修复的优先级和做法。
4. Harness 现在对缓存是**盲的**：响应里能解释「为什么没命中」的字段全部被丢，也没有逐请求记录。所以仍然不能说这些问题解释了 GPT 与 Opus 差距的多少。计划的第一步就是补上这一块。

## 补充的官方规则（本轮实读，GPT-5.6 及更新模型）

1. **断点位置**：隐式缓存的断点在「最新的合格消息」末尾。合格消息是用户消息、一组连续工具结果的最后一条、开头连续 developer 消息的最后一条。查找时只看当前请求的隐式断点、往前最多 20 个合格消息结尾、开头 developer 块的结尾（以及显式断点，Harness 现在没有用）。**写入只发生在断点上**：两个请求共享的前缀如果不在某个写过的断点结束，就不能复用（文档「A shared prefix is not always a cached prefix」）。
2. **顶层 `instructions` 不能放显式断点。** 要让稳定指令单独可复用，得放进 developer 消息的 `input_text` 块并加 `prompt_cache_breakpoint`。
3. **寿命和价格**：默认 30 分钟，`prompt_cache_options.ttl` 可选 `12h`。写入按普通输入的 1.25×（12h 为 2.5×），读取 0.1×（GPT-6.1 Sol / Terra 为 0.05×）。用量里 `cached_tokens` 是读，`cache_write_tokens` 是写（`cache_write_tokens_details` 再分 `ttl_30m_tokens` / `ttl_12h_tokens`），普通输入 = 输入 − 读 − 写。
4. **前缀变化后丢推理**：较新的前沿模型会丢弃前缀变化之后的推理项（防蒸馏）。改 `instructions`、`tools`、编辑旧输入、把自定义摘要放在最近回合之前，都算前缀变化。响应的 `input_transformations` 写明哪些推理项被移除及原因（`prefix_mismatch`）。
5. **`reasoning.context`**：`all_turns` 把以前回合的推理渲染进上下文，`current_turn` 不渲染。GPT-5.6 系列默认 `all_turns`，更早的模型默认 `current_turn`，GPT-6.1 Sol 也支持 `all_turns`；套餐里各模型实际用哪种，要看响应里的 `reasoning.context`（实际生效值）。无状态重放要保留每个输出项，原文是「including encrypted reasoning and assistant phase」。
6. **官方缓存诊断**：请求带 `prompt_cache_options.comparison_response_id`（上一个响应的 id），响应的 `prompt_cache_diagnostics` 给出 `cache_hit` / `cache_miss` 和原因（`tools_changed`、`input_changed`、`reasoning_effort_changed`、`context_compacted`、`model_changed`、`prompt_cache_key_changed` 等）。不另收费，兼容零数据保留。
7. **追加式更新**：思考档位用追加的 `configuration_update` 项（支持的 GPT-6 模型），顶层 `reasoning.effort` 保持不变；工具保持声明和顺序不变，用 `tool_choice` / `allowed_tools` / `additional_tools`；规则更新追加新的 developer 消息。
8. **服务端压缩**：`context_management: [{type: "compaction", compact_threshold}]`（或 `/responses/compact`）在 `store:false` 下可用，返回加密的 compaction 项，其后的推理项不会被丢。自定义压缩则应把压缩项放在上下文末尾，或者接受其后的推理被丢。
9. **一次性请求**：`prompt_cache_options.mode: "explicit"` 且不设断点时，请求不读也不写缓存，没有写入费。
10. **缓存键**：GPT-5.6 及更新模型只用它做核算隔离；键变了反而会被记成未命中（`prompt_cache_key_changed`）。更早的模型才靠稳定的键优化路由。

套餐路线的限制页今天重读过：`prompt_cache_retention` 明确不支持；`prompt_cache_options`、`prompt_cache_breakpoint`、`reasoning.context`、`configuration_update`、`context_management` **既没列为不支持，也没说支持**，都要真实探针确认后才能用。

## 本轮查证到的问题

离线脚本在本地 `sandbox/cache-review-20261112/verify_harness_cache.py`（未入库）：真的循环和控制器接本机假服务，按上面规则 1 推演相邻两个请求能复用多少，token 用 Harness 自己的估算。**是按文档规则的推演，不是对服务的实测。**

| # | 问题 | 代码与离线证据 | 影响 |
| --- | --- | --- | --- |
| A1 | 响应级诊断字段全部丢弃 | `Finished` 只有 `usage`、`response_id`、`incomplete`（`chatgpt/responses.py:136-141`、`326-330`）。假服务返回 `reasoning.context`、`input_transformations`、`prompt_cache_diagnostics`、`service_tier`，循环一个都没留 | 推理有没有被丢、为什么没命中、实际生效的推理上下文，都无从知道 |
| A2 | Cache Write 没读 | `Usage.of`（`responses.py:58-69`）。输入 12,000／读 4,000／写 7,000 显示成 In 8,000、写为空；应为 In 1,000、写 7,000 | 命中率数值不变；In 和按价格估成本都偏 |
| A3 | 存档丢 Cache Write，重开后总量被再加一遍 | `_add_usage` 只存 `input`／`output`／`cacheRead`（`chatgpt/controller.py:765`），`open_saved` 恢复时写入恒为空（`controller.py:292`）。应用的账本从缓存恢复后看到写入计数变小，按「序列重新开始」把整段对话再计入一次（`core/usage.py:86-96`、`124-132`，`app_core.py:1204`、`1567-1570`）。用这几个类直接复现：三次带写入的调用，重开前 In 2,000／写 14,000／读 23,500，重开后账本成了 In 4,000／写 14,000／读 47,000；整机路径只核对了代码，未实跑 | 现在只影响会报写入的路线（Anthropic、Bedrock）。**A2 一修，GPT 路线也会中，所以两处必须一起改** |
| A4 | 没有逐请求记录 | 即 `docs/loop-review-2026-11-11.md` 的 L8，至今未做 | 无法在真实使用中定位哪一次请求为什么没命中 |
| B1 | 助手消息的 `phase` 被丢 | 上文已证；本轮确认同时丢的还有每个输出项的 `id`、`status` | 官方要求保留；对推理保留的实际影响未验证 |
| B2 | `history_item` 是白名单 | `responses.py:197-213`：未列出的字段一律丢，不认识的输出项类型（例如服务端压缩的 `compaction` 项）整项丢弃 | 服务端加新字段时静默丢失；也挡住了规则 8 |
| C1–C8 | 八种变更都重写历史之前的内容 | 日期（`harness/prompt.py:259`）、Plan／Auto（`254`、`264-265`）、权限档（`261`）、思考档位（顶层 `reasoning.effort`，`responses.py:183-189`）、`AGENTS.md` 被改（`271-281`，别的代理改也算）、技能增删改（`controller.py:485`、`prompt.py:266-267`）、「思考用中文」设置（`252`）、记忆服务器在首回合 20 秒等待之后才起来（`controller.py:325-336`，这时 `tools` 和 `instructions` 都变）。离线：除「什么都没变」可复用 100% 外，八种全部 0 | 每发生一次，整段对话按新输入重算（按 API 价是写入的 1.25×，而不是读取的 0.1×／0.05×；套餐额度怎么折算，文档没写），其后的推理项在适用规则 4 的模型上被丢。一次 `/plan` 往返是两次 |
| D1 | 自定义摘要放在保留的尾部之前 | `harness/compact.py:197-202`。离线：压缩后第一个请求可复用 0；尾部仍带着加密推理项，按规则 4 会被服务端丢弃 | 压缩后缓存重建是预期代价，但尾部推理白带 |
| D2 | 摘要请求是另一份提示 | `compact.py:421-432`：自己的指令、不带工具、把历史渲染成文本 | 读不到主对话的缓存；隐式模式下还要按写入价写一份不会再用的缓存；用量计入该对话的累计命中率 |
| E | 基础前缀不能单独复用 | 指令在顶层 `instructions`（`responses.py:172-178`），输入里没有 developer 消息，也没有显式断点。离线：同项目两个新会话之间可复用 0 | 每个新会话、每次压缩后，指令加工具（估算约 3.6K–5.5K token）重算一次。量小，短会话里占比高 |
| F | 一次性请求照常写缓存 | 记忆整理的旁路请求（`chatgpt/side.py:36`）在回合结束后发（有限频），用最快的模型，不计入对话用量；它和摘要请求都没用规则 9 | 不影响显示的命中率，多花写入价 |
| G1 | API Provider 的旧 OpenAI 模型 | `Shape.cache_key` 默认关，也不发 `prompt_cache_retention` | 只影响 5.6 之前的模型 |
| G2 | Anthropic 路线 | 断点位置正确（system 一处，最后一个块一处，`llm/anthropic.py:117-136`），但 `system` 同样受 C 类变更影响；默认 5 分钟寿命，没有 1 小时选项 | C 类修好后一并受益 |

复核后**没有问题**的：同配置续步和续轮的工具、指令、旧输入稳定；加密推理有回传；重试发的是同一个请求；存档的 JSON 往返不改变条目；工具、技能、MCP 工具的顺序是确定的；`<wittycode-context>` 块在用户消息末尾并随条目保存；`store:false`；新模型不传缓存键；读命中率的公式。

**仓库之外、会拉低累计命中率但不是 Harness 缺陷的**：停顿超过缓存寿命（30 分钟）后的第一个请求、对话中途换模型（缓存按模型分，推理也只在同一模型系列内复用）、短会话的冷启动。界面上的数字是整个对话、所有模型、含摘要请求的累计值。

## 修复计划

顺序按「先看得见，再改确定的，最后改设计」。每一阶段单独提交、单独验证。

### 阶段 0：可观测性（不改变发给模型的内容）

- 读 `cache_write_tokens` 及两种寿命的细分，归到 Cache Write；**同一次改动里**让存档存取带上 `cacheWrite`（否则 A3 的重复计入会扩大到 GPT 路线），旧存档缺这个字段时保持为空，不猜；补一条「带写入的会话重开后总量不变」的回归。
- `Finished` 带出响应级字段：实际生效的 `reasoning.context`、`input_transformations`、`prompt_cache_diagnostics`、`service_tier`。
- 每个请求写一条诊断记录（默认关或有上限，放在设置目录）：时间、模型、档位、工具声明和指令的哈希、输入条数和前缀链式哈希、与上一个请求的公共前缀长度、读／写／普通输入／输出、耗时、是不是摘要或旁路请求、被移除的推理项数和原因。**只有数字和不可逆哈希**，不存正文、路径、令牌、账号。
- 诊断开关：带上 `prompt_cache_options.comparison_response_id`，取官方原因码（先过探针 2）。
- 验收：假服务返回新字段的单元测试；记录里不含正文的测试；全量测试全绿。

### 阶段 1：协议保真（确定的小修）

- 保留助手消息的 `phase`：续步、下一回合、存档、重开都有回归；旧记录不猜填。
- `history_item` 从「只留认识的字段」改成「原样保留，只去掉确认不能回传的」；`id`／`status` 回不回传由探针 3 决定。不认识的输出项类型不再静默丢弃，至少记日志。
- 验收：`phase` 往返测试；Chat／Anthropic／Bedrock 适配器不受影响的测试。

### 阶段 2：前缀稳定（预计收益最大）

- **指令快照**：会话第一个回合渲染的指令、工具声明和顶层档位存进会话，之后每个请求原样复用。
- **易变内容改成追加**：日期、权限档、Plan／Auto、技能清单变化、`AGENTS.md` 变化、MCP 说明变化、思考语言，从指令里拿出来；变了就在下一条用户消息之前追加一条更新（Responses 路线用 developer 消息，其余适配器折进用户消息的 `<wittycode-context>` 块），写明它取代之前的说法。本地权限检查本来就即时生效，不依赖提示，不变。
- **思考档位**：支持的模型用 `configuration_update`（先过探针 5），不支持的保持现状。
- **工具声明固定**：开了共享记忆就始终声明它的工具（schema 是静态的），服务器没连上时调用返回错误，而不是让声明随连接状态变化。
- **重新快照的时机**：压缩之后、换模型之后。这两处缓存本来就要重建，顺便刷新不多花。
- 涉及：`prompt.py`、`controller.py`、存档、四个适配器、压缩时的渲染、页面的历史重建（都要认识新的更新项）。
- 验收：把本轮离线脚本的九种情形改成回归测试（变更后指令、工具、顶层档位不变，旧输入是新输入的前缀，更新项在末尾）；Plan 下仍拒绝改动；规则变更后模型看得到新规则。

### 阶段 3：压缩和一次性请求

- 摘要请求在放得下时复用主前缀：同样的指令和工具、`tool_choice: "none"`，在历史末尾追加「写摘要」的消息，这样整段历史按读取价读；放不下再退回现在的分块渲染。
- 压缩后的尾部：要么整段都进摘要，要么保留尾部但不再发注定被丢的加密推理。对 Responses 路线评估服务端压缩（先过探针 7）；换模型和其他提供方仍需要自己的摘要。
- 旁路请求和分块摘要用规则 9 免写（先过探针 8）。

### 阶段 4：跨会话复用、寿命和旧模型（按探针结果取舍）

- 稳定指令放进开头的 developer 消息并加显式断点，让同项目的新会话和压缩后的请求复用基础前缀（先过探针 4）。
- 缓存寿命设置：默认 30 分钟，可选只给稳定块 12 小时。
- API Provider：5.6 之前的 OpenAI 模型发稳定的 `prompt_cache_key` 和支持的 `prompt_cache_retention`；Anthropic 可选 1 小时寿命。
- 界面：命中率旁加「最近一次请求」和按模型的数字。

### 阶段 5：真实对照

同一个 GPT 模型、同样的档位和输入，修复前后各跑一遍，用阶段 0 的记录逐请求比较读、写、普通输入、耗时和被丢的推理项。调用次数由用户先定。

### 不建议做的

- 默认给新模型加 `prompt_cache_key`（不需要，键一变还会被记成未命中）。
- 为了缓存改 `store:true` 或给套餐的 HTTP 请求加 `previous_response_id`（套餐路线不允许）。
- 用冻结旧规则不告诉模型来换缓存。

## 需要真实探针确认的（未做，等用户给调用上限）

用最便宜的套餐模型、最低档位，每个请求很小（指令凑够 1,024 token 的最小可缓存长度）。预计 12–16 个请求。

1. 基线两步往返：用量里有没有 `cache_write_tokens`，响应的 `reasoning.context` 是什么，输出项带不带 `phase`。
2. `prompt_cache_options.comparison_response_id` 是否被接受，是否返回 `prompt_cache_diagnostics`。
3. 重放时保留 `phase`（以及 `id`／`status`）是否被接受；保留和丢弃两种做法下 `input_transformations` 有没有差别。
4. 指令放进 developer 消息并加 `prompt_cache_breakpoint`，换一条首条消息再发，看 `cached_tokens`；与顶层 `instructions` 对照。
5. `configuration_update` 是否被接受。
6. `reasoning.context: "all_turns"` 是否被接受。
7. `context_management` 是否被接受（阈值设高，不触发压缩）。
8. `prompt_cache_options.mode: "explicit"` 不设断点是否被接受，`cache_write_tokens` 是否为 0。

## 本轮验证

- 新离线脚本：重放与请求形状、九种回合间变更、同项目两个新会话、回合中途压缩、旁路请求、本仓库的指令大小，结果如上表。
- 上一轮的 14 项离线观察重跑仍全部成立。
- 相关现有测试 **243 通过（54.39 秒）**：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_chatgpt_responses.py tests/test_chatgpt_controller.py tests/test_harness_loop.py tests/test_harness_prompt.py tests/test_harness_compact.py tests/test_chatgpt_compaction.py tests/test_usage.py tests/test_devin_items.py tests/test_llm_anthropic.py tests/test_llm_chat.py -q -p no:cacheprovider
```

- 官方文档本轮实读 6 页：Prompt caching、Prompt cache diagnostics、Keep the prefix unchanged、Reasoning models、Compaction、套餐的 Preview limitations。
- **真实模型 0 次**；没有读用户的真实会话、凭据或设置；没有改业务代码、测试或界面。全量测试未重跑（没有代码改动）。套餐路线对上面各新字段的接受情况、服务端的真实命中率、各阶段的实际收益均**未验证**。
