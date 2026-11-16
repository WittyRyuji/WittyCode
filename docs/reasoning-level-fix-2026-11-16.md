# 思考档位的送达与关闭（设计说明）

- 日期：2026-11-16
- 状态：已独立核验核心问题并实现可配置修复；真实服务效果未验证，未调用真实模型
- 范围：API Provider 的三条路线（`generic-chat-completion-api` / `openai` / `anthropic`），不含 ChatGPT Plan
- 行号是原始设计时的位置；修复后以函数／字段名和第 7 节的实际测试文件定位

## 1. 已核验的问题：省略参数不等于关闭，兼容服务的档位字段并不统一

用工程内真实链路（`ProviderStore` → `ApiBackend` → `chat_body`/`responses_body`/`anthropic_body`，本机假服务收 POST）实测，provider 条目写在临时 settings、base URL 指向本机，0 次真实模型调用。

| 路线 | 非 off 档位实际发出 | off 实际发出 |
| --- | --- | --- |
| Chat（`POST {base}/chat/completions`） | `reasoning_effort: "minimal"…"max"` | 无任何档位字段，也没有 `thinking` |
| Responses（`POST {base}/responses`） | `reasoning: {"effort": "…", "summary": "auto"}` + `include: ["reasoning.encrypted_content"]` + `store:false, stream:true` | `reasoning` 与 `include` 都不发 |
| Anthropic（`POST {base}/v1/messages`） | `thinking: {"type":"enabled","budget_tokens":N}`（minimal 1024 / low 2048 / medium 8192 / high 24000 / xhigh·max 30976） | `thinking: {"type":"disabled"}` |

三条结论：

1. Chat 与 Responses 的档位值能到服务端，但 **off 只是「不提要求」**；服务端默认开思考时等于没关。
2. Anthropic 路线在通用分支把档位换算成 `budget_tokens`；Claude 的预算／自适应适配不是「完全没传档位」。问题是 DeepSeek 等兼容服务要求的显式 `output_config.effort` 没有由非 Claude 分支发出，不能把通用预算等同于该服务的档位字段。
3. `extraArgs` 确实是最后覆盖（`llm/chat.py:127`、`llm/responses_api.py:29`、`llm/anthropic.py:167`），可以当临时手段，但它对所有轮次固定生效，无法跟输入框的档位联动。

## 2. 服务端的契约（DeepSeek，2026-11-16 实读官方文档）

来源：<https://api-docs.deepseek.com/guides/thinking_mode>

| | Chat（OpenAI 格式） | Anthropic 格式 | Responses 格式 |
| --- | --- | --- | --- |
| 开关 | `{"thinking": {"type": "enabled/disabled"}}` | `{"thinking": {"type": "enabled/disabled"}}` | 由档位决定，`none` = 关闭 |
| 档位 | `reasoning_effort: low/high/max` | `output_config: {"effort": low/high/max}` | `reasoning: {"effort": none/low/high/max}` |

- 默认开思考，默认 effort `high`。
- 映射：minimal→low，low→low，medium→high，high→high，xhigh→high，max→max，ultra→max。所以本应用菜单的 7 档除 off 外都能被接受。
- 带 `tools` 的 Chat 请求必须回传历史 `reasoning_content`，否则 400（本仓库的 Chat 适配器对同源思考会回传，见 `llm/chat.py:75-77`，未在真实服务上验证）。

## 3. 目标与不变量

目标：

1. 三条路线的档位都以服务端认识的字段送达。
2. `off` 表达「关闭思考」，而不是「不提要求」。
3. 不为单家厂商写死代码；通用 body 仍是 OpenAI/Anthropic 规范形态。

不变量（不随之改变）：

- `summary`/`include`/`store`/`stream`/`max_tokens` 的通用形态。
- ChatGPT Plan 路线的任何行为。
- 合并优先级里 `extraArgs` 仍最高。

## 4. 方案：条目级的「档位 → body 片段」映射

provider 的模型条目新增可选字段 `reasoningArgs`：JSON 对象，键是档位（`off`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`），每个档位的值必须是请求体片段对象。**片段内的顶层字段**值为 `null` 时删除该字段；档位本身的值不能是 `null`，嵌套对象里的 `null` 不递归删除。例如 `{"off":{"include":null}}` 删除 `include`，而 `{"off":null}` 是无效配置。

合并顺序（只对 API 模型生效）：

```
通用字段  <  reasoningArgs[本轮的档位]  <  extraArgs
```

不做内置 preset（如 `reasoningPreset: "deepseek"`）的原因：厂商字段是它们自己的扩展，写进代码就要跟着对方文档发版；放配置里，任何"兼容但有差异"的服务都能用，也不需要我们维护厂商清单。

档位从哪来：`off` 在 API 模型上会带内部标记 `wc_reasoning_effort: "off"`（`chatgpt/responses.py:199-200`）；Chat 的 body 构造函数目前没读它，这是前置改动。

## 5. 改动清单

### 5.1 请求侧

| 文件 | 改动 |
| --- | --- |
| `llm/chat.py` 的 `chat_body` | 在 `extra_args` 之前调用共用 `apply_reasoning_args`，从原始请求读取本轮档位，包括 off 内部标记 |
| `llm/responses_api.py` 的 `responses_body` | 按既有逻辑清理 `reasoning`/`include`，再从原始请求应用映射；内部标记不发给服务端 |
| `llm/anthropic.py` 的 `anthropic_body` | 在通用 `thinking_fields` 之后、`extra_args` 之前调用同一个映射处理器；可整体替换 `thinking` 并补 `output_config.effort` |
| `llm/wire.py` 的 `apply_reasoning_args` | 统一解析档位与执行顶层覆盖／null 删除，不修改原始请求或映射配置 |

### 5.2 条目与传输

| 文件 | 改动 |
| --- | --- |
| `providers.py:300-327` | `ApiModelSpec` 加 `reasoning_args: dict[str, dict]` |
| `providers.py` 的 `_parse_draft` / `_reasoning_args` | 草稿解析 JSON 文本；键限 `EFFORTS`（已包含 off），每档值必须是对象，拒绝 NaN／Infinity；不合法抛 `ProviderError`，不写文件 |
| `providers.py:697` | `_set_or_drop(entry, "reasoningArgs", …)` 写回，空则删 |
| `providers.py` 的 `_provider_view` | 同一 Provider 的公共映射以 JSON 文本回显；不同映射用 `reasoningArgsMixed` 提示并保持原值 |
| `providers.py:1048-1080` | 组装 spec 时带上 |
| `wire.py:22-36` | `Route` 加 `reasoning_args: Mapping[str, Any]` |
| `llm/backend.py:58-72` | `route_of` 传入 |

### 5.3 设置页

`frontend/src/components/ProviderEditor.tsx`：

- 高级区「额外参数（JSON）」下方加「思考档位参数（JSON，可选）」，`rows=4`。
- 校验照抄 `extraArgs`（`:118-123`），错误文案点名档位键，例如「键只能是 off/low/medium/high…，值要是一个 JSON 对象」。
- 有值时默认展开高级区（`:151` 同款判断）。
- 占位示例用 Chat 那条最小配置。

## 6. 配置示例（DeepSeek）

**Chat（`generic-chat-completion-api`）**：档位已由 `reasoning_effort` 送达，只缺关闭。

```json
{"off": {"thinking": {"type": "disabled"}}}
```

**Responses（`openai`）**：`none` 即关闭。

```json
{"off": {"reasoning": {"effort": "none"}}}
```

若该端点拒 `include`（DeepSeek 文档只写了 effort，未提这个字段）：

```json
{"off": {"reasoning": {"effort": "none"}, "include": null}}
```

**Anthropic**：当前档位被换成 `budget_tokens`，要用映射改成 `output_config.effort`。

```json
{
  "off":     {"thinking": {"type": "disabled"}},
  "minimal": {"thinking": {"type": "enabled"}, "output_config": {"effort": "low"}},
  "low":     {"thinking": {"type": "enabled"}, "output_config": {"effort": "low"}},
  "medium":  {"thinking": {"type": "enabled"}, "output_config": {"effort": "high"}},
  "high":    {"thinking": {"type": "enabled"}, "output_config": {"effort": "high"}},
  "xhigh":   {"thinking": {"type": "enabled"}, "output_config": {"effort": "high"}},
  "max":     {"thinking": {"type": "enabled"}, "output_config": {"effort": "max"}}
}
```

（映射依据第 2 节的 effort 表；Anthropic 的 base URL 是 `https://api.deepseek.com/anthropic`，拼接见 `wire.py:44-48`。）

## 7. 测试清单

1. `tests/test_reasoning_args.py`：三种 kind 的全部 7 档及 off→low 切换的真实本机 HTTP body 断言；未配置映射的原始行为也保留回归。原有 `tests/test_llm_backend.py` 不改。
2. 优先级：`reasoningArgs` 覆盖通用字段；`extraArgs` 覆盖 `reasoningArgs`；`null` 能删掉 `include`。
3. 非法 `reasoningArgs`（非 JSON / 非对象 / 未知档位键 / 值是数组）→ `ProviderError`，settings.json 不被改写。
4. ProviderStore 往返：存盘→读回→视图一致；旧文件没有该键时行为不变。
5. `frontend/scripts/reasoning-args.test.mjs`：非法 JSON／映射提示及高级区展开判定；`tests/test_app_reasoning_args_e2e.py`：真实 Qt 编辑器校验、保存、重开自动展开与本机 HTTP 请求。

## 8. 风险与备选

- 配置写错会让请求被服务端拒（400）：保存时校验 + 错误点名档位。
- 配置持久保存，但**每次请求按本轮的档位选择对应片段**；切换输入框的档位会切换片段，不是固定应用同一段配置。未配置对应档位时保留既有协议行为，不自动猜测厂商字段。
- 设置页的高级输入统一配置该 Provider 的全部模型；磁盘上仍是每条模型各自的 `reasoningArgs`。原有模型的映射不同时会明确提示，未编辑时保留各自原值，填写新映射后才统一覆盖，填写 `{}` 清除全部。旧客户端不提交该字段时也保留原值。
- Anthropic 配置偏长（7 条）；后续可加「档位别名表」（`{"minimal":"low"}`）简化，不在本次范围。
- 只作用于 `api:` 模型的三种 HTTP 路线，ChatGPT Plan 和 Bedrock 不应用此映射。未配置映射时兼容既有请求；这不代表所有服务都能零配置关闭思考。实际使用 DeepSeek 时需在自己的 Provider 中明确填入上面的映射，本次没有修改用户真实配置。
- 备选（不推荐先做）：内置 DeepSeek preset，用户零配置，但把厂商知识写进代码，对方改文档就要发版。

## 9. 未验证

- DeepSeek 是否接受 Responses 的 `summary`/`include`（文档未写，可能忽略也可能 400）。
- DeepSeek 的 Anthropic 格式是否接受我们原有的 `budget_tokens`（文档只写 `thinking` 开关和 `output_config.effort`）。
- 真实服务上的 400 边界（本轮全部为本机假服务，0 次真实模型调用）。

## 附：复现方法

临时脚本（不入库）用工程内模块直接跑，不要读写用户设置：

1. `ProviderStore(models_file=<临时文件>, environ={"TEST_API_KEY": …})` 建一个 provider 条目，`baseUrl` 指向 `tests.llm_support.FakeApi`。
2. `ApiBackend(store).models()` 取模型 id；`build_request(model=…, instructions=…, items=[user_message("hi")], tools=[], reasoning=<档位>)`。
3. `backend.stream(request)` 消费到底（脚本流形状不完整会报读取错误，不影响请求）。
4. 断言 `FakeApi.posts[0]` 的路径与 body 字段。

## 10. 独立核验与实现说明

本次重新抓取并阅读官方正文，而不是只采信本文件或既有记忆：

- [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 的三格式表格、默认开启／high、effort 映射以及带 tools 时回传 `reasoning_content` 的要求，与第 2 节一致。
- [official OpenAI documentation：Reasoning](https://developers.openai.com/api/docs/guides/reasoning) 明确档位和默认值依赖具体模型，部分模型拒绝 `none`。因此不能给所有 OpenAI 兼容服务无条件把 off 改成 none。
- [Claude extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking) 区分手动 `budget_tokens` 与自适应思考，支持对原文「Anthropic 档位根本发不出去」的修正。

已由本机 HTTP 服务重现未配置时的三条原始请求形状，并验证配置后每个档位及 off→low 切换实际发出的 body。共用映射处理器只在三种 API 适配器中调用，顺序为通用字段→当前档位片段→extraArgs；不改 `build_request` 或 ChatGPT Plan 路线。新增持久化、非法配置不落盘、混合映射保留、前端校验／高级区默认展开，以及真实 Qt 编辑器保存后再次读取并请求本机服务的回归。假服务收到参数不等于真实提供商已接受或模型确实按相应档位思考；第 9 节边界仍然保留。
