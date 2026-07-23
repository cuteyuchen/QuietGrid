# 网格步长振荡容量乐观上界评估

每个单元允许使用事后最有利 lookback 和阈值；该结果不可部署，只用于排除特征家族。

| 单元 | 标的 | Lookback | 阈值 | 最差种子 PnL | 最低覆盖 | Oracle 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| CURRENT_DEVELOPMENT_BASE | BTCUSDT | 720 | 0.01145331 | 5.5493 | 72.22% | 否 | max_drawdown_le_5pct |
| CURRENT_DEVELOPMENT_BASE | ETHUSDT | 720 | 0.15842497 | 3.3516 | 2.78% | 否 | best_window_concentration_le_35pct, minimum_trade_coverage_ge_25pct |
| CURRENT_VALIDATION_BASE | BTCUSDT | 1440 | 0.01333520 | 8.7755 | 74.07% | 是 |  |
| CURRENT_VALIDATION_BASE | ETHUSDT | 1440 | 0.00666760 | 5.0209 | 88.89% | 是 |  |
| CURRENT_DEVELOPMENT_COST50 | BTCUSDT | 720 | 0.22363607 | -2.0522 | 0.93% | 否 | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, minimum_trade_coverage_ge_25pct |
| CURRENT_DEVELOPMENT_COST50 | ETHUSDT | 720 | 0.15482497 | 2.8062 | 2.78% | 否 | best_window_concentration_le_35pct, minimum_trade_coverage_ge_25pct |
| CURRENT_VALIDATION_COST50 | BTCUSDT | 1440 | 0.01293520 | 5.7499 | 74.07% | 是 |  |
| CURRENT_VALIDATION_COST50 | ETHUSDT | 1440 | 0.00646760 | 2.5908 | 88.89% | 是 |  |
| PREHISTORY_EXTERNAL_BASE | BTCUSDT | 180 | 0.00362730 | 2.1845 | 21.43% | 否 | best_window_concentration_le_35pct, minimum_trade_coverage_ge_25pct |
| PREHISTORY_EXTERNAL_BASE | ETHUSDT | 180 | 0.00673301 | 5.8390 | 28.57% | 否 | best_window_concentration_le_35pct |
| PREHISTORY_EXTERNAL_COST50 | BTCUSDT | 180 | 0.07741249 | -0.2688 | 3.57% | 否 | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, best_window_concentration_le_35pct, minimum_trade_coverage_ge_25pct |
| PREHISTORY_EXTERNAL_COST50 | ETHUSDT | 180 | 0.00653301 | 4.9757 | 28.57% | 否 | best_window_concentration_le_35pct |
| SPOT_EXTERNAL_BASE | BTCUSDT | 180 | 0.00673027 | 14.9837 | 33.66% | 是 |  |
| SPOT_EXTERNAL_BASE | ETHUSDT | 180 | 0.04400694 | -1.0550 | 2.97% | 否 | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, best_window_concentration_le_35pct, minimum_trade_coverage_ge_25pct |
| SPOT_EXTERNAL_COST50 | BTCUSDT | 180 | 0.00653027 | 7.8999 | 33.66% | 是 |  |
| SPOT_EXTERNAL_COST50 | ETHUSDT | 180 | 0.04300694 | -1.3185 | 2.97% | 否 | all_seeds_positive, worst_seed_positive, all_seed_profit_factors_gt_1, best_window_concentration_le_35pct, minimum_trade_coverage_ge_25pct |

通过单元：6/16。

结论：NO_PREREGISTERED_CYCLE_CAPACITY_CANDIDATE：至少一个单元在全部 lookback 和 oracle 阈值下仍无法通过，排除本特征家族。

CURRENT Final OOS 未读取；生产默认值未修改。
