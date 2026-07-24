# Round 28：现货/季度交割合约现金套利结果

固定交割前 30 日入场、50 bps 最低正基差，只做多现货/做空季度合约并持有至交割前最后小时；不含 funding。

| 单元 | 资产 | 入场窗 | 净收益 | PF | 最大回撤 | 正收益窗 | 最佳窗集中度 | 价格 PnL | 成本 | 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `DEVELOPMENT_BASE` | BTC | 3 | 6.1119 | 46.419 | 0.91% | 66.67% | 58.10% | 9.6027 | 3.4907 | 否 | positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct |
| `DEVELOPMENT_BASE` | ETH | 3 | 0.8858 | 1.665 | 1.19% | 66.67% | 95.86% | 2.9156 | 2.0298 | 否 | positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct |
| `DEVELOPMENT_COST50` | BTC | 3 | 4.3655 | 7.131 | 1.03% | 66.67% | 59.65% | 9.6027 | 5.2372 | 否 | positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct |
| `DEVELOPMENT_COST50` | ETH | 3 | -0.1303 | 0.932 | 1.32% | 33.33% | 100.00% | 2.9156 | 3.0459 | 否 | total_net_profit_strictly_positive, profit_factor_gt_1, positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct |
| `VALIDATION_BASE` | BTC | 0 | 0.0000 | N/A | 0.00% | 0.00% | 100.00% | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, profit_factor_gt_1, positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct, minimum_active_window_count |
| `VALIDATION_BASE` | ETH | 0 | 0.0000 | N/A | 0.00% | 0.00% | 100.00% | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, profit_factor_gt_1, positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct, minimum_active_window_count |
| `VALIDATION_COST50` | BTC | 0 | 0.0000 | N/A | 0.00% | 0.00% | 100.00% | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, profit_factor_gt_1, positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct, minimum_active_window_count |
| `VALIDATION_COST50` | ETH | 0 | 0.0000 | N/A | 0.00% | 0.00% | 100.00% | 0.0000 | 0.0000 | 否 | total_net_profit_strictly_positive, profit_factor_gt_1, positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct, minimum_active_window_count |
| `POSTHISTORY_BASE` | BTC | 4 | 3.0057 | 8.793 | 0.40% | 50.00% | 67.37% | 7.9602 | 4.9545 | 否 | positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct, minimum_active_window_count |
| `POSTHISTORY_BASE` | ETH | 1 | 0.9792 | ∞ | 0.33% | 100.00% | 100.00% | 1.7156 | 0.7365 | 否 | best_profitable_trade_concentration_le_35pct, minimum_active_window_count |
| `POSTHISTORY_COST50` | BTC | 4 | 0.5266 | 1.326 | 0.73% | 50.00% | 77.28% | 7.9602 | 7.4336 | 否 | positive_trade_ratio_ge_75pct, best_profitable_trade_concentration_le_35pct, minimum_active_window_count |
| `POSTHISTORY_COST50` | ETH | 1 | 0.6102 | ∞ | 0.40% | 100.00% | 100.00% | 1.7156 | 1.1054 | 否 | best_profitable_trade_concentration_le_35pct, minimum_active_window_count |

通过单元：0/12。

结论：NO_PREREGISTERED_SPOT_QUARTERLY_CARRY_CANDIDATE：至少一个严格单元失败，排除本协议定义的 30 日、50 bps、正向现金套利 family。

CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。
