# Round 23：Premium Index 基差收敛乐观上界结果

方向仅由观察期结束时 premium 符号决定；Oracle 只事后选择退出分钟。结果忽略真实成交基差、借币与执行风险，不可部署。

| 单元 | 标的 | 窗口 | 基差收益 | Funding | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `PREHISTORY_EXTERNAL_BASE` | BTCUSDT | 27 | 15.2270 | 7.9222 | 5.4000 | 17.7492 | 56.354 | 0.24% | 81.48% | 是 |  |
| `PREHISTORY_EXTERNAL_BASE` | ETHUSDT | 27 | 9.0128 | 4.8099 | 3.2400 | 10.5827 | 382.090 | 0.22% | 96.30% | 是 |  |
| `PREHISTORY_EXTERNAL_COST50` | BTCUSDT | 27 | 15.2270 | 7.9222 | 8.1000 | 15.0492 | 16.598 | 0.25% | 74.07% | 是 |  |
| `PREHISTORY_EXTERNAL_COST50` | ETHUSDT | 27 | 9.0128 | 4.8099 | 4.8600 | 8.9627 | 65.886 | 0.23% | 88.89% | 是 |  |
| `CURRENT_DEVELOPMENT_BASE` | BTCUSDT | 108 | 60.5180 | 26.5112 | 21.6000 | 65.4292 | 74.605 | 0.74% | 87.96% | 是 |  |
| `CURRENT_DEVELOPMENT_BASE` | ETHUSDT | 108 | 42.5271 | 22.6405 | 12.9600 | 52.2076 | 303.958 | 2.80% | 95.37% | 是 |  |
| `CURRENT_DEVELOPMENT_COST50` | BTCUSDT | 108 | 60.5180 | 26.5112 | 32.4000 | 54.6292 | 20.350 | 0.78% | 73.15% | 是 |  |
| `CURRENT_DEVELOPMENT_COST50` | ETHUSDT | 108 | 42.5271 | 22.6405 | 19.4400 | 45.7276 | 55.311 | 2.85% | 83.33% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_BASE` | BTCUSDT | 49 | 25.4144 | -1.9456 | 9.8000 | 13.6688 | 19.251 | 0.42% | 77.55% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_BASE` | ETHUSDT | 49 | 21.1234 | -0.1346 | 5.8800 | 15.1089 | 34.897 | 0.27% | 79.59% | 否 | best_window_concentration_le_35pct |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_COST50` | BTCUSDT | 49 | 25.4144 | -1.9456 | 14.7000 | 8.7688 | 4.602 | 0.46% | 53.06% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_COST50` | ETHUSDT | 49 | 21.1234 | -0.1346 | 8.8200 | 12.1689 | 10.610 | 0.28% | 65.31% | 否 | best_window_concentration_le_35pct |
| `POSTHISTORY_EXTERNAL_BASE` | BTCUSDT | 107 | 23.1868 | -1.4278 | 21.4000 | 0.3590 | 1.054 | 0.87% | 37.38% | 是 |  |
| `POSTHISTORY_EXTERNAL_BASE` | ETHUSDT | 107 | 24.4488 | -0.5381 | 12.8400 | 11.0708 | 6.959 | 0.53% | 56.07% | 是 |  |
| `POSTHISTORY_EXTERNAL_COST50` | BTCUSDT | 107 | 23.1868 | -1.4278 | 32.1000 | -10.3410 | 0.295 | 2.33% | 18.69% | 否 | total_pnl_positive, profit_factor_gt_1, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_COST50` | ETHUSDT | 107 | 24.4488 | -0.5381 | 19.2600 | 4.6508 | 1.888 | 0.77% | 41.12% | 是 |  |

通过单元：13/16。

结论：NO_PREREGISTERED_BASIS_CONVERGENCE_CANDIDATE：至少一个单元在 causal premium 方向、Oracle 退出和理想同步 Maker 假设下仍失败，排除本协议定义的基差收敛 family。

两个官方数据缺口窗口已在任何 PnL 计算前对 BTC/ETH 同时固定排除；CURRENT Final OOS 未读取；生产默认值未修改。
