# 已知问题

以下三项测试在 2026-11-24 的 `dev` 上失败，合并 Fusion 席位前后表现一致，并非这次合并引入。

## Devin 主模型切回 Fusion 后未再次派工

- 现象：Devin 作为主模型，跨引擎切换回 Fusion 后，第二次派工未发生（`registry.calls` 为 1，预期为 2）。
- 测试：`tests/test_fusion_switch.py::test_cross_engine_pause_restores_expert_owner_budget_and_usage_without_double_counting[devin:swe-2-high]`
- 已排除：切回操作已成功，日志中没有归档错误，不是已知的归档报错阻断切换。
- 状态：未定位，可能不能稳定复现。

## Devin 会话关闭后侧栏等不到标题

- 现象：关闭会话后，侧栏未等到标题 `hello devin`（`timed out waiting for 'hello devin' in .srow`）。
- 测试：`tests/test_app_e2e.py::test_a_devin_conversation_goes_from_the_model_menu_to_the_answer_and_an_approval`
- 已排除：关闭前已收到 Devin 的回复和审批结果，不是会话启动、回复或审批失败。
- 状态：未定位，可能不能稳定复现。

## 悬停 Token 用量不显示饼图

- 现象：悬停 Token 用量时，模型用量占比饼图未出现（`timed out waiting for .usage-pie x 1`）。
- 测试：`tests/test_app_e2e.py::test_token_tooltip_shows_the_models_share_of_the_whole_conversation`
- 已排除：两轮回复及换模型均已完成，也能定位用量元素 `.usage`，不是会话未跑完或悬停目标缺失。
- 状态：未定位，可能不能稳定复现。
