# Fusion live 传输矩阵

`evals/live_fusion_matrix` 是一个需要显式开启的文本入口：对 `plan`、`devin`、`api` 三个后端在
`main`/`worker`/`light` 三个席位上的全部 27 种组合（`3^3`），用生产兼容的委派工具路径做真实路由检验——
主模型按指令调 `delegate_tasks`，产品在真实专家子会话里执行各席位后端，再回收回报。它检验的是
**调度与委派接线**，不是 V3 declare/route/自动恢复的实机验收；后者属另一套评测。

每席位的目标型号与思考档由入口的 `TARGET` 表给定（验收时点用户指定）：

| 后端 | 型号 | effort |
|------|------|--------|
| `plan` | `gpt-6.1-terra` | `low` |
| `api` | `deepseek-flash`（DeepSeek 官方端点） | `low` |
| `devin` | `swe-2-high` | `medium` |

本次真实验收清单只覆盖 `plan`/`devin`/`api` 三个后端，Factory 不在清单内：装置不创建 Factory 客户端，
任何含 `factory` 的 `--cell` 在参数解析阶段直接被拒。

## 列举组合（不读凭据）

```powershell
.venv\Scripts\python.exe -m evals.live_fusion_matrix
```

无参数时只打印 27 个唯一格名（`main,worker,light` 顺序），不读账户、provider 或环境目录，不构造引擎。

## 跑单格 / 分组（真实请求，需凭据）

在仓库根目录用托管 runner 前台执行（`$profileDir` 由产品自身的 `default_settings_path` 取到，不用手写路径）：

```powershell
$profileDir = & '.venv\Scripts\python.exe' -c "from desktop.core.settings import default_settings_path; print(default_settings_path().parent)"
& '.venv\Scripts\python.exe' '.\desktop\core\test_runs.py' -- '.venv\Scripts\python.exe' -m evals.live_fusion_matrix --profile "$profileDir" --cell plan,devin,api
```

- `--cell` 可重复，一次跑若干个不同的格（如 `--cell devin,api,plan --cell api,api,api`）；
  重复格、缺 `--profile` 或 profile 不存在都会以参数错误退出。
- 托管 runner 提供 `WITTYCODE_TEST_WORK`（本运行独立临时目录）与 `WITTYCODE_TEST_EVIDENCE`（证据输出）；
  进程结束后由 runner 管理清理，命令保持前台。
- `--profile` 是应用数据目录（即上一步取到的 `$profileDir`，通常 `%APPDATA%\WittyCode`，含 `settings.json`、
  `models.json`、`providers.json`、`chatgpt\`），只读使用：Plan 只取当前有效 access token，过期、被拒或缺失时该格
  直接失败（`error_type` 记为异常类名），不刷新、不写回账户文件；API key 经 `ProviderStore` 的受支持
  接口解析，只用于当次请求，不复制、不打印。会话存储、Fusion Drive、项目目录全部落在本运行的 work 目录。
  每格策略为 `max_calls=2`、`max_seconds=75`，回合上限 130 秒；测试回合只注册 `todo_write`，
  专家无文件系统、Shell、远程 MCP 或技能工具。

## 发送前校验（不联网也生效）

`CheckedWire` 包住每个 HTTP 通道，在放行前检查：方法 `POST`、目的 URL 与后端约定端点一致、
`model` 严格等于指定型号、effort 严格等于 `low`（api 用 `reasoning_effort`，plan/responses 用
`reasoning.effort`）、`thinking` 未禁用、请求数不超过 8。任一不满足即 `ValueError` 拒发，
错误模型/effort/目的地不会出网。Devin 席位经 `devin acp` 的 `models` 握手取目录；不创建 Factory 客户端。

## 证据

每格写一份 `evidence/<main-worker-light>.json`，字段只含类型与状态，不含模型文本、密钥、账号或会话 ID：

- `status`: `passed`（main 完成且两个专家 `finished`/`model`/`effort`/`receipt` 四项全真）、
  `attempted`/`not_verified`/`failed`；`error_type` 只记异常类名。
- `main_done`；`checks.worker`/`checks.light`；`experts.worker`/`experts.light`；
  `requests.plan`/`requests.api`：各通道实际放行的请求数。

退出码：`0` 所选格全过；`1` 有格未过；`2` 参数错误。

## 回归

离线检查在 `tests/test_live_fusion_matrix.py`：列举 27 唯一格且不触凭据、Factory 格被拒、
`CheckedWire` 对错误目的地/型号/effort/请求数全部拒发（用桩流代网络，全程离线）。

```powershell
& '.venv\Scripts\python.exe' '.\desktop\core\test_runs.py' -- '.venv\Scripts\python.exe' -m pytest tests/test_live_fusion_matrix.py -q
```
