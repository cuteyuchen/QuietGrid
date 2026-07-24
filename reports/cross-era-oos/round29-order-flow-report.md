# Round 29：小时主动买量不平衡结果

过去 8 根完整小时的主动买量不平衡均值达到 ±15% 后顺势执行；完整计入真实 funding、主动费率与滑点。

| 单元 | 资产 | 净收益 | 日 PF | Sharpe | 最大回撤 | 正收益月 | 价格 PnL | Funding | 成本 | 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `DEVELOPMENT_BASE` | BTC | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `DEVELOPMENT_BASE` | ETH | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `DEVELOPMENT_COST50` | BTC | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `DEVELOPMENT_COST50` | ETH | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `VALIDATION_BASE` | BTC | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `VALIDATION_BASE` | ETH | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `VALIDATION_COST50` | BTC | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `VALIDATION_COST50` | ETH | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `POSTHISTORY_BASE` | BTC | -6.2654 | 0.156 | -1.497 | 1.48% | 5.88% | 0.6928 | 0.0410 | 6.9991 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `POSTHISTORY_BASE` | ETH | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `POSTHISTORY_COST50` | BTC | -11.5147 | 0.034 | -1.966 | 2.38% | 5.88% | 0.6928 | 0.0410 | 12.2485 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |
| `POSTHISTORY_COST50` | ETH | 0.0000 | N/A | N/A | 0.00% | 0.00% | 0.0000 | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, daily_profit_factor_gt_1, daily_annualized_sharpe_gt_0_5, positive_calendar_month_ratio_ge_50pct, best_profitable_month_concentration_le_35pct |

通过单元：0/12。

结论：NO_PREREGISTERED_ORDER_FLOW_CANDIDATE：至少一个严格单元失败，排除本协议定义的 8 小时、15%、1x 主动买量不平衡 family。

CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。
