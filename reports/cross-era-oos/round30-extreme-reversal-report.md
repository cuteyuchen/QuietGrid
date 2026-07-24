# Round 30：短周期价格极端反转结果

使用过去 12 个小时的因果价格收益；达到 ±3% 后反向持有 48 小时，完整计入真实 funding 与主动成本。

| 单元 | 净收益 | 日 PF | Sharpe | 最大回撤 | 正收益月 | 交易数 | 价格 PnL | Funding | 成本 | 通过 | 失败检查 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `DEVELOPMENT_BASE_BTC` | 643.4218 | 1.238 | 1.430 | 27.52% | 70.59% | 184 | 791.2480 | 36.0762 | 183.9024 | 否 | maximum_drawdown_le_20pct |
| `DEVELOPMENT_COST50_BTC` | 505.4950 | 1.182 | 1.211 | 30.73% | 70.59% | 184 | 791.2480 | 36.0762 | 321.8292 | 否 | maximum_drawdown_le_20pct |
| `DEVELOPMENT_BASE_ETH` | 308.2501 | 1.126 | 1.091 | 41.80% | 70.59% | 209 | 400.6983 | 33.1917 | 125.6399 | 否 | maximum_drawdown_le_20pct |
| `DEVELOPMENT_COST50_ETH` | 214.0202 | 1.086 | 0.897 | 44.65% | 70.59% | 209 | 400.6983 | 33.1917 | 219.8698 | 否 | maximum_drawdown_le_20pct |
| `VALIDATION_BASE_BTC` | -62.1644 | 0.938 | -0.122 | 36.83% | 50.00% | 75 | 5.8485 | 7.2812 | 75.2941 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, best_profitable_month_concentration_le_35pct |
| `VALIDATION_COST50_BTC` | -118.6350 | 0.886 | -0.406 | 41.52% | 41.67% | 75 | 5.8485 | 7.2812 | 131.7647 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `VALIDATION_BASE_ETH` | -65.8805 | 0.932 | 0.179 | 59.96% | 50.00% | 105 | -8.6348 | 5.9984 | 63.2441 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5 |
| `VALIDATION_COST50_ETH` | -113.3135 | 0.886 | 0.024 | 65.40% | 50.00% | 105 | -8.6348 | 5.9984 | 110.6771 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5 |
| `POSTHISTORY_BASE_BTC` | -66.3446 | 0.935 | -0.135 | 34.45% | 41.18% | 94 | 29.3328 | -1.7357 | 93.9417 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |
| `POSTHISTORY_COST50_BTC` | -136.8009 | 0.871 | -0.469 | 42.81% | 41.18% | 94 | 29.3328 | -1.7357 | 164.3980 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |
| `POSTHISTORY_BASE_ETH` | -140.1377 | 0.908 | -0.369 | 68.17% | 47.06% | 161 | -47.6690 | 4.0954 | 96.5641 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |
| `POSTHISTORY_COST50_ETH` | -212.5608 | 0.864 | -0.701 | 84.18% | 41.18% | 161 | -47.6690 | 4.0954 | 168.9872 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |

通过单元：0/12。

结论：NO_PREREGISTERED_EXTREME_REVERSAL_CANDIDATE：至少一个严格单元失败，排除本协议定义的 12h/3%/48h 价格极端反转 family。

CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。
