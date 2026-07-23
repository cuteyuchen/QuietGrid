# Round 22：现货-永续 Funding Carry 乐观上界结果

Oracle 事后选择每个窗口的 carry 方向；忽略基差、借币与执行风险，仅保留实际 funding 和双腿 Maker 往返费用。

| 单元 | 标的 | 窗口 | Funding 收入 | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `PREHISTORY_EXTERNAL_BASE` | BTCUSDT | 28 | 11.1425 | 5.6000 | 5.5425 | 6.515 | 0.11% | 46.43% | 是 |  |
| `PREHISTORY_EXTERNAL_BASE` | ETHUSDT | 28 | 6.5774 | 3.3600 | 3.2174 | 11.496 | 0.06% | 46.43% | 是 |  |
| `PREHISTORY_EXTERNAL_COST50` | BTCUSDT | 28 | 11.1425 | 8.4000 | 2.7425 | 2.003 | 0.36% | 35.71% | 是 |  |
| `PREHISTORY_EXTERNAL_COST50` | ETHUSDT | 28 | 6.5774 | 5.0400 | 1.5374 | 2.160 | 0.35% | 35.71% | 否 | best_window_concentration_le_35pct |
| `CURRENT_DEVELOPMENT_BASE` | BTCUSDT | 108 | 39.6010 | 21.6000 | 18.0010 | 4.722 | 0.55% | 38.89% | 是 |  |
| `CURRENT_DEVELOPMENT_BASE` | ETHUSDT | 108 | 30.1346 | 12.9600 | 17.1746 | 7.627 | 0.58% | 44.44% | 是 |  |
| `CURRENT_DEVELOPMENT_COST50` | BTCUSDT | 108 | 39.6010 | 32.4000 | 7.2010 | 1.599 | 1.56% | 29.63% | 是 |  |
| `CURRENT_DEVELOPMENT_COST50` | ETHUSDT | 108 | 30.1346 | 19.4400 | 10.6946 | 2.606 | 1.34% | 33.33% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_BASE` | BTCUSDT | 49 | 4.9895 | 9.8000 | -4.8105 | 0.049 | 0.98% | 6.12% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_BASE` | ETHUSDT | 49 | 3.1592 | 5.8800 | -2.7208 | 0.133 | 0.92% | 10.20% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_COST50` | BTCUSDT | 49 | 4.9895 | 14.7000 | -9.7105 | 0.008 | 1.95% | 2.04% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_COST50` | ETHUSDT | 49 | 3.1592 | 8.8200 | -5.6608 | 0.033 | 1.89% | 6.12% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_BASE` | BTCUSDT | 108 | 9.5660 | 21.6000 | -12.0340 | 0.033 | 2.41% | 4.63% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_BASE` | ETHUSDT | 108 | 6.0941 | 12.9600 | -6.8659 | 0.061 | 2.29% | 5.56% | 否 | total_pnl_positive, profit_factor_gt_1, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_COST50` | BTCUSDT | 108 | 9.5660 | 32.4000 | -22.8340 | 0.002 | 4.57% | 0.93% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_COST50` | ETHUSDT | 108 | 6.0941 | 19.4400 | -13.3459 | 0.012 | 4.45% | 2.78% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |

通过单元：7/16。

结论：NO_PREREGISTERED_FUNDING_CARRY_CANDIDATE：至少一个单元在未来已知方向、零基差风险和同步 Maker 假设下仍失败，排除本协议定义的周末 funding carry 家族。

封存月份与 CURRENT Final OOS 未读取；没有注册候选；生产默认值未修改。
