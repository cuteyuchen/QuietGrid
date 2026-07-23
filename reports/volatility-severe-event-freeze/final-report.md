# P9 波动减仓后只减不增报告

- 结构：V1.75/N3/F20%/BOTH；减仓成功后只允许 REDUCE 单；连续 10 根正常 Bar 后恢复
- 选择：单一预注册结构；Validation 不参与选择
- Final OOS：旧区间已消费，仅为 Research Validation
- COST50：PASS
- 完整测试：PASS
- 生产参数：未修改

## 对照

| 指标 | 基线 | P4 BOTH | P9 SEVERE EVENT FREEZE |
| --- | --- | --- | --- |
| 六种子平均净收益 | 28.165621851810478 | 23.097466743931676 | 26.59670335140491 |
| 六种子最差净收益 | 23.995900684789852 | 19.193696375510367 | 21.920769260460503 |
| 最差 5% 窗口均值 | -3.323583028320009 | -2.6479713958630557 | -3.1448155455493496 |
| 最大回撤 | 0.016382553155923477 | 0.013332728749885035 | 0.017514905892246902 |
| 最坏窗口集中度 | 0.33074767611108397 | 0.5061857851225422 | 0.36057685985157134 |
| 费用/毛利润 | 0.16572455311591572 | 0.20621173942637916 | 0.18448598933896732 |
| RANGE PnL | 140.80528646810282 | 119.8819709814637 | 160.21323197748927 |
| VOLATILITY_EXPANSION PnL | -89.65014864410225 | -60.01894634092444 | -91.68246788164956 |
| 波动减仓 PnL | 0.0 | -120.50009448740963 | -72.76476972877953 |
| 波动减仓成本 | 0.0 | 41.74846116039 | 23.52907140984 |

## 门槛

| 门槛 | 结果 |
| --- | --- |
| volatility_loss_improvement_ge_20pct | FAIL |
| worst_5pct_loss_improvement_ge_20pct | FAIL |
| max_drawdown_not_worse_than_5pct | FAIL |
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
