# Round 17：对称配对纪律 Phase A 结果

BTC/ETH 均限制每侧最多 1 个未配对 lot，并以 0.75 个完整网格步长作为减仓目标；CURRENT Final OOS 未评估。

| 候选 | 通过单元 | 最差种子 PnL | 最低覆盖 | 全通过 |
| --- | ---: | ---: | ---: | --- |
| `SYMMETRIC_PAIR_CAP1_TARGET075` | 4/16 | -45.7947 | 64.29% | 否 |

## 未通过单元

| 单元 | 标的 | 最差种子 PnL | 最低覆盖 | 失败检查 |
| --- | --- | ---: | ---: | --- |
| CURRENT_DEVELOPMENT_BASE | BTCUSDT | 7.8288 | 82.41% | max_drawdown_le_5pct |
| CURRENT_DEVELOPMENT_BASE | ETHUSDT | -26.5642 | 84.26% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, max_drawdown_le_5pct |
| CURRENT_DEVELOPMENT_COST50 | BTCUSDT | -7.0317 | 81.48% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, max_drawdown_le_5pct |
| CURRENT_DEVELOPMENT_COST50 | ETHUSDT | -39.0608 | 83.33% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, max_drawdown_le_5pct |
| PREHISTORY_EXTERNAL_BASE | BTCUSDT | -10.4368 | 67.86% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1 |
| PREHISTORY_EXTERNAL_BASE | ETHUSDT | -1.6649 | 82.14% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1 |
| PREHISTORY_EXTERNAL_COST50 | BTCUSDT | -16.0745 | 64.29% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, best_window_concentration_le_35pct |
| PREHISTORY_EXTERNAL_COST50 | ETHUSDT | -3.0382 | 82.14% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1 |
| SPOT_EXTERNAL_BASE | BTCUSDT | 17.8255 | 85.15% | max_drawdown_le_5pct |
| SPOT_EXTERNAL_BASE | ETHUSDT | -39.6724 | 78.22% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, max_drawdown_le_5pct |
| SPOT_EXTERNAL_COST50 | BTCUSDT | 5.8253 | 85.15% | max_drawdown_le_5pct |
| SPOT_EXTERNAL_COST50 | ETHUSDT | -45.7947 | 77.23% | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, max_drawdown_le_5pct |

选中候选：无。

结论：NO_ROBUST_SYMMETRIC_PAIRING_CANDIDATE：唯一注册候选未通过全部 16 个 Phase A 单元。

生产默认值未修改；没有独立授权文件时，CURRENT Final OOS 继续封存。
