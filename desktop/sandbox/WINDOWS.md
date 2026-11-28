# desktop.sandbox.windows — Windows 11 MXC 后端

用 Microsoft 官方 MXC（Microsoft eXecution Container，github.com/microsoft/mxc）
的 `processcontainer` 后端在 Windows 11 上执行一条命令。绝不回退为普通进程：
依赖缺失、探测失败或原生拒绝都显式报错，不启动任何子进程。

## 架构

```
core.run → windows.execute ─┐
                            ▼
        node.exe（可信、>=24.21）运行 mxc.mts（stdin 传 JSON）
                            ▼
        @microsoft/mxc-sdk /v1（koffi FFI → mxc_ffi.dll → wxc-exec）
                            ▼
        Windows ProcessContainer（base-container 隔离层）
```

- `windows.py`：解析依赖、构造 `ContainerRequest`、驱动 helper、限量捕获输出、
  超时杀进程树并抛 `subprocess.TimeoutExpired`。仅 Python 标准库。
- `mxc.mts`：Node 24+ 直接运行的 TypeScript 桥；`action=probe` 调原生
  `probe()`，`action=run` 先对实际请求做原生 `probe`（拒绝则不启动），再
  `run(request)`，把 `{refused, exitCode, timedOut, warnings, stdoutB64,
  stderrB64}` 打成单行 JSON 帧写 stdout——SDK 完整捕获的 workload 流在
  helper 内各截前 512 KiB、Base64 编码，帧恒完整且远小于宿主 4 MiB 读取
  上限；退出码与超时是字段而非 helper 自己的退出码。宿主不读写 guest 可
  达的任何控制文件：状态只走 helper 的 stdout 管道。

## 依赖与信任模型

后端只认显式、绝对、可解析的路径，永不查 PATH 或工作区里的同名 launcher：

| 项 | 环境变量 | 固定安装位置（依次探测） |
| --- | --- | --- |
| Node | `WITTYCODE_MXC_NODE` → `node.exe` 绝对路径 | `%ProgramData%\WittyCode\mxc\nodejs\node.exe`，再 `%LOCALAPPDATA%\WittyCode\mxc\nodejs\node.exe` |
| SDK | `WITTYCODE_MXC_SDK` → 含 `node_modules\@microsoft\mxc-sdk` 的目录 | 同两根下的 `sdk\` |

- Node 需 **24.21+**（或 26.8+；SDK 的 native stdio 门槛）。`probe()` 会在干净环境里跑 `node --version` 验证。
- SDK 为 npm 官方包 `@microsoft/mxc-sdk@1.1.0`（含运行时依赖 koffi、semver、node-pty 与 `@koromix/koffi-win32-x64` 原生包），预期为校验过完整性（固定 SHA-512）的解包目录，不跑任何 npm 安装脚本。包内 `bin\x64\wxc-exec.exe` 等均为 Microsoft 签名（测试里逐一看过 `Get-AuthenticodeSignature` → `Valid | CN=Microsoft Corporation`）。
- 解析出的 node、SDK 目录与 helper 绝不允许落在本次运行的 workspace 或 scratch 内，也不允许 workspace/scratch 反向落在它们里面（如 workspace 放在 SDK 目录下）——`execute()` 对两类重叠都直接拒绝，否则被沙箱化的工作负载可反向改写可信侧。
- 不向用户环境安装；测试把所有下载物放在 `WITTYCODE_TEST_WORK` 下。

## 策略映射

| 后端参数 | ContainerRequest 字段 |
| --- | --- |
| workspace | `workingDirectory`；`writable` → `readwritePaths`，否则 `readonlyPaths` |
| scratch | `readwritePaths`（私有可写），并兜底 `environment.LOCALAPPDATA`（Windows 要求该变量存在） |
| readable | 追加 `readonlyPaths` |
| protected | `deniedPaths` |
| env | `environment` 逐字 + `inheritDefaultEnvironment: false` |
| timeout | `timeoutMs`（原生杀树）+ 父侧 deadline + `taskkill /T /F` |
| 网络 | `egress.default=deny`、`ingress.default=deny` + `hostLoopback=deny` |
| UI | `disable`、`clipboard=none`、禁输入注入 |

`processcontainer.config.filesystem.enumeratePaths` 自动加入所有授权路径的祖先目录（只给枚举/遍历权，不给内容读），否则授权根下的文件因父目录不可遍历仍打不开。workload 的 stdout/stderr 由 SDK 完整捕获、helper 各截前 512 KiB Base64 回传（超出部分即丢失——这是明确的截断而非损坏，帧本身始终完整）；helper 进程的 cwd 与 TEMP 用 scratch 兄弟目录下的宿主私有临时目录，绝不落在 guest grant 上。

## 已验证（本机 Windows 11，真实 base-container）

托管 runner + `WITTYCODE_TEST_MXC=1`，11/11 通过：探测就绪（`base-container`）、echo/env 透传、workspace 写、scratch 写、workspace 只读+`readable` 授权读、未授权邻居目录拒读、`protected` 拒读写且宿主文件不变、单线程回环对照（宿主 `connect_ex` 返回 0 且 listener 接入；容器内同一 `python.exe` 返回非零 errno、子进程正常退出 0、listener 无第二次接入）、公共 `core.run` 绝对路径 Python 子进程 workspace 内可写/邻居拒读、guest 在 scratch 写同名 `mxc-report.json` 不被宿主读写或删除且 >1 MiB 输出截断为 512 KiB 不损坏、无限循环 6s 超时即杀；`WITTYCODE_MXC_NODE` 指向不存在路径时 `probe()` 失败且 `execute()` 不启动任何进程。

## 测试

```powershell
# 默认：全部跳过、零下载、零网络
python desktop/core/test_runs.py -- python -m desktop.sandbox.test_windows -v

# 本机原生验证：下载固定摘要的 Node 与 npm 包到 WITTYCODE_TEST_WORK 后实测
$env:WITTYCODE_TEST_MXC='1'
python desktop/core/test_runs.py -- python -m desktop.sandbox.test_windows -v
```

固定的包（npm tgz，sha512 校验后解包，不跑安装脚本）：`@microsoft/mxc-sdk@1.1.0`、`koffi@3.3.8`、`semver@7.7.4`、`node-pty@1.1.0`、`@koromix/koffi-win32-x64@3.3.8`。本机 Node 低于 24.21 时，测试再从 nodejs.org 下载 latest-v24.x 的 win-x64 zip（`SHASUMS256.txt` 固定摘要 + Authenticode 验签）。

## 已知限制

- 容器里只有 cmd 等极简依赖的可执行体能启动；`powershell`、`curl`、`node` 在 base-container 内 `STATUS_DLL_INIT_FAILED`（0xC0000142）崩溃，不能用它们做命令负载或网络探针——网络用例改用受信 `python.exe`（read-only 授权）+ `connect_ex`。
- `%TEMP%` 被 AppContainer 虚拟化；要写真实 scratch 用其绝对路径。
- workload 的 stdout/stderr 超过 helper 各 512 KiB 上限的部分被丢弃（输出截断，结果帧不损坏）。
- workload 真实退出码 124 与超时语义已由结构化字段区分，无歧义。
- 已实测 `python.exe -I -S` 可在容器内运行并创建 socket（`connect_ex` 返回 errno 而非崩溃）；`powershell`/`curl`/`node` 在 base-container 内启动即 `STATUS_DLL_INIT_FAILED`（0xC0000142），不能用作负载或网络探针。
