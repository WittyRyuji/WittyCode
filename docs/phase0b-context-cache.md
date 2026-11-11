# WittyCode Phase 0b 验证：上下文隔离、提示缓存、长会话与 Python 版本锁定

> 日期：2026-10-28。方法与 Phase 0 相同（`spikes/` 实测 + `fixtures/` 录制）。
> 运行环境：Windows 11，Python 3.11.9（`.venv`），droid.exe 0.251.0（Factory 桌面版 app-0.208.0），droid-sdk 0.5.0，默认模型 `claude-haiku-5-5`（reasoning low，除非另注）。
> 金丝雀（canary）为随机「词-数字」（如 `falcon-7391`），每条事实唯一；泄漏与否可在文本层面无歧义判定。验证性提问从不复述金丝雀的值，因此某会话历史/回答中出现其它会话的金丝雀即构成泄漏证据。
> 全部金丝雀值、会话 id、逐事件流水见 `fixtures/context/state.json`、`fixtures/cache/*.jsonl`、`fixtures/long_session/*`。

## 结论总览

| 范围 | 结果 |
|---|---|
| A 上下文隔离与一致性（A1–A8 + 无模型历史扫描） | 全部 PASS |
| B 提示缓存测量（8 个场景） | 全部测得，行为符合「系统前缀共享 + 会话尾部 5 分钟 TTL」模型 |
| C 长会话脚本 | 短验证（1,2 分钟检查点）PASS；完整 95 分钟运行命令见 C 节 |
| D Python 3.11 锁定 | 已落地 3 个文件；既有文件未改动 |

实测使用真实模型轮 28 轮（另含 1 次压缩摘要调用）。

---

## A. 上下文隔离与会话内完整性

测试脚本：`spikes/ctx_isolation.py`（A1–A5）、`spikes/ctx_actions.py`（A6）、`spikes/ctx_compact_rewind.py`（A7+A8）、`spikes/ctx_history_check.py`（无模型历史扫描）。
录制：`fixtures/context/isolation_a|b.jsonl`、`actions.jsonl`、`compact_rewind.jsonl`（+ 对应 `.raw.jsonl` 原始通知）、汇总 `state.json`、`history_check.json`。

| 用例 | 验证内容 | 结果 | 关键证据 |
|---|---|---|---|
| A1 进程模型 | 每个 `Session` 是否独占一个 `droid.exe` | PASS | A pid=44780、B pid=52864；共享 `ProcessTransport` 打开第二个 Session → `TimeoutError`（共享不受支持），关闭报 `Transport not connected` |
| A2 并发双会话 | `asyncio.gather` 并发 plant+recall，互不串话 | PASS | A 只知 X、B 只知 Y（`state.json.replies`：`A_recall=nectar-8044\nnone`、`B_recall=harbor-4665\nnone`） |
| A3 同进程顺序会话 | 同进程再开同 cwd 新会话 C | PASS | C 召回 `none\nnone`，无任何金丝雀 |
| A4 他会话存活时 resume | B 存活时新客户端 `Session.resume(A)` | PASS | 召回只有 X（无 Y/Z）；`B_open_during_resume=true` |
| A5 fork 隔离 | `fork()` 后在分叉里种 Z | PASS | 分叉知 X+Z；resume 的原 A 只知 X 不知 Z |
| A6a 中断一致性 | Create 完成后、Execute（`Start-Sleep 30`）期间 `interrupt()` | PASS | `RunInterrupted`（6.76s）；`interrupt_probe.txt` 已落盘且内容与 payload 一致；后续回答与磁盘一致（`interrupt_probe=yes`） |
| A6b 权限取消一致性 | Create 权限回 `CANCEL` | PASS | `cancel_probe.txt` 不存在；模型承认未创建（`cancel_probe=no`），回答与磁盘一致 |
| A7 压缩 | 3 金丝雀 → `compact()` → 继任会话召回 | PASS | `removed_count=3`；继任者召回 3/3；见下方「API 形状发现」注意点 |
| A8 回退 | 第 2 轮种金丝雀 → `rewind()` 回第 1 轮 | PASS | 继任会话召回为 none；`rewind_info()` 正常返回；源会话历史仍含金丝雀（见历史扫描） |
| 历史扫描 | 低层 `load_session` 对 8 个会话角色做内存扫描（不写盘） | PASS | A 只含 X；B 只含 Y；A_fork 含 X,Z；C 无；actions 含两个 payload；compact_succ 含 c1–c3 且无他；rewind_src 含 rw；rewind_succ 无任何金丝雀。存储层零交叉污染 |

### API 形状发现（WittyCode 集成时必须知道）

- **fork() 直接返回 `Session`**；`compact()` / `rewind()` 返回 outcome，继任会话在 `outcome.session`（另有 `removed_count`、`restored_count` 等计数）。
- 三种替换操作都使源会话**退役**（继续用抛 `SessionReplacedError`，`close()` 变 no-op），继任者接管同一客户端/进程；要同时操作分叉两侧需 `Session.resume(旧id)` 开新连接。
- **compact 复用原 session id**（本次 `compact_src` 与 `compact_succ` id 相同）；**rewind / fork 生成新 id**。
- 协议层无 session 路由（`add_user_message`/`interrupt`/`fork`/`compact` 均不带 session id）→ **每条连接同一时刻只有一个活跃会话**；WittyCode 的多标签应为「每标签一个 Session/进程」，不要试图共享进程。
- `update_settings(model=..., reasoning_effort=...)` 对 resume 得到的会话同样生效（B 节 turn 6/7 实测）。
- 注意点：`session.context()` 在本次 compact 前后返回完全相同的 `used=11598`（疑似未即时刷新）；**token 用量流（`agent_turn_completed` / `RunResult.usage`）是更可靠的上下文大小信号**。
- 中断时序：在 `ToolProgress` 事件处触发 `interrupt()` 可稳定打断执行中的工具；**已完成步骤的文件副作用保留，且模型后续自述与磁盘一致**——会话内完整性在中断/权限取消后成立。

---

## B. 提示缓存（prompt cache）

测试脚本：`spikes/cache_curve.py`（轮 1–4）、`spikes/cache_resume.py`（轮 5–8）。
录制：`fixtures/cache/curve*.jsonl`、`resume*.jsonl`、`curve_summary.json`、`resume_summary.json`、`resume_fork_summary.json`。

### 可用的用量字段

- 高层 `RunResult.usage` / `TokenUsageUpdate`：`inputTokens`、`outputTokens`、`cacheCreationTokens`、`cacheReadTokens`、`thinkingTokens`、`factoryCredits`（`TokenUsageUpdate` 为累计值，`RunResult.usage` 为单轮值）。
- 原始通知（`session.on_notification`）：`session_token_usage_changed`（`tokenUsage` 累计 + `lastCallTokenUsage{inputTokens,outputTokens,cacheReadTokens}`，逐次 LLM 调用）与 `agent_turn_completed`（`tokenUsage` 单轮含缓存字段、`cumulativeTokenUsage`、`durationMs`）。高层流丢弃了 `lastCallTokenUsage`，需要逐调用粒度时订阅原始通知。

### 实测数字（`RunResult.usage` 单轮值）

| 场景 | 轮 | cache_read | cache_create | in | out | credits | 解读 |
|---|---|---|---|---|---|---|---|
| 冷启动（新会话第 1 轮） | 1 | 14 492 | 3 090 | 347 | 9 | 362 | 系统前缀（≈14.5k，其它会话已建）命中；会话部分全建 |
| 背靠背 | 2 | 17 582 | 16 | 4 | 4 | 73 | 全命中，成本降到 ~20% |
| 背靠背 | 3 | 17 598 | 16 | 4 | 4 | 73 | 同上 |
| 闲置 360s 后 | 4 | 14 492 | 3 138 | 4 | 4 | 216 | 会话尾部缓存过期（约 5 分钟 TTL）→ 重建；系统前缀仍命中 |
| 新进程 resume | 5 | 14 492 | 3 154 | 4 | 4 | 217 | 形状与闲置过期相同（部分失效） |
| 换模型 → `gpt-6-luna` | 6 | 0 | 10 705 | 3 | 6 | 537 | **完全 miss**（跨提供方/模型是不同缓存命名空间） |
| 换回 haiku + reasoning MEDIUM | 7 | 14 492 | 4 205 | 4 | 4 | 270 | haiku 系统前缀仍在 TTL 内命中；会话尾部重建（含 luna 轮故更大）。reasoning 变化的影响与换模型混淆，本次未单独隔离 |
| fork 后第一轮 | 8 | 18 697 | 16 | 4 | 4 | 77 | **fork 完整继承缓存**，不失效 |
| 压缩后第一轮（A7 继任者） | – | 14 492 | 3 460 | 4 | 27 | 237 | 压缩重写了缓存前缀；压缩摘要调用本身 in=750/out=309/cacheRead=0 |

### 失效因子小结

- **约 5 分钟 TTL**：闲置 6 分钟即重建会话尾部（3138 tokens、216 credits）。
- **进程重启 / resume**：与 TTL 过期同形状（系统前缀幸存，尾部重建）。
- **换模型（跨提供方）**：完全失效，最贵（537 credits，≈1.5× 冷启动）。
- **compact**：重建前缀（继任者第一轮 237 credits）。
- **fork**：不失效（77 credits，接近最便宜）。
- 时间戳证据：turn 4 的 `session_token_usage_changed` 出现在 t=378.6s（轮 3 在 t=10.4s），`cacheCreationTokens` 从 3122 → 6260。

### 对 WittyCode 的成本含义

- 命中轮（73–77 credits）≈ 冷轮（362）的 **1/5**；保持单长会话、避免无谓新开会话是最省策略。
- 闲置超过约 5 分钟的下一轮约 3× 命中轮成本（216）——UI 上「继续旧会话」与「新会话」在 >5min 闲置后成本接近，但 resume 仍省系统前缀。
- 频繁换模型代价最高；模型切换按钮应有成本提示。
- fork 免费继承缓存：实现「从当前会话分叉探索」功能无缓存惩罚。

---

## C. 长会话脚本

脚本：`spikes/ctx_long_session.py`。行为：种金丝雀 → 每个检查点（分钟）记录「自身 droid.exe 存活（PID 匹配）/ 工作集内存 / 金丝雀召回轮 / 延迟 / 用量」，逐行 flush 写 `fixtures/long_session/progress.jsonl`；最后一个检查点后 **taskkill 只杀自己的 droid PID**（杀前校验 PID 属于本会话且 ≠ 自身 Python 进程），随后 `Session.resume` 同 id 再召回验证恢复。全部通过才 exit 0；结果写 `summary.json`。

短验证（`--checkpoints 1,2`）已 PASS：

- 检查点 1m：alive、内存 289 792 000 B、召回成功、延迟 3.22s
- 检查点 2m：alive、内存 289 890 304 B（稳定）、召回成功、延迟 1.70s
- kill：pid 47636，taskkill exit 0，`alive_after_kill=False`
- resume 恢复：新 pid 52412，召回成功 → PASS（全程 2.2 分钟，exit 0）

完整验收运行命令（默认检查点 5,15,30,60,90，约 95+ 分钟）：

```
Set-Location <项目根>; .\.venv\Scripts\python.exe spikes\ctx_long_session.py
```

（由主代理在后台启动；进度可实时查看 `fixtures/long_session/progress.jsonl`。）

---

## D. Python 3.11 锁定

新增 3 个文件（**既有文件均未改动**）：

- `pyproject.toml`：PEP 621 元数据 + `requires-python = ">=3.11,<3.12"`；依赖钉版 `droid-sdk==0.5.0`、`PySide6==6.12.0`、`qasync==0.28.0`。
- `.python-version`：`3.11`（供 pyenv/uv 等工具识别）。
- `spikes/_pyguard.py`：运行时守卫，`sys.version_info[:2] != (3, 11)` 即退出并打印重建提示；Phase 0b 每个 spike 首行导入，已在 venv 实测 exit 0。

venv 重建方法：`py -3.11 -m venv .venv`（随后 `.\.venv\Scripts\python.exe -m pip install droid-sdk==0.5.0 PySide6==6.12.0 qasync==0.28.0`）。

后续建议（不在本次范围）：应用主入口（未来的 `main.py`）同样内联 3.11 检查或复用 `_pyguard`。

---

## 安全与卫生

- 全程未打印任何令牌/密钥；隐藏上下文消息（`visibility=llm_only`）只做了内存扫描，未落盘。
- 新 fixtures（`fixtures/context`、`fixtures/cache`、`fixtures/long_session`）已扫描 `fk-` / `sk-` / `Bearer` / JWT(`eyJ…`) / `PRIVATE KEY` / `password` / `api[_-]?key`：除自家记录里的布尔字段 `factory_api_key_env_set: false` 外零命中。
- 只杀过自己启动的 droid.exe（pid 47636）；Factory 桌面版自身的 droid 进程（47456、43876、41884、45716 等）从未触碰。