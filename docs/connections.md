# 本地 Connections 与可选 Factory CLI

## 边界

WittyHarness 是 WittyCode 自己的循环，运行 ChatGPT Plan 与 API Provider；Devin 和 Factory 是可选的外部引擎。不安装它们，应用仍能启动、登录 ChatGPT 或使用 API。共享记忆页面按 **WittyHarness、Devin、Factory** 显示，内部已有的 `chatgpt` 标识不改名，也不迁移用户的数据。

本批兼容 OpenAI / Codex 生态的**本地格式子集**，不是插件商店或远程账号接入。外部配置只读，导入和编辑写在所选 WittyCode 设置目录旁的 `connections.json`，不覆盖 Codex、Factory 或其他客户端的文件。

## 设置 → Connections

可选「全局」或一个已添加的项目，分别查看 Skills、Plugins、Hooks、MCP。项目下同名的 MCP 覆盖全局定义；单独点某一行「测试连接」，测试的是那一行，不是它的同名覆盖项。

- **Skills**：兼容 `SKILL.md`，包括 `.agents/skills`、`.codex/skills` 与原有技能目录。既有技能仍按原规则发现和使用；开关可将其停用，不修改源文件。
- **Plugins**：支持本地目录中的 `.codex-plugin/plugin.json`，以及已有 Claude / Factory 清单标记。读取清单的 `skills`、`hooks`、`mcpServers`，插件总开关同时约束其子项。导入目录内的路径不能越界，链接目录不进入。
- **Hooks**：读取 `hooks.json` 的 `hooks → 事件 → 分组 → hooks`，支持 `type: command`、`command`、Windows 的 `command_windows`、`matcher`、`timeout`（最多 120 秒）。支持 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`、`SessionEnd`。当前只在 WittyHarness 执行；启动事件在会话首次发消息时运行。常见 OpenAI 工具别名可用于匹配；启动／发消息事件可附加上下文，发送前与工具执行前可拒绝操作，但不会越过应用自己的权限判定。执行后的 Hook 失败只附加提示，不撤销已发生的操作；结束事件不实现自动续跑。
- **MCP**：读取 Codex `config.toml` 的 `[mcp_servers.NAME]` 和 JSON 的 `mcpServers`。本批支持 stdio 的 `command`、`args`、`env` 和 `env_vars`；环境值用 `${变量名}`，不导入明文密钥。会话里的进程以项目目录启动；全局行未选项目时，连接测试以来源文件夹启动。独立 `cwd`、工具白／黑名单和自定义 MCP 超时选项暂不实现，发现这些配置会明确拒绝，不把忽略后的结果当成兼容。

### 导入与确认

1. 粘贴配置文件、`SKILL.md` 或本地插件目录，点「预览导入」。这一步不会启动命令。
2. 检查完整的导入项目和 Hook / MCP 命令，勾选一次确认，再「确认导入并启用」。本次包含的可执行配置之后自动启用，不逐个重复询问。
3. 停用再启用未改变的导入项会保留信任。命令编辑需要再次确认；来源配置或插件文件变化后会停用执行，要求重新预览导入。
4. MCP 在新开／重新连接的会话中加载；「测试连接」会立即启动所选服务器、握手并读取工具列表，随后关闭。Hooks 在下一次对应事件生效。

导入是允许执行外部程序，不是沙箱。仅导入信任的来源；本地指纹检查不保证系统里安装的命令或其依赖永远不变。插件商店、远程 MCP / OAuth 和非 command Hook 不在本批范围。

## Factory CLI

在「账号与模型」或「外部程序」指定 `droid` 路径（留空从 PATH 检测），按需启用。先在终端运行 Factory CLI 完成它自己的登录，WittyCode 不复制它的凭据或 API 模型设置。

适配器直接运行：

```text
droid exec --input-format stream-jsonrpc --output-format stream-jsonrpc
```

这不是标准 ACP，也不依赖 Factory SDK；Devin 仍使用自己的 ACP 接口。支持模型目录、会话、文本和附件、审批、提问、停止以及本应用会话索引；Factory 的权限策略由外部 CLI 负责。当前引导没有可关联的采用回显，使用排队，不伪造成功。发送或停止结果无法确认时关闭连接并要求重连，不把超时误报为可以继续发送。

Factory 的真实账号和服务尚未验证；当前证据来自本机假 CLI 的真实子进程协议测试与 Qt 窗口测试。不是对任意未来 CLI 版本的兼容保证。

## 模型用量

已知的每模型明细保存在本地会话和应用账本里，关闭／重启后保留；旧记录从未保存的明细列为「未归属」，不按当前模型猜填。图表采用明亮柔和粉彩和渐变；排序变化不换色，同一图内避碰。ChatGPT 登录没有订阅剩余额度接口，因此不再显示空额度圆环。
