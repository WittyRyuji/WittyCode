# 用 ChatGPT 订阅驱动别的 harness：Devin 是怎么做的，我们能不能做

状态：**调研（2026-11-04 上午），已被后续实现取代**：用户决定走自己的 harness 直连 Responses API，已实现为第四个引擎，见 `docs/chatgpt-plan-harness.md`（规范事实、模块合同、待真实令牌核实的清单都在那里）和 `AGENTS.md` 的「ChatGPT Plan 引擎」一条。下面第 3 节「建议先不做」是当时的结论，已经不适用；官方文档里还写了怎么接 Codex app-server（环境变量 `ACCESS_TOKEN` + `model_provider`），我们没有用这条路。来源都是官方文档（括号里是网址）；标「推断」的是我从文档推出来的，没有验证。

## 1. Devin 里用 GPT 订阅：harness 还是 Devin 的

不经过 Codex CLI。这是 OpenAI 的官方项目 **Sign in with ChatGPT**（2026-09-29 DevDay 发布，7 月先上线了只管登录的 beta）里的「**ChatGPT plan usage**」（Devin 文档叫 token sharing）：用户在应用里点「Continue with ChatGPT」，授权「Use your ChatGPT plan」，之后这个应用向 OpenAI 模型发的**合格请求**记到用户的 ChatGPT 订阅（Go / Plus / Pro）的 Work 和 Codex 用量上，不需要 API key。

- Devin 是 16 个首批合作方之一（Devin、Amp、Warp、Notion、Vercel、Kilo Code …，开源的有 OpenCode、OpenClaw、Pi、T3 Code）。Devin 一侧：Pro / Max / Teams 套餐可用，免费版和 Enterprise 不行；一个账号级开关（CLI 里 `/billing on`），GPT 模型的请求记到 ChatGPT 套餐，其他模型照旧记 Devin 额度；ChatGPT 额度用完，CLI 会重试到 Devin 额度。（https://docs.devin.ai/admin/billing/chatgpt）
- 所以 Devin 里选 GPT 模型：**agent 循环（系统提示、工具、权限、压缩）是 Devin 的**，模型请求由 Devin 向 OpenAI 发出、记到你的订阅。Devin 的 CLI 不要求单独登录 ChatGPT，授权是在 Devin 设置里做的，所以授权大概率存在 Devin 的服务端（推断）。
- 官方帮助页：https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites

## 2. 技术上怎么做（官方开发者文档，https://developers.openai.com/siwc）

OAuth 授权码 + PKCE：

1. 打开 `https://auth.openai.com/api/accounts/authorize`，`client_id=dynamic_agent_client`（第一次，让 OpenAI 登记一个新客户端；用户在同意页给它起名；之后用返回的 `client_id`），`ext_agent_host_id=urn:uuid:…`（每台机器一个、先存好再登录），scope `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`（最后一个就是「用我的 ChatGPT 套餐」，用户可以在同意页关掉），`resource=https://api.openai.com/v1`，回调是本机 `http://127.0.0.1:<端口>/auth/callback`（只能是 127.0.0.1，端口可变）。
2. 用授权码到 `https://auth.openai.com/api/accounts/oauth/token` 换 token；检查 ID token 和 scope。access token 约 1 小时，refresh token 约 30 天、每次用都换新的。
3. 用 access token 当 Bearer 调 `POST https://api.openai.com/v1/responses`（不是 chatgpt.com 的 backend-api）。限制：`store: false`、`stream: true`；不能带 `temperature`、`max_output_tokens`、`previous_response_id`、`metadata`、`truncation`、`user` 等；system 角色的消息会被拒，用 `instructions`；托管工具（图片生成、文件搜索、Code Interpreter、托管 MCP、computer use、`tool_search`）不能用，自己的 function 工具可以；错误 `subscription_sharing_usage_limit_exceeded`（429）、`subscription_sharing_user_not_eligible`（403）。
4. 用户在 ChatGPT「设置 > 用量」里能看到每个应用用了多少，给每个应用设每周上限（只是上限，不是预留）；Plus 的 5 小时窗口是所有应用共用的，Pro 没有 5 小时窗口、只有每周的。

**官方还写了怎么接 Codex app-server**：给子进程设环境变量 `ACCESS_TOKEN`，用 `-c model_provider=…` 把 `base_url` 指到 `https://api.openai.com/v1`、`env_key="ACCESS_TOKEN"`、`wire_api="responses"`、`requires_openai_auth=false`、`supports_websockets=false` 启动 `codex app-server`，就不需要 Codex 自己的登录；token 过期要重启 app-server 再 `thread/resume`。

## 3. 我们能不能做

**能，路径有两条，资格不同：**

- **开源、跑在用户自己电脑上的工具**：不用向 OpenAI 申请，也没有 client secret，第一次登录时自己登记（上面的 `dynamic_agent_client`）。文档原话：「These docs explain ChatGPT plan usage for open-source and locally hosted apps」。WittyCode 是本地桌面应用，**但它现在没有公开**（还没推 GitHub；就算推了，是公开还是私有、什么许可证，由用户定）。帮助页写「Supported open-source tools remain available to all ChatGPT users」，商业应用要走合作方。
- **付费或远程托管的商业应用**（Devin 这种）：要填 OpenAI 的 interest form（https://openai.com/form/sign-in-with-chatgpt-interest/）成为合作方，目前只有首批 16 个。

**能得到什么**（我们已经有 Codex 引擎，它用的就是 ChatGPT 订阅，所以新增的只有下面几件）：

1. **GPT 模型进 Droid 的 harness**：在「API Provider」里加一个「ChatGPT 套餐」来源，Base URL `https://api.openai.com/v1`，密钥用命令（Droid 的 `apiKeyHelper`，WittyCode 的编辑器已经支持「命令」这种密钥来源）每次打印一个刷新过的 access token。**没验证**：Droid 的 OpenAI provider 发的请求是不是满足上面第 3 条的限制（它很可能会带 `temperature`、`max_output_tokens`、system 消息），不满足就不能用；access token 一小时过期，helper 被 Droid 缓存多久也不知道。
2. **WittyCode 自己的模型调用不要 API key**：比如以后「回合结束后提炼值得记的笔记」（记忆的 Phase 5）、给会话起名，可以直接用订阅里的 GPT，用户不用另配 key。这是最干净的用法。
3. **Codex 引擎换成 SIWC 登录**：只有在想让 WittyCode 自己管 token（每应用设上限、在 ChatGPT 设置里单独看用量）时才有意义；现在用 Codex 自己的登录没有问题。

**要付的代价和风险：**

- 要自己存 refresh token（能花用户订阅额度的凭证）：要放在 `%APPDATA%\WittyCode` 里、只用户可读，退出登录要调 `https://auth.openai.com/api/accounts/oauth/revoke`；文档要求 UI 写明「Using ChatGPT plan」，并提供「管理用量」的链接。
- Plus 套餐的 5 小时窗口和 Codex、其他应用**共用**：WittyCode 用多了会让 Codex 也等。
- 回调端口：Codex 自己的登录也用 1455；两个登录同时进行要换端口。
- 这个功能在 OpenAI 那边是 **preview**，限制和错误码还在变。
- 合规：开源本地工具的路径是官方给的；闭源分发是否属于「商业应用」要问 OpenAI，不要自己下结论。

## 4. 建议

先不做。要做的话顺序是：(a) 先查清楚 WittyCode 以什么形式发布（开源与否）；(b) 做最小的 OAuth 登录 + 一个 `core/chatgpt_plan.py`（token 存取、刷新、撤销），用它调一次 Responses API 验证；(c) 再决定是用在 WittyCode 自己的调用（第 2 点），还是接成 Droid 的 Provider（第 1 点，需要先验证请求格式）。
