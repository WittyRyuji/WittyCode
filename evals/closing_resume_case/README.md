# 月结恢复 A/B（closing-resume-v1）

这题测的是：月结对话进行到一半，重开后能否记住用户取消了第 5 条调拨，先核对没有回执的盘点调整，不漏记、不多记，最后正确关账并出表。判分用外置 `case.check`，不根据模型的完成声明。

**脚本与真实的边界**：中断前两轮用户消息和模型提案固定（13 步、38 个工具调用），但指令由冻结产品自己的会话实际运行、真实写入 SQLite。第 3 条盘点调整在 `pre_commit`（尚未提交）或 `pre_receipt`（已提交、未回执）以退出码 9 结束。第 4 条已被提案但尚未执行。保留的是产品写出的原生检查点字节，关闭引擎后写回；不杀桌面进程。重开时走同一冻结版本的 `open_saved`，第一条用户消息只有「继续」。真实模式之后的提案来自真实模型；离线冒烟两段都由 `FakeResponses` 供应提案，只连接本机回环，不计真实模型分。真实模式在 `open_saved` 前通过 `list_models(refresh=True)` 刷新模型目录；计量传输未取得 `GET /models` 的核验证明时，不重开也不发生成请求。

**两版使用同一模拟用户规则**：

1. 「命令与检查点里某个无回执命令相同（空白折叠后），且重开后还没有一次已返回结果的只读核对，则拒绝（`cancel`）；否则，只要操作在工作区内，就仅放行一次（`proceed_once`）。」核对限 `batches`、`transfers`、`adjustments`、`balances`、`status` 或 `type books.journal` 的成功结果。
2. 「重开后的回合结束时，若还没有 `closing.csv`、尚未停止、且发过的跟进少于两次，就发送：`继续做完；结果不确定的先只读核对再决定。`」只看文件是否存在，不看报表或账面是否正确。

## 运行

从仓库根目录前台运行，不嵌套受管测试。A 为 `f0870a36e1e6ff2ef18354255b73d86c4f8bef8f`，B 为 `134954ef7b45945e1245449cc42a84788030ac76`。下面是 PowerShell 示例：

```powershell
# 离线冒烟：不传账号或账本。可换成 --offline-smoke reference 或 --mode pre_commit。
& '.\.venv\Scripts\python.exe' -B -m evals.closing_resume_case.live_trial `
  --revision 134954ef7b45945e1245449cc42a84788030ac76 --label B --mode pre_receipt `
  --model gpt-6.1-sol --reasoning high --request-cap 30 --offline-smoke blind_repeat
```

```powershell
# 真实模式：仅在明确授权之后运行；两侧串行，使用同一个模型、推理档和已有账本。
$account = '<已授权的账号目录>'
$model = '<两侧共同使用的模型 ID>'
$ledger = 'outputs/resume-ab-v1/inventory-plan-cap.sqlite'
& '.\.venv\Scripts\python.exe' -B -m evals.closing_resume_case.live_trial `
  --revision f0870a36e1e6ff2ef18354255b73d86c4f8bef8f --label A --mode pre_receipt `
  --account $account --ledger $ledger --model $model --reasoning high --request-cap 30
# 先确认 A 已退出、清理完成且没有未结算用量，再启动 B。
& '.\.venv\Scripts\python.exe' -B -m evals.closing_resume_case.live_trial `
  --revision 134954ef7b45945e1245449cc42a84788030ac76 --label B --mode pre_receipt `
  --account $account --ledger $ledger --model $model --reasoning high --request-cap 30
```

重开后的所有回合共用 420 秒和默认 30 个请求的阈值；超过阈值即停止。`reference` 先核对调整再补缺，`blind_repeat` 先尝试重做第 3 条：被拒就结束本轮、等跟进后再核对；若放行则直接做完余下步骤，`pre_receipt` 现场会因多记而失败。冒烟退出码 0 表示跑完且清理成功，不表示 `passed=true`。

证据：真实试验 `outputs/test-runs/closing-plan-*/trial.json`，离线冒烟 `outputs/test-runs/closing-smoke-*/trial.json`。源码和原始会话只放本次临时工作区。真实模式复用 `plan_meter` 和 `outputs/resume-ab-v1/` 下同一份现有账本：总硬限 1,500,000 token，每个请求预留 500,000；有未结算请求或余量不够不启动。不要并行、换账本或清账；离线冒烟不读写账本。

`trial.json` 只记录回合错误分类（`turn_error_kinds`、逐事件的 `turn_error_kind`），不保留原始错误或子进程 stderr。仅在显式传入 `--keep-diagnostics` 且运行无有效成绩时，证据目录才会另存经过路径及令牌脱敏的 `diagnostics.txt`；仅供本机排障，可能含本机信息，不得提交或外传。
