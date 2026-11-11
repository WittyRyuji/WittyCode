# WittyCode 前端

React 19 + TypeScript + Vite。同一个页面有两种运行方式，打开时（第一次渲染之前，`src/mode.ts`）决定：

- **正式界面（live）**：在 WittyCode 的 Qt WebEngine 窗口里（页面里有 `qt.webChannelTransport`，或 URL 带 `?live=1`）。数据全部来自 Python 的 `AppCore`（`desktop/core/app_core.py`），每个动作是一次对 Python 的调用（`src/live.ts`），Python 的事件改页面的状态（`src/liveReducer.ts`）。`app.init` 回答之前只画标题栏，没有任何演示数据，也没有审阅栏。
- **设计审阅（mock）**：在普通浏览器里打开。真实的 React 组件 + 模拟数据（`src/mock/`）。

## 看效果

- 单文件版（不用服务器、不用网络，双击即可）：`npm run build` 后的 `dist/index.html`，已复制一份到 `../../outputs/wittycode-design-review.html`。
- 开发：`npm run dev`，浏览器打开提示的地址。
- 页面顶部的黑色「设计审阅」栏只在原型里有：切换主题、窗口宽度、跳到各个场景、看设计规范。点「隐藏」可以去掉它和模拟的窗口外框。
- URL 参数（用于截图和固定在某个状态）：`?scene=done|long|live|approve|ask|dead|empty|none|codex|devin|api&switch=codex|devin|droid（打开后立刻在对话里换到 Codex / Devin / 再换回 Droid）&theme=light|dark&fs=sm|md|lg&expand=groups|all&settings=appearance|providers|defaults|droid|about|shortcuts&dialog=1&lightbox=1&drop=1&notice=1&guide=1&width=800&review=0&side=0`。
- 账号与模型页的状态：`codex=signed-out|signing-in|api-key|missing`（Codex 未登录 / 登录中 / 用 API Key / 没装命令行）、`devin=signed-out|signing-in|missing`（Devin 同理）、`providers=none`（没有 API Provider）、`provider=<id>` 或 `provider=new:<预设>`（直接打开编辑器，预设有 `openai anthropic openrouter fireworks groq gemini ollama lmstudio bedrock custom`）。`settings=providers` 打开这一页。

## 和 Python 的接口（正式界面）

Python 在 QWebChannel 上注册一个对象 `backend`（`desktop/ui/shell.py`），并在页面运行前注入 Qt 的 `qwebchannel.js`：页面调 `backend.call(method, paramsJson, callId)`，Python 用信号 `reply(callId, ok, json)` 回答（`ok` 为假时 `json` 是给用户看的一句中文，页面放到状态栏），有变化时发 `event(name, json)`。全部调用和事件的类型在 `src/protocol.ts`（照 `app_core.py` 写的），传输在 `src/transport.ts`（每次调用一个 Promise；除了登录和原生文件对话框，120 秒没有回答算失败）。

- 调用：`app.init`、`project.*`、`session.*`（新建、打开已保存的、关闭、停止、重连、发送、回答批准和提问、`choose` 改模型 / 推理 / 权限 / 模式，没有 id 时改新会话的默认值）、`attachments.*`（附件由 Python 收下、压缩、保存；页面把拖入 / 粘贴的文件读成 base64 交给它，超过 64 MiB 的不读；📎 用 Python 的文件对话框）、`settings.set`、`factory.account`、`codex.*`、`devin.*`、`providers.*`、`window.*`、`files.*`（助手文字里点到的文件：`files.read` 给右侧栏的文本，`files.open` 的 `how` = `default` / `choose` / `reveal`，`files.resolve` 给复制路径；相对路径按会话的项目文件夹找，程序和脚本不会因默认打开而运行）、`shell.openExternal`（页面里的链接只交给系统浏览器，只认 http / https / mailto，页面自己从不跳转）。
- 事件：`notice`、`tabs`、`sidebar`、`models`、`defaults`、`session`、`turns`、`restore`、`settings`、`accounts`、`window`、`closeRequested`。
- id：项目就是文件夹路径；侧栏里的行是已保存的会话（`s-...`，Codex 的是 `cx:...`，Devin 的是 `dv:...`）或打开着的对话（`c1`、`c2`…）。Python 的轮次和块按对话编号（每个对话都有 `b3`），所以展开状态按对话分开记。
- 时间：Python 给 `modifiedAt`（毫秒），「刚刚 / 5 分钟前 / 昨天 14:20」由页面自己写（`src/lib/time.ts`，与 `timefmt.py` 一致），每 30 秒刷新。
- 窗口：页面画标题栏，四边 6 px、四角 10 px 的隐形缩放区按下时调 `window.startResize`（最大化时没有）；快捷键 Ctrl+N / W / O / L / End / B / , / Q、F5、Esc 在正式界面里都有效。

## 账号与模型

下面是 `src/bridge.ts` 里的那部分（组件直接用），原型里由 `src/mock/bridge.ts` 假装回答，正式界面里是 `src/liveBridge.ts`：

| 调用 | 作用 |
| --- | --- |
| `factory.account()` | 找到的 droid.exe 和它的版本（原型里还有演示用的邮箱、套餐，真正的 droid 不报告这些） |
| `codex.account()` / `login()` / `cancelLogin()` / `logout()` / `setEnabled(on)` | Codex 订阅：命令行是否存在、登录状态、套餐、5 小时 / 每周额度；登录打开浏览器，完成后返回新状态 |
| `devin.account()` / `login()` / `cancelLogin()` / `logout()` / `setEnabled(on)` | Devin 账号：命令行是否存在、登录状态（邮箱、套餐）；登录运行 `devin auth login`（它自己开浏览器），完成后返回新状态；Devin 桌面版和命令行共用这个登录 |
| `providers.save(draft)` / `remove(id)` | API Provider 写进 / 删出 `~/.factory/settings.json` 的 `customModels`。**密钥只放环境变量**：`apiKey` 写 `${变量名}`（编辑器的「环境变量」栏只让用户写变量名本身，不带 `${}`，`lib/models.ts` 的 `envVarText` / `envHintOf` 在显示和存储之间转换），页面里没有输入密钥的地方，配置里不会出现密钥原文；已有的明文密钥原样保留、界面标出警告，用户改成环境变量并保存后才从配置里删掉 |
| `providers.test(draft)` / `listModels(draft)` | 测试连接（耗时、模型数、失败原因）；向服务端要模型 ID 列表 |
| `providers.envStatus(names)` | 每个变量名的状态：`set`（WittyCode 启动的 Droid 读得到）、`pending`（已写进用户环境变量，但 WittyCode 启动在先，需重启）、`unset`（没有）。没设置时界面给出可复制的 `setx` 命令 |
| `window.minimize()` / `toggleMaximize()` / `close()` / `startMove()` / `startResize(edge)` / `isMaximized()` | **标题栏由页面自绘**（Qt 窗口无边框）：图标 + 名称 + 应用菜单 + 可拖动空白 + 三个窗口按钮。按下标题栏空白处调 `startMove()`（Python 用 `QWindow.startSystemMove()`，保留 Windows 贴边和分屏），边缘缩放用 `startSystemResize` |

要点：Codex 订阅是**另一个引擎**（`codex app-server`，登录令牌由 Codex 自己存在 `~/.codex/auth.json`），它的会话和 Droid 的会话各存各的，互相读不到对方的上下文；Devin 账号是**第三个引擎**（`devin acp`，Agent Client Protocol；模型各有自己的思考档位，权限档位就是 Devin 的模式：问答 / 计划 / 写代码 / 智能 / 完全访问）；API Provider 是 Droid 自带的 BYOK（`customModels`），由 Droid 运行。因此模型菜单按来源分组（FACTORY / CODEX 订阅 / DEVIN / API · 名称），权限档位也按引擎各有一套。

**同一个对话里可以换引擎（Droid ⇄ Codex ⇄ Devin，三者两两可换）**：选另一边的模型后，对话留在原来的标签里，插入一条「换引擎」分隔线（从哪个模型换到哪个、带过去多少条文字、哪些不带），状态栏的 Context 重新计、Token 接着累加（整个对话的总和，不因换引擎、断开、重开归零），权限回到新引擎的第一档；下一条消息带上之前的文字（只含用户消息和助手回答，≤ 200 条 / 64 KiB，超出标「节选」，`src/lib/handoff.ts`），发出去的那条消息下面标「附带了之前 N 条对话的文字」。空会话换引擎不插分隔线；一轮还在运行时另一边的模型是灰的。Python 侧要做的：自己存对话文字，一个对话对应多段原生会话（Droid 一段、Codex 一段），换引擎时开新的原生会话并在下一条消息前拼上交接文字，这一轮成功后才算交接完成。同引擎内换模型（例如 Droid 内部）照旧，不插分隔线。

## 命令

| 命令 | 作用 |
| --- | --- |
| `npm run build` | 类型检查 + 打包成单个 `dist/index.html`（脚本、样式、字体、图片全部内联；带 Content-Security-Policy，只允许内联脚本、样式和 `data:` 图片、字体，不许任何网络） |
| `npm run build:app` | 同上，再检查它不引用任何外部文件、CSP 在所有脚本和样式之前，然后复制到 `../desktop/ui_dist/index.html`（WittyCode 加载的就是它） |
| `npm run typecheck` | 只做类型检查 |
| `node scripts/unit.mjs`（`npm run unit`） | `src/liveReducer.ts` 和 `src/lib/time.ts` 的单元测试，Node 直接跑 .ts，不用装别的 |
| `node scripts/check.mjs` | 设计审阅页：用真实鼠标键盘事件（Edge + 自带的 CDP 驱动 `scripts/cdp.mjs`）检查 149 项交互（含 Codex 登录、API Provider 增删改测、环境变量状态、自绘标题栏、模型菜单分组、同对话换引擎、单文件和 CSP），需先 `npm run build` |
| `node scripts/check-live.mjs` | 正式界面：Qt 自己的 `qwebchannel.js`（从 `../.venv` 的 PySide6 里读出来，没有就用一个同协议的替身）加上 `scripts/fake-backend.mjs`（照 `app_core.py` 回答、播放一段对话的假 Python），检查 114 项（没有演示数据、标签、流式文字、批准、提问、附件、换引擎、链接、快捷键、设置、缩放区、关窗口询问、时间刷新），截图 `shots/live-*.png` |
| `node scripts/shots.mjs [过滤词]` | 设计审阅页深色 / 浅色各 42 张截图，输出到 `shots/` |

## 结构

| 位置 | 作用 |
| --- | --- |
| `src/styles/tokens.css` | **设计变量**：颜色、字号、线宽、形状。组件只用变量，深色主题只是换一组值 |
| `src/styles/fonts.css` | 字体：Inter（界面和正文）、JetBrains Mono（代码、路径、数字），woff2 随程序打包；中文用系统的微软雅黑 UI |
| `src/types.ts` | 界面渲染的数据结构，对应 `desktop/core/viewmodel.py`（`transcript.py`、`prompts.py` 的 JSON）；也含引擎、模型来源、Codex 账号、API Provider |
| `src/mode.ts` | 正式界面还是设计审阅：第一次渲染之前决定 |
| `src/protocol.ts`、`src/transport.ts` | 和 Python 之间的全部调用、事件的类型；QWebChannel 传输 |
| `src/live.ts`、`src/liveReducer.ts`、`src/liveBridge.ts` | 正式界面的动作（调用 Python）；事件怎样改状态（纯函数，有单元测试）；组件用的 Bridge |
| `src/bridge.ts`、`src/bridge-instance.ts` | 账号、Provider、窗口这部分接口，和按模式选出的实现 |
| `src/store.ts` | 全部状态和动作（zustand）。这里的动作是设计审阅的；正式界面启动时换成 `live.ts` 里的 |
| `src/lib/vocab.ts` | 推理档位、权限档位、模式、接口类型、Provider 预设的用词（两种模式共用） |
| `src/mock/` | 模拟数据和模拟 Droid（`simulator.ts`：发消息后流式回答、申请批准；`attachments.ts`：在页面里读附件） |
| `src/components/` | TitleBar（自绘标题栏）、ResizeHandles（无边框窗口的缩放区）、Sidebar、Tabs、Transcript（`blocks/`）、Composer、StatusBar、Settings、ProvidersPage、ProviderEditor、Overlays、Brand |
| `scripts/` | `cdp.mjs`（Edge 驱动）、`check.mjs`、`check-live.mjs`、`fake-backend.mjs`、`unit.mjs`、`shots.mjs`、`copy-app.mjs` 和 `selfcontained.mjs`（`build:app` 的复制和检查） |
| `src/assets/brand/` | 方形图标（`LOGO_ICON.png`）和从「WITTY LOONG 白色LOGO.svg」抽出的龙头、字标路径 |

## 设计依据

界面从 WITTY LOONG 的两个 Logo 推出来：纯黑白、粗线条、直角、方框、反白字牌。没有强调色，「强调」就是墨色本身；只有表达含义的颜色（会话状态、成功 / 警告 / 错误、diff）。细节见页面里的「设计规范」。
