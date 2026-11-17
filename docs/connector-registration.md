# 内置 Connector 的发布者注册

WittyCode 的内置连接器由**应用发布者注册一次 OAuth 客户端**，普通用户只点「浏览器授权并连接」。不要让每个用户创建 OAuth 应用，也不要借用其他产品的客户端身份。目前提供 GitHub 和 Google Workspace；没有发布者 Client ID 时显示「暂未开放」。

## GitHub：OAuth App + Device Flow

1. 在 GitHub 的 **Settings → Developer settings → OAuth Apps → New OAuth App** 创建自己的应用；组织发布时选择相应的拥有者。
2. 名称使用产品名，Homepage URL 使用自己的产品主页或公开仓库。
3. GitHub 表单需要 Authorization callback URL，可填 `http://127.0.0.1:8766/oauth/callback`。**当前 WittyCode 走 Device Flow，不使用这个回调地址，也不启动该端口的回调服务**；该地址仅与旧版授权配置保持一致。
4. 注册后在应用设置启用 **Device Flow**，记录公开的 **Client ID**。本流程不需要客户端密钥，不要把 Client secret 发给用户或写入仓库。
5. 将 Client ID 配置为下文的 `WITTYCODE_GITHUB_CLIENT_ID`。

实际授权流程：WittyCode 向 GitHub 获取设备码，打开 `https://github.com/login/device`，在应用里显示用户验证码；用户输入并批准后，应用轮询获取令牌。无需公网回调服务。

当前请求范围为 `repo read:user`。`repo` 的服务端授权包含写权限，不能声称是只读 OAuth 授权；WittyCode 现有连接器工具本身只实现搜索和读取。

官方说明：[创建 OAuth App](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)、[授权 OAuth App（含 Device Flow）](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)。

## Google Workspace：Desktop app + PKCE

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 选择或创建自己的项目。
2. 在 **APIs & Services → Library** 启用 **Google Drive API、Gmail API、Google Calendar API**。
3. 在 **Google Auth Platform** 设置 Branding / Audience / Data Access。面向普通公开用户的应用选择 External；仅组织内部使用时可按实际账号条件选择 Internal。开发阶段使用 Testing，并添加测试用户。
4. 在 Clients 创建 OAuth 客户端，类型选 **Desktop app（桌面应用）**，不是 Web application、API key 或 Service account。
5. 记录公开的 Client ID，配置为 `WITTYCODE_GOOGLE_CLIENT_ID`。现有新授权流程使用 PKCE；无需收集或打包客户端密钥。
6. 正式公开前，补齐自己控制的产品主页、隐私政策及所需的授权域名，并按 Google Console 的要求完成发布和验证。

当前使用这些只读范围：

- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/calendar.readonly`

这些范围可能涉及敏感或受限权限审核，具体要求以 Google Console 和 Google 的用户数据政策为准。Testing 不等于可面向所有用户发布；此模式的用户数、令牌有效期等限制也由 Google 决定。

WittyCode 采用系统浏览器、随机端口的 `http://127.0.0.1:<port>/` 回环地址和 PKCE。Desktop app 不需要照 Web application 的方式登记一个固定的线上重定向地址。不要为了迁就已有 Web Client ID 去修改或放宽回调检查。

官方说明：[原生桌面应用 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)。其中令牌交换的 `client_secret` 为可选；WittyCode 使用公开 Client ID 和 `code_verifier`，不把桌面应用当作能保守客户端密钥的机密客户端。

## 给 WittyCode 注入公开 Client ID

### 发布包

在本项目 GitHub 仓库的 **Settings → Secrets and variables → Actions → Variables** 添加两个 Repository variables：

| 变量 | 值 |
| --- | --- |
| `WITTYCODE_GITHUB_CLIENT_ID` | 自己注册的 GitHub OAuth App 的公开 Client ID |
| `WITTYCODE_GOOGLE_CLIENT_ID` | 自己注册的 Google Desktop app 的公开 Client ID |

现有 `release.yml` 会把这两个变量传给发布构建；构建将公开身份写入包内的 `connector-clients.json`，不需要用户自行设置。**更改仓库变量后必须重新构建和发布**；旧安装包不会自动读取 GitHub 仓库变量。

Variables 这里只放公开 Client ID。Client secret、access token 和 refresh token 都不能放到仓库变量、源码、截图或聊天里。

### 源码开发

没有内置客户端身份时，可以仅给当前开发进程设置同名环境变量，启动后端后生效。已发布包优先使用包内身份，环境变量只是开发／打包回退，不覆盖有效的包内配置。不要为了开发改用户的真实设置或凭据文件。

## 注册后的验收

1. 用隔离的测试资料夹启动应用，确认相应 Connector 不再显示「暂未开放」。这仅说明检测到了 Client ID，并不证明 OAuth 应用或范围正确。
2. 点「浏览器授权并连接」，核对服务方显示的应用名称和权限，完成授权。
3. 启用连接器，开新会话或重连，再验证一次自己允许读取的测试资源。凭据由 Windows DPAPI 保护，不要导出或截取令牌。
4. 验证取消、拒绝授权、断开及再次授权。应用内「断开」删除本地凭据，服务端授权需要到相应账号的授权应用页面另行撤销。

真实账号的完整 OAuth 授权与服务访问仍需在发布者完成注册后实测；本机假服务测试不能代替此步骤。本文不会替用户注册应用或修改外部账号。
