# P6 仅亏损状态波动减仓报告

- 结构：冻结 P4 的 V1.50/N10/F20/BOTH，仅在触发开盘时净 PnL 为负才减仓
- 选择：单一预注册结构；Validation 不参与选择
- Final OOS：旧区间已消费，仅为 Research Validation
- COST50：FAIL
- 完整测试：FAIL
- 生产参数：未修改

## 对照

| 指标 | 基线 | P4 BOTH | P6 LOSS_GUARD |
| --- | --- | --- | --- |
| 六种子平均净收益 | 28.165621851810478 | 23.097466743931676 | 17.85079342972453 |
| 六种子最差净收益 | 23.995900684789852 | 19.193696375510367 | 17.85079342972453 |
| 最差 5% 窗口均值 | -3.323583028320009 | -2.6479713958630557 | -3.0099888128865278 |
| 最大回撤 | 0.016382553155923477 | 0.013332728749885035 | 0.016491198750633606 |
| 最坏窗口集中度 | 0.33074767611108397 | 0.5061857851225422 | 0.47379689574752065 |
| 费用/毛利润 | 0.16572455311591572 | 0.20621173942637916 | 0.2014158747212894 |
| RANGE PnL | 140.80528646810282 | 119.8819709814637 | 16.999371955108117 |
| VOLATILITY_EXPANSION PnL | -89.65014864410225 | -60.01894634092444 | -13.566803635460218 |
| 波动减仓 PnL | 0.0 | -120.50009448740963 | -23.94493554771566 |
| 波动减仓成本 | 0.0 | 41.74846116039 | 4.60580356226 |

## 门槛

| 门槛 | 结果 |
| --- | --- |
| volatility_loss_improvement_ge_20pct | PASS |
| worst_5pct_loss_improvement_ge_20pct | FAIL |
| max_drawdown_not_worse_than_5pct | PASS |
| mean_pnl_retention_ge_80pct | FAIL |
| range_profit_retention_ge_75pct | FAIL |
| positive_seed_count_ge_4 | FAIL |
| both_symbols_no_catastrophic_deterioration | FAIL |
| fee_ratio_not_materially_worse | PASS |
| best_window_concentration_le_35pct | FAIL |
| volatility_reduce_observed | PASS |
| inventory_reduction_ge_90pct_target | PASS |
| full_pytest_passed | FAIL |

## 结论

本轮没有稳健候选，保持生产参数不变。
