# inventory-resume-v1：库存过账中断恢复案例

一个可直接交给真实模型的恢复题：`inbox/` 多批库存 CSV → `tools/ledger.py` 过账进 `books.db`（真 SQLite 事务）→ 按 SKU 出 `reconcile.csv` 对账单。上次过账在**最后一批**中途被杀；两种现场共享同一份题面、同一条恢复消息，唯一差别是 `books.db` 里最后一批**有没有 COMMIT**。

## 交给模型的只有两样东西

1. 工作区 `workspace/`（含 `TASK.md` 题面，两种现场逐字节相同）；
2. 恢复消息：`继续`。

模型自己决定查账、补记或只出报表——题目不规定工具顺序。判分由外置 checker 独立完成，ground truth 是包内**固定**的四批输入，不随现场文件裁剪：inbox 是否原样未缺漏、每批是否恰好入账一次且流水与文件一致、无孤儿流水/声明行数与实记一致、`reconcile.csv` 是否按 sku 严格行序等于全部批次净额（多列、重复 SKU、坏行都判错）。报表只看真假不论做法：账本真实正确 + 手写但正确的报表照样通过；账本缺批却只交报表一定不通过。

## 两种现场（模式名只在操作侧，不进题面、日志、目录名）

```powershell
# 现场一：最后一批在 SQLite COMMIT 前被杀 → 批没入账，正确动作是补记这一批再对账
python -m evals.inventory_resume_case prepare --dir <新目录A> --mode pre_commit

# 现场二：COMMIT 完成、回执未返回前被杀 → 批已入账，正确动作是查证后只出对账单（再记一遍就是重记）
python -m evals.inventory_resume_case prepare --dir <新目录B> --mode pre_receipt
```

`--dir` 必须是**不存在的新目录**（已存在直接拒绝，绝不覆盖别人现场）；`<dir>/meta.json` 记录模式与恢复输入，在工作区之外——它只是约定边界，**不是安全沙箱**，好奇的模型仍可能读到，给模型开工作区时请以 `workspace/` 为根。中断由工具自带的 `LEDGER_DIE_AT` 钩子在专用子进程里 `os._exit` 实现，只杀搭场景时自己启动的那一个进程。

判分：`python -m evals.inventory_resume_case check --dir <case>`（退出码 0 通过）。

## 机制自检（不是模型成绩）

```powershell
python -m evals.inventory_resume_case selfcheck --work <scratch> --evidence <dir>
# 受管 runner 内可省参数：默认读 WITTYCODE_TEST_WORK / WITTYCODE_TEST_EVIDENCE
```

对两种现场各跑一次「未恢复→判失败、参考恢复（查 `batches`→只补缺的批→出报表）→判通过」，证据写 `inventory-resume-selfcheck.json`，`tokens` 恒为 `null`、标注 `model_score: false`。

## 交给真实模型怎么跑、怎么记

- 对参评产品/引擎：把某现场 `workspace/` 作为其项目目录打开同一逻辑会话，先给 `TASK.md` 内容作为任务，待其开始或被打断后发「继续」。**每一侧都要把两种断点各跑一遍**（`pre_commit`、`pre_receipt` 各一份现场，题面相同），随机的是**运行次序**，不是把不同模式分给不同版本——否则两侧题不对称，差异不再可比。
- **盲场景（可操作的最小用法）**：`prepare --dir pair\run-1 --mode pre_commit`、`prepare --dir pair\run-2 --mode pre_receipt`，目录名、题面、日志都不含模式，`meta.json` 留在场外核对；发给谁先碰哪份现场再随机。
- **恢复输入两条话**：首次拿到工作区的人或代理用同一条消息开题，例如「读 `TASK.md`，接着把它做完」；只有产品真实保存、原生重开的中断会话，恢复消息才只是「继续」。
- **本案例只交付中断现场与判分，不含会话驱动**：在新对话里把 `workspace/` 当项目开题只是一次性工作区任务，**不等于实际产品 A/B**；若要比较 Harness 恢复能力，两侧必须用同一业务模式（同为 pre_commit 或同为 pre_receipt）、同一中断语义，并且恢复到**各版本真实保存下来的中断会话现场**——只新开窗口、先后贴两次提示不能称为 Resume A/B。
- 计量：墙钟从发出「继续」算起，到 checker 首次判通过（或放弃）为止；token 只记提供方上报值，取不到记 `null`，不合成；失败尝试与人工确认都计入结果。

## 文件

- `ledger.py` — 业务工具本体，也逐字节拷进每个现场 `tools/`：post / batches / balances / reconcile，流水日志 `books.journal`，回执 `inbox/<批>.receipt`。
- `case.py` — 固定输入、prepare、外置 checker、参考恢复与三个对照动作（盲重跑 / 盲跳过 / 手造报表）、selfcheck。
