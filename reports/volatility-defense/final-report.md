# P4 因果波动扩张防御报告

- 选中：`P4_R2_V150_N10_F20`（仅按 Development 选择）
- 信号：严格使用当前 Bar 之前 60 根已闭合 K 线
- Validation：仅用于最终验收
- Final OOS：旧区间已消费，本轮仅为 Research Validation
- 生产参数：未修改
- COST50：PASS
- 完整测试：PASS

## 基线对照

| 指标 | 基线 | P4 |
| --- | --- | --- |
| 六种子平均净收益 | 28.165621851810478 | 23.097466743931676 |
| 六种子最差净收益 | 23.995900684789852 | 19.193696375510367 |
| 最差 5% 窗口均值 | -3.323583028320009 | -2.6479713958630557 |
| 最大回撤 | 0.016382553155923477 | 0.013332728749885035 |
| 费用/毛利润 | 0.16572455311591572 | 0.20621173942637916 |
| RANGE PnL | 140.80528646810282 | 119.8819709814637 |
| VOLATILITY_EXPANSION PnL | -89.65014864410225 | -60.01894634092444 |

## 执行实效

- 因果波动扩张 Bar：47431
- 最大连续扩张：23
- 主动部分减仓：569
- 减仓成本：41.748461 USDT
- 库存下降中位数：0.19999999999999996

## 门槛

| 门槛 | 结果 |
| --- | --- |
| volatility_loss_improvement_ge_20pct | PASS |
| worst_5pct_loss_improvement_ge_20pct | PASS |
| max_drawdown_not_worse_than_5pct | PASS |
| mean_pnl_retention_ge_80pct | PASS |
| range_profit_retention_ge_75pct | PASS |
| positive_seed_count_ge_4 | PASS |
| both_symbols_no_catastrophic_deterioration | PASS |
| fee_ratio_not_materially_worse | PASS |
| best_window_concentration_le_35pct | FAIL |
| volatility_reduce_observed | PASS |
| inventory_reduction_ge_90pct_target | PASS |
| full_pytest_passed | PASS |

## 结论

本轮没有稳健候选，保持生产参数不变。

P5–P9 的后续结构验证见 [iteration-summary-p5-p9.md](iteration-summary-p5-p9.md)。
