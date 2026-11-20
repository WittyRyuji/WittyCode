# 会话默认值、自定义规则与插件

## 会话默认值

在「设置 → 会话」保存模型、推理、权限和模式，供后续新会话使用。对话内的选择只影响该会话；首次发送前在输入框里的选择只影响即将创建的会话，都不会覆盖这里的默认值。

「关于」包含快捷键。设置窗口按原宽高各扩大 25%，小窗口仍受可用空间限制。左侧回合导航保持垂直居中，非当前标记更细、更紧凑。

完成提示音只在整次运行收尾后播放一次：工具／计划子步骤、目标的中间回合和排队消息衔接不响。目标受阻、被停止、回复不完整或步数耗尽也不冒充完成；问题／权限确认音和最终错误音保持独立。

## 自定义规则

「设置 → 自定义」可以选择回复语言、思考与进度语言，并编辑：

- **WittyCode 全局规则**：设置文件同目录下的 `customization/AGENTS.md`；默认位于 `%APPDATA%\WittyCode\customization\AGENTS.md`。不会覆盖其他代理的全局规则。
- **项目规则**：已添加项目根目录的 `AGENTS.md`。只改所选文件，不自动生成其他代理的配置。

规则需要点「保存规则」，下次发送时读取。项目规则比全局规则更具体；实际指令仍受引擎与模型的支持限制。语言选项不保证服务端隐藏推理一定遵循，也不会要求服务端公开隐藏推理。

编辑器限制为 32 KiB，保留已有 UTF-8／UTF-16 BOM、UTF-16 字节序和 CRLF 换行。检测到外部修改时拒绝覆盖；可确认放弃草稿后重新加载。保存进行中暂不允许切页或关闭设置。规则进入模型上下文时仍受原有规则和上下文预算限制。

## 内置连接器

「设置 → 插件 → 连接器」内置 GitHub 和 Google Workspace。应用注册完成后，用户只需点「浏览器授权并连接」，不需要在界面填写 OAuth 客户端、密钥或回调端口。**两家应用尚未注册时，连接器显示暂未开放，不能实际授权。**

### GitHub

点击连接后浏览器打开 GitHub 设备授权页，在网页输入应用显示的一次性验证码并同意授权；无需客户端密钥或远程中转。

申请的 OAuth 范围为 `repo read:user`。**`repo` 本身包含写权限，虽然 WittyCode 当前仅提供只读工具**：代码搜索、仓库文本文件读取、Issue／PR 搜索。是否能读取私有仓库仍受账号、组织与 GitHub 策略限制。

官方配置说明：https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps

### Google Workspace

点击连接后，在浏览器完成 Google 授权；应用以 PKCE 和随机本机回环端口接收结果，不需要客户端密钥或远程中转。

申请 Drive、Gmail、Calendar 的只读范围。工具支持文件搜索、文本与 Google 文档读取、Gmail 搜索与正文读取、日历事件查询。Google Sheets 导出为第一张工作表的 CSV；不提供写入、发送邮件或修改事件。Gmail HTML 正文转为文本，不执行其中脚本或加载外部内容。

Gmail 只读属于受限范围；测试用户、管理员限制与 Google 的验证要求由 WittyCode 的 OAuth 项目决定。

官方说明：https://developers.google.com/identity/protocols/oauth2/native-app

### 发布者注册应用

发布者需先在 GitHub 注册 OAuth App 并启用设备授权；在 Google Cloud 注册 Desktop app，启用 Drive、Gmail、Calendar API 并完成适用的同意屏幕和权限验证。GitHub 设备授权和 Google 桌面公开客户端使用公开 Client ID，不需要在应用内或发布包中保存客户端密钥。

注册后，在发布环境提供公开 Client ID：`WITTYCODE_GITHUB_CLIENT_ID` 和 `WITTYCODE_GOOGLE_CLIENT_ID`。发布工作流从同名仓库变量读取，构建时只把有效的公开 ID 写入发布包；源代码和用户界面不填写它们。开发运行时也可从进程环境读取这两个 ID。未配置的连接器保持不可用，不会冒充授权成功。

### 凭据与断开

Google 授权使用 PKCE、随机 state 和本机回环回调；GitHub 使用设备验证码，两者均可取消等待。Windows 下令牌使用当前用户的 DPAPI 加密，保存在设置目录的 `connector-settings.json`，刷新、配置及断开会跨进程序列化。已有连接及其旧客户端配置保留，不会在安装更新时删除。

停用后不再提供连接器工具；重新启用后按下次连接时的配置生效。「断开」删除本地凭据，**不等于在服务端撤销授权**，需要时还应在 GitHub／Google 的账号设置中撤销。不要把此凭据文件随公开包分发，新环境应重新授权。

## 直接添加 MCP

在「设置 → 插件 → MCP → 添加自定义 MCP」选择全局或项目范围，填写名称与 JSON／TOML 配置，确认来源和执行风险后添加。

支持直接的服务器对象，也支持只含一个服务器的 Codex 风格包装：

```toml
[mcp_servers.demo]
command = "python"
args = ["-m", "demo_tools"]
env_vars = ["DEMO_SERVICE_KEY"]
```

Streamable HTTP 示例：

```toml
[mcp_servers.remote_demo]
url = "https://example.com/mcp"
bearer_token_env_var = "DEMO_MCP_TOKEN"
http_headers = { "X-Client" = "wittycode" }
env_http_headers = { "X-Service-Value" = "DEMO_SERVICE_VALUE" }
```

远端只接受 HTTPS 或明确的本机回环 HTTP。授权值通过环境变量读取，不接受 URL 内账号密码、已识别的凭据查询参数或明文授权头；会拒绝不支持的配置项，不静默忽略。HTTP MCP 通过应用内置的 stdio 适配器供现有引擎使用，支持 JSON／SSE 响应、会话头与关闭会话。不提供通用远程 MCP OAuth 登录，也不把远端自称「只读」当成本地可信权限。

添加配置本身不启动程序，点「测试连接」或会话实际连接时才会启动。配置保存在 WittyCode 自己的插件目录和 `connections.json`，不修改外部 Codex／Claude 配置；启停、编辑、测试和原有导入方式仍可用。

## 官方技能商店

「设置 → 插件 → 技能商店」提供两个公开来源：

- OpenAI：`openai/skills` 的 `skills/.curated`。
- Anthropic：`anthropics/skills` 的 `skills`。

这是官方公开仓库的技能目录，不是账号专属市场，也不等于兼容所有厂商的插件包。目录按提交固定下载来源；先预览技能、许可和文件数量，再明确确认安装。会保留技能资源和仓库许可，不自动运行脚本或安装依赖，不覆盖已有目录。

下载会拒绝路径越界、链接、Windows 数据流及过大的包；安装前再次核对预览指纹。离开页面后完成的下载会取消；最多保留五份未安装预览，退出时清理仍受管理的临时下载，已经确认的安装不会因预览清理被删除。

确认安装后自动注册并启用，立即刷新会话技能列表；用户可在会话输入技能声明的 `/名称`，模型也可按技能声明的调用权限使用。声明为停用或既不允许用户调用也不允许模型调用的技能不会被伪装成可用。两家来源在同一范围有同名技能时选用最新明确安装的来源；目录存在但未启用时可再次预览并确认启用，不覆盖或删除已有文件，本地内容的来源和原始版本不会假称已验证。项目覆盖、来源变更重确认和权限约束不变。使用技能前仍需查看其许可、依赖和具体操作。

## 官方内置技能

「设置 → 插件 → Skills」列表第一行是随客户端分发的官方技能 `numbered-reply`（`desktop/assets/skills/numbered-reply/SKILL.md`），默认启用、标为 WittyCode 官方内置：它要求每个引擎对面向用户的答复先给一段结论、再按 1–N 分述每个事项，向用户提问也逐个按 1–N 列出，便于按编号引用。它不改变明确的格式要求，也不把答复包装成 JSON、代码或内部结构化报告。

开关存入全局设置，立即影响下一条消息，重启后保持。自动注入只读安装包内的这份文件：同名项目或个人技能不会被当成它加载，其 `/numbered-reply` 显式调用仍按普通技能的既有规则；委派给 Fusion 专家的内部消息不注入。
