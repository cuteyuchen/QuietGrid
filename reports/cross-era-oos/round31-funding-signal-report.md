# Round 31：因果 Funding Rate 方向信号结果

Funding event 发生后等待 1 小时；正费率反向做空、负费率反向做多，固定持有 72 小时，完整计入真实 funding 与主动成本。

| 单元 | 净收益 | 日 PF | Sharpe | 最大回撤 | 正收益月 | 交易数 | 价格 PnL | Funding | 成本 | 通过 | 失败检查 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `DEVELOPMENT_BASE_BTC` | 495.1367 | 1.168 | 1.057 | 57.32% | 70.59% | 143 | 520.5109 | 117.3413 | 142.7155 | 否 | maximum_drawdown_le_20pct |
| `DEVELOPMENT_COST50_BTC` | 388.1001 | 1.129 | 0.925 | 59.82% | 70.59% | 143 | 520.5109 | 117.3413 | 249.7522 | 否 | maximum_drawdown_le_20pct |
| `DEVELOPMENT_BASE_ETH` | 207.7445 | 1.083 | 0.922 | 81.12% | 52.94% | 145 | 216.2016 | 78.6174 | 87.0745 | 否 | maximum_drawdown_le_20pct |
| `DEVELOPMENT_COST50_ETH` | 142.4386 | 1.056 | 0.913 | 84.67% | 52.94% | 145 | 216.2016 | 78.6174 | 152.3804 | 否 | maximum_drawdown_le_20pct |
| `VALIDATION_BASE_BTC` | -128.1292 | 0.891 | -0.317 | 38.53% | 50.00% | 69 | -81.4659 | 22.4168 | 69.0800 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5 |
| `VALIDATION_COST50_BTC` | -179.9392 | 0.850 | -0.537 | 43.98% | 50.00% | 69 | -81.4659 | 22.4168 | 120.8901 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5 |
| `VALIDATION_BASE_ETH` | -92.6515 | 0.904 | -0.342 | 56.85% | 33.33% | 74 | -64.0453 | 15.9803 | 44.5865 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `VALIDATION_COST50_ETH` | -126.0914 | 0.872 | -0.557 | 63.04% | 33.33% | 74 | -64.0453 | 15.9803 | 78.0263 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `POSTHISTORY_BASE_BTC` | 150.6461 | 1.293 | 1.041 | 19.58% | 52.94% | 48 | 185.5988 | 12.8928 | 47.8455 | 是 |  |
| `POSTHISTORY_COST50_BTC` | 114.7620 | 1.214 | 0.815 | 22.80% | 47.06% | 48 | 185.5988 | 12.8928 | 83.7296 | 否 | maximum_drawdown_le_20pct, positive_calendar_month_ratio_ge_50pct |
| `POSTHISTORY_BASE_ETH` | 45.3258 | 1.059 | 0.488 | 67.60% | 47.06% | 60 | 73.8018 | 7.6099 | 36.0859 | 否 | maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |
| `POSTHISTORY_COST50_ETH` | 18.2614 | 1.023 | 0.416 | 71.65% | 47.06% | 60 | 73.8018 | 7.6099 | 63.1503 | 否 | maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |

通过单元：1/12。

结论：NO_PREREGISTERED_FUNDING_SIGNAL_CANDIDATE：至少一个严格单元失败，排除本协议定义的因果 funding-rate 方向信号 family。

CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。
