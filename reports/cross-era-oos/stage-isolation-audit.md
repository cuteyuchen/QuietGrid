# 跨周期阶段隔离审计

日期：2026-07-23

Development 正式筛选只把 `split.development` 的 window_id 传入回测函数，候选收益、回撤、尾部、集中度和选择结果均只来自 Development。

审计发现初版 `scripts/cross_era_oos.py` 在加载研究状态时，为全部窗口预计算了仅用于报告的事后市场状态标签。Validation 与 Final OOS 的标签：

- 未传入 Development 回测；
- 未进入 Development 汇总；
- 未写入 Development JSON 或 Markdown；
- 未被用于候选选择；
- 未由研究者查看。

因此 Development 的 `SELECTED NONE` 结论不受影响，但该实现不满足最严格的“封存段连报告标签也不预计算”要求。代码现已修改为按阶段 window_id 计算标签：

- `screen` 只计算 Development 标签；
- `validate` 只计算 Validation 标签；
- `finalize` 只计算 Final OOS 标签。

后续阶段使用修正后的隔离实现。生产参数保持不变。
