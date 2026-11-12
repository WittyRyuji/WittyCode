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
