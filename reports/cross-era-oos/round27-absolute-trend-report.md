# Round 27：BTC/ETH SMA50/200 绝对趋势结果

每日 01:00 UTC 仅用此前 200 个完整 UTC 日 close 生成信号；完整计入真实 funding、主动费率与滑点。

| 单元 | 资产 | 净收益 | 收益率 | 日 PF | Sharpe | 最大回撤 | 正收益月 | 最佳月集中度 | Funding | 成本 | 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `DEVELOPMENT_BASE` | BTC | -134.4554 | -26.89% | 0.966 | 0.835 | 95.20% | 41.18% | 24.85% | -128.4182 | 3.8140 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, positive_calendar_month_ratio_ge_50pct |
| `DEVELOPMENT_BASE` | ETH | 148.0360 | 49.35% | 1.044 | 0.802 | 77.03% | 52.94% | 18.77% | -98.8759 | 2.3764 | 否 | maximum_drawdown_le_20pct |
| `DEVELOPMENT_COST50` | BTC | -137.3160 | -27.46% | 0.966 | 0.851 | 95.55% | 41.18% | 24.90% | -128.4182 | 6.6745 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, positive_calendar_month_ratio_ge_50pct |
| `DEVELOPMENT_COST50` | ETH | 146.2537 | 48.75% | 1.043 | 0.800 | 77.27% | 52.94% | 18.80% | -98.8759 | 4.1588 | 否 | maximum_drawdown_le_20pct |
| `VALIDATION_BASE` | BTC | 76.7095 | 15.34% | 1.044 | 0.530 | 42.68% | 58.33% | 28.74% | 0.5359 | 2.2278 | 否 | maximum_drawdown_le_20pct |
| `VALIDATION_BASE` | ETH | -56.5834 | -18.86% | 0.968 | 0.627 | 84.26% | 66.67% | 27.89% | -5.8005 | 1.3987 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct |
| `VALIDATION_COST50` | BTC | 75.0387 | 15.01% | 1.043 | 0.525 | 42.83% | 58.33% | 28.78% | 0.5359 | 3.8986 | 否 | maximum_drawdown_le_20pct |
| `VALIDATION_COST50` | ETH | -57.6325 | -19.21% | 0.967 | 0.626 | 84.32% | 66.67% | 27.98% | -5.8005 | 2.4478 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct |
| `POSTHISTORY_BASE` | BTC | -189.1731 | -37.83% | 0.903 | 0.074 | 79.62% | 47.06% | 27.40% | -8.4557 | 3.8510 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |
| `POSTHISTORY_BASE` | ETH | 105.3313 | 35.11% | 1.050 | 0.672 | 60.69% | 52.94% | 34.36% | -4.8686 | 2.2946 | 否 | maximum_drawdown_le_20pct |
| `POSTHISTORY_COST50` | BTC | -192.0613 | -38.41% | 0.901 | 0.076 | 80.12% | 47.06% | 27.35% | -8.4557 | 6.7393 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, maximum_drawdown_le_20pct, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct |
| `POSTHISTORY_COST50` | ETH | 103.6103 | 34.54% | 1.049 | 0.669 | 60.90% | 52.94% | 34.34% | -4.8686 | 4.0156 | 否 | maximum_drawdown_le_20pct |

通过单元：0/12。

结论：NO_PREREGISTERED_ABSOLUTE_TREND_CANDIDATE：至少一个严格单元失败，排除本协议定义的 SMA50/200、1x、每日 01:00 执行 family。

CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。
