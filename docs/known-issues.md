# 已知问题

以下测试在 2026-11-24 的 `dev` 上出现过失败，合并 Fusion 席位前后表现一致。

## 悬停 Token 用量不显示饼图

- 现象：悬停 Token 用量时，模型用量占比饼图未出现（`timed out waiting for .usage-pie x 1`）。
- 测试：`tests/test_app_e2e.py::test_token_tooltip_shows_the_models_share_of_the_whole_conversation`
- 已排除：两轮回复及换模型均已完成，也能定位用量元素 `.usage`，不是会话未跑完或悬停目标缺失。
- 状态：未定位，可能不能稳定复现。
