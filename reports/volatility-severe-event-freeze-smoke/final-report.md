# P9 波动减仓后只减不增报告

- 结构：V1.75/N3/F20%/BOTH；减仓成功后只允许 REDUCE 单；连续 10 根正常 Bar 后恢复
- 选择：单一预注册结构；Validation 不参与选择
- Final OOS：旧区间已消费，仅为 Research Validation
- COST50：FAIL
- 完整测试：FAIL
- 生产参数：未修改

## 对照

| 指标 | 基线 | P4 BOTH | P9 SEVERE EVENT FREEZE |
| --- | --- | --- | --- |
| 六种子平均净收益 | 28.165621851810478 | 23.097466743931676 | 21.920769260460503 |
| 六种子最差净收益 | 23.995900684789852 | 19.193696375510367 | 21.920769260460503 |
| 最差 5% 窗口均值 | -3.323583028320009 | -2.6479713958630557 | -2.9671520328698553 |
| 最大回撤 | 0.016382553155923477 | 0.013332728749885035 | 0.01642888524207206 |
| 最坏窗口集中度 | 0.33074767611108397 | 0.5061857851225422 | 0.34060349367071974 |
| 费用/毛利润 | 0.16572455311591572 | 0.20621173942637916 | 0.1925884343258892 |
| RANGE PnL | 140.80528646810282 | 119.8819709814637 | 23.912511621927816 |
| VOLATILITY_EXPANSION PnL | -89.65014864410225 | -60.01894634092444 | -15.200178026903101 |
| 波动减仓 PnL | 0.0 | -120.50009448740963 | -11.965977949431913 |
| 波动减仓成本 | 0.0 | 41.74846116039 | 3.893085029725 |

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
| best_window_concentration_le_35pct | PASS |
| volatility_reduce_observed | PASS |
| inventory_reduction_ge_90pct_target | PASS |
| full_pytest_passed | FAIL |

## 结论

本轮没有稳健候选，保持生产参数不变。
