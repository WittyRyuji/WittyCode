# ChatGPT Plan 引擎（自有 harness）设计

状态：2026-11-04 开始实现。本文是实现约定，也是模块之间的接口合同。

## 1. 决定（用户，2026-11-04）

1. 直接调 `POST https://api.openai.com/v1/responses`，由我们自己的 harness（代理循环）处理 ChatGPT 订阅，不经过 `codex app-server`。
2. 核心开源，没有问题；以后要收费或做托管，再走 OpenAI 的兴趣表单申请。
3. 现有方式（Codex 引擎用本机 `codex login`）保留，新增一种接入方式：**ChatGPT Plan**。模型菜单里单独一节，标题「ChatGPT Plan」。

## 2. 官方规范（2026-11-04 读过原文）

来源：`https://developers.openai.com/siwc`（Sign in with ChatGPT）下「ChatGPT plan usage」各页，均可在 URL 后加 `.md` 读原文。

- 资格：**ChatGPT 套餐用量对所有开源应用开放**；付费或远程托管应用要填兴趣表单（`https://openai.com/form/sign-in-with-chatgpt-interest/`）。「开源」的精确定义没在这几页里写，上线前要确认（需要公开仓库和许可证）。
- 流程：OAuth 2.0 授权码 + PKCE（S256），公开客户端，**没有 client secret**。首次登录 `client_id=dynamic_agent_client`（动态注册），回调里返回签发的 `client_id`（`oaiapp_...`），之后重新授权用它。
- 授权端点 `https://auth.openai.com/api/accounts/authorize`，令牌端点 `.../api/accounts/oauth/token`，撤销端点 `.../api/accounts/oauth/revoke`，JWKS `https://auth.openai.com/.well-known/jwks.json`（发现文档 `.well-known/openid-configuration`，ID token 只用 **RS256**）。
- 授权参数：`response_type=code`，`redirect_uri=http://127.0.0.1:<端口>/auth/callback`（必须 `127.0.0.1`，不能 `localhost`；路径固定 `/auth/callback`；首次注册后端口可以变，但同一次尝试里授权和换码用同一个 URI），`scope=openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`，`resource=https://api.openai.com/v1`，`state`、`nonce`、`code_challenge`（S256），首次另加 `agent_name_hint`（应用名，始终一致）和 `ext_agent_host_id`（每台机器一个稳定的 `urn:uuid:<uuid4>`，**首次登录前就要选好并保存**，所有登录都带）。重新授权不带 `agent_name_hint`，可带 `id_token_hint`（上次的 ID token，跳过账号选择）和 `login_hint`（邮箱）。
- 回调：`?code=&state=&client_id=<签发的>&scope=...`；用户拒绝是 `error=access_denied`。校验 `state`；回调里的 `client_id` 与已知的不同就拒绝。
- 换码：POST 表单到令牌端点，`grant_type=authorization_code`、`client_id`（签发的）、`code`、`code_verifier`、同一个 `redirect_uri`、同一个 `resource`。返回 `access_token`（1 小时）、`refresh_token`（30 天，每次刷新换新的）、`id_token`、`token_type`、`expires_in`、`scope`、`earliest_refresh_at`。
- 校验：ID token 验签（JWKS）、`iss=https://auth.openai.com`、`aud`=签发的 `client_id`、`exp`、`nonce`；用 `sub` 当账号标识；**授予的 scope 里必须有 `chatgpt.tokens.use.direct`**，否则只算登录成功、套餐用量未开（引导用户重新授权，不要往下推理）。
- 刷新：POST 表单 `grant_type=refresh_token`、签发的 `client_id`、`refresh_token`、`resource`（不带 `scope`）；**同一会话的刷新要串行**，用返回的新 refresh token 整体替换。不可用的刷新令牌：`invalid_grant`、`invalid_refresh_token`、`token_expired`、`refresh_token_expired`、`refresh_token_invalidated`、`refresh_token_reused`（清掉令牌，重新走授权）；`invalid_client` 是配置错。
- 退出：向撤销端点 POST 表单 `token=<refresh_token>`、`token_type_hint=refresh_token`、`client_id`；空 `200` 就是成功；网络错或 5xx 退避重试，最终不确认也要清本地令牌并告诉用户（可在 ChatGPT 设置里断开）。保留账号和 client 的对应以及主机 ID。
- 凭据文件：每个签发的 `client_id` 一份，存 email、issuer、subject、client_id、ext_agent_host_id、id_token、access_token、refresh_token、token_type、expires_in、scopes、saved_at；原子写；不进日志。
- 推理：`POST https://api.openai.com/v1/responses`，`Authorization: Bearer <access_token>`，**`store:false`、`stream:true`**，`input` 是数组（历史自己带），用 `instructions` 或 developer 消息（`role:"system"` 的 message 项会被拒）。不要用 ChatGPT 的 `backend-api`。
  - 不支持的字段：`background`、`conversation`、`max_output_tokens`、`max_tool_calls`、`metadata`、`moderation`、`multi_agent`、`prompt`、`prompt_cache_retention`、`safety_identifier`、`temperature`、`top_logprobs`、`top_p`、`truncation`、`user`；HTTP 下不要 `previous_response_id`。
  - 不支持的工具：图片生成、文件搜索、Code Interpreter、原生 computer use、托管 MCP / 连接器、`tool_search`。文档写「函数/自定义工具放进 namespace，或用 `additional_tools` 输入项提供」——**具体是否允许顶层平铺的 `function` 工具没写明，待真实令牌核实**（Codex 自己对内置工具平铺、对 MCP 工具用 `{"type":"namespace","name":"mcp__<server>__","tools":[...]}`）。
  - 模型列表：`GET https://api.openai.com/v1/models`，返回 `{"models":[{slug, display_name, visibility, ...}]}`，`visibility=="list"` 的才显示；其余字段（推理档位、上下文窗口）**待真实令牌核实**。
  - 错误：流开始前可能是 `{"detail": "..."}`（401/403/503）；流里 `response.failed` 带 `error.code`：`subscription_sharing_user_not_eligible`(403)、`subscription_sharing_usage_limit_exceeded`(429)、`subscription_sharing_usage_unavailable`(503)、`subscription_sharing_unsupported_capability`(400，看 `error.param`)、`subscription_sharing_route_not_supported`(403)、`subscription_sharing_invalid_user`(401)、`chatpass_v2_scope_not_authorized` / `chatpass_v2_invalid_authorization_context`(403)、`subscription_sharing_user_unavailable`(503)。保留 HTTP 状态、错误码、request id。OpenAI **不会**悄悄换成别的计费方式。
  - Plus 的五小时额度由所有应用共用。用户在 `https://chatgpt.com/settings/usage` 给每个应用设上限。
- 界面要求：按钮文字 **Continue with ChatGPT**（官方品牌样式）；首次登录后弹一次「You're using your ChatGPT plan / Got it」；输入框或模型选择附近显示 **Using ChatGPT plan** + **Manage usage** 链接；额度用尽时主操作是「Manage usage」；设置里说明与应用自己的计费是两回事。

## 3. 模块

引擎标识 `chatgpt`，页面模型 id 前缀 `chatgpt:`，侧栏会话行前缀 `cg:`，显示名「ChatGPT Plan」。

```
desktop/core/chatgpt/        与 OpenAI 打交道（不含代理循环）
  errors.py     ChatGPTError / AuthError，friendly_failure（中文提示）
  http.py       标准库 urllib 在线程里跑的异步 HTTP（JSON、表单、SSE 行流）；系统代理自动生效
  jwt.py        解 JWT、用 JWK 验 RS256（纯 Python，没有 cryptography）
  oauth.py      SIWC：Endpoints、SignIn（回环监听 + 浏览器 + 换码 + 校验）、refresh、revoke
  accounts.py   AccountRecord、AccountStore（凭据 + 主机 ID）、TokenProvider（到期前刷新，串行）
  models.py     /v1/models 的解析 + 内置兜底清单
  responses.py  Responses 请求构造 + SSE 事件解析成中立事件
  engine.py     ChatGPTEngine：账号、模型、会话列表、打开/关闭（对 AppCore 的接口，仿 DevinEngine）
  controller.py ChatGPTController：一个会话（仿 DevinController 的公开面）
  history.py    已存会话 -> Transcript 回合
desktop/core/harness/        引擎无关的代理循环
  tools.py      ToolSpec / ToolContext / ToolResult / ToolRegistry
  fs_tools.py   Read / LS / Glob / Grep / Create / Edit
  shell.py      Execute（PowerShell）
  permissions.py 权限档与决定、ActionView
  mcp.py        最小 MCP stdio 客户端（接记忆服务器）
  loop.py       代理循环
  store.py      会话存档
```

### 3.1 合同：`core/chatgpt`（登录与令牌）

```python
# errors.py
class ChatGPTError(Exception):
    message: str; code: str | None; status: int | None; request_id: str | None; body: Any
class AuthError(ChatGPTError):
    terminal: bool          # True: 令牌不可再用，要重新走授权
class PlanNotEnabled(AuthError)   # 授予的 scope 里没有 chatgpt.tokens.use.direct

# http.py
@dataclass class HttpResponse: status: int; headers: dict[str, str]; body: bytes; def json(self) -> Any; def text(self) -> str
class HttpClient:
    async def request(self, method, url, *, headers=None, form=None, json_body=None, timeout=30.0) -> HttpResponse
        # 不因 HTTP 状态码抛错；连不上 / 超时抛 ChatGPTError(code="network")
    async def stream_lines(self, method, url, *, headers=None, json_body=None, timeout=...) -> AsyncIterator[str]
        # 状态码 >= 400 时读完 body 抛 ChatGPTError（带 status、request id、解析出的 error.code / detail）

# oauth.py
PLAN_SCOPE = "chatgpt.tokens.use.direct"
@dataclass(frozen=True) class Endpoints: issuer, authorize, token, revoke, jwks, resource ; @classmethod default()
class SignIn:
    def __init__(self, http, endpoints, *, host_id, app_name, client_id=None, id_token_hint=None, login_hint=None,
                 open_browser, port=0, store=None, clock=time.time)
    async def run(self, timeout=300.0) -> AccountRecord     # 回环监听 -> open_browser(url) -> 回调 -> 校验 -> 换码 -> 验 ID token
    def cancel(self) -> None
async def refresh(http, endpoints, record, *, clock=...) -> AccountRecord
async def revoke(http, endpoints, record) -> bool          # True: 远端确认；False: 没确认（本地仍应清除）

# accounts.py
@dataclass(frozen=True) class AccountRecord:
    email, issuer, subject, client_id, ext_agent_host_id, id_token, access_token, refresh_token, token_type,
    expires_in, scopes: tuple[str, ...], saved_at: float          # 外加 property expires_at / plan_enabled / label
class AccountStore:
    def __init__(self, root: Path)                                # root = <设置目录>/chatgpt
    def host_id(self) -> str                                      # 首次调用生成 urn:uuid:... 并保存
    def accounts(self) -> list[AccountRecord]; def get(client_id); def save(record); def remove(client_id)
    def active_id(self) -> str | None; def set_active(client_id)
class TokenProvider:
    def __init__(self, store, http, endpoints, *, clock=time.time, margin=300.0)
    async def access_token(self, client_id=None) -> str           # 剩余 < margin 就刷新；同一账号串行；先重读磁盘
```

### 3.2 合同：`core/harness`（工具与权限）

```python
# tools.py
@dataclass(frozen=True) class ToolSpec:
    name: str            # 模型看到的名字（snake_case）
    description: str; parameters: dict      # JSON schema
    ui_name: str         # 页面用的名字（Droid 风格：Read LS Glob Grep Create Edit Execute），toolinfo 靠它分类
    kind: str            # "read" | "edit" | "exec"
@dataclass class ToolContext: project: Path; abort: threading.Event; shell: str = "powershell"
@dataclass class ToolResult: output: str; is_error: bool = False; diffs: tuple[FileDiff, ...] = (); data: dict = {}   # data: 页面上 ToolItem 的 input（ui_input 的结果）
class ToolRegistry:
    def specs(self) -> list[ToolSpec]; def get(name) -> ToolSpec | None
    def ui_input(self, name, args) -> dict          # 页面 ToolItem 的 input（键名跟 toolinfo.describe_input 对得上）
    async def run(self, name, args, ctx) -> ToolResult
# 工具： read_file→Read{file_path,offset,limit}、list_dir→LS{directory_path}、glob→Glob{patterns,folder}、grep→Grep{pattern,path,glob_pattern,...}、
#       create_file→Create{file_path,content}、edit_file→Edit{file_path,old_str,new_str,change_all}、shell→Execute{command,cwd,timeout}
# permissions.py
LEVELS = ("ask", "code", "full")
@dataclass(frozen=True) class Decision: verdict: str   # "allow" | "ask" | "deny"
                                       reason: str
def decide(level, spec, args, ctx, *, remembered: set[str]) -> Decision
def remember_key(spec, args, ctx) -> str                # 「本会话内同类操作不再询问」的键
def action_view(tool_use_id, spec, args, ctx) -> ActionView
```

### 3.3 权限档

`ask`（问答）：只读工具直接放行，写文件和运行命令都问。`code`（写代码，默认）：项目目录内的读、写、改直接放行，运行命令问（只读命令白名单除外），项目外一律问。`full`（完全访问）：都放行。**没有操作系统沙箱**；路径只做项目范围检查。「本会话内同类操作不再询问」按 `remember_key` 记在会话里，不写盘。

### 3.4 会话存档

`<设置目录>\chatgpt\sessions\<uuid>.json`：`{version, id, project, title, model, reasoning, autonomy, created_at, modified_at, items: [Responses 输入项], usage: {…}, ledger…}`，加 `index.json`（每个项目的会话行，供侧栏）。原子写。`items` 是下一次请求要带的完整历史（含 `reasoning` 项的加密内容，若这条路允许 `include`），页面的回合由 `history.py` 从 `items` 重建。

## 4. 待真实令牌核实

登录一次后用便宜的模型逐项试。**脚本已经写好：`python sandbox/chatgpt_real_probe.py`**（读 WittyCode 保存的账号，发 7 个很小的请求：默认形状、命名空间工具、不要加密推理、不要推理摘要、关并行工具调用、带 `prompt_cache_key`、一次完整的工具往返；打印每一项被接受还是被拒，保存原始模型列表到 `sandbox/chatgpt_probe/models.json`，不打印令牌；用本地假服务器试跑过：`python sandbox/chatgpt_probe_dryrun.py`）。结果记回本文：

1. `/v1/models` 的字段：推理档位、默认档位、上下文窗口、是否支持图片。
2. `tools` 能不能顶层平铺 `function`；MCP 工具要不要 namespace。
3. `include: ["reasoning.encrypted_content"]` 是否被接受；不接受就丢掉 reasoning 项。
4. `reasoning: {effort, summary}` 的取值；`parallel_tool_calls`、`tool_choice` 是否可用。
5. `usage` 里 `input_tokens_details.cached_tokens`、`output_tokens_details.reasoning_tokens` 的形状。
6. 刷新令牌换新后旧的是否立刻失效（决定刷新的串行方式要多严）。
7. 「开源」资格的具体要求。

## 5. 阶段

A 登录与令牌（`core/chatgpt` 的 errors / http / jwt / oauth / accounts + 假 OAuth 服务器）→ B 工具与权限（`core/harness`）→ C 循环与引擎接入 AppCore → D 前端 → 检查点：真实登录核实第 4 节 → 测试、变异、文档。
