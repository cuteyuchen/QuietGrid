# Round 26：USD-M 季度期限价差单一因果候选结果

方向仅由入场季度-永续基差符号决定，固定持有 168 小时；完整计入永续实际 funding 与双腿 Maker 往返费用。

| 单元 | 资产 | 窗口 | 价格 PnL | Funding | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `DEVELOPMENT_BASE` | BTC | 67 | 27.1120 | -63.3872 | 13.3943 | -49.6695 | 0.397 | 10.74% | 37.31% | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |
| `DEVELOPMENT_BASE` | ETH | 67 | 14.1114 | -42.5739 | 8.0657 | -36.5282 | 0.366 | 13.39% | 31.34% | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct |
| `DEVELOPMENT_COST50` | BTC | 67 | 27.1120 | -63.3872 | 20.0914 | -56.3666 | 0.352 | 11.48% | 31.34% | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |
| `DEVELOPMENT_COST50` | ETH | 67 | 14.1114 | -42.5739 | 12.0985 | -40.5611 | 0.330 | 14.14% | 26.87% | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |
| `VALIDATION_BASE` | BTC | 48 | 5.7030 | -12.6769 | 9.6592 | -16.6331 | 0.263 | 3.33% | 20.83% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `VALIDATION_BASE` | ETH | 48 | 6.8699 | -8.0074 | 5.8048 | -6.9423 | 0.434 | 2.49% | 35.42% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct |
| `VALIDATION_COST50` | BTC | 48 | 5.7030 | -12.6769 | 14.4888 | -21.4627 | 0.188 | 4.29% | 18.75% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `VALIDATION_COST50` | ETH | 48 | 6.8699 | -8.0074 | 8.7072 | -9.8446 | 0.312 | 3.30% | 27.08% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct |
| `POSTHISTORY_BASE` | BTC | 91 | 25.1248 | -20.9660 | 18.2187 | -14.0600 | 0.474 | 3.14% | 35.16% | 否 | total_pnl_positive, profit_factor_gt_1 |
| `POSTHISTORY_BASE` | ETH | 91 | 13.6250 | -13.3238 | 10.9191 | -10.6178 | 0.453 | 3.94% | 34.07% | 否 | total_pnl_positive, profit_factor_gt_1 |
| `POSTHISTORY_COST50` | BTC | 91 | 25.1248 | -20.9660 | 27.3281 | -23.1693 | 0.297 | 4.80% | 26.37% | 否 | total_pnl_positive, profit_factor_gt_1 |
| `POSTHISTORY_COST50` | ETH | 91 | 13.6250 | -13.3238 | 16.3786 | -16.0773 | 0.306 | 5.63% | 29.67% | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct |

通过单元：0/12。

结论：NO_PREREGISTERED_QUARTERLY_CALENDAR_SPREAD_CANDIDATE：至少一个单元在固定一周、因果基差方向、实际 funding 和理想同步 Maker 假设下仍失败，排除本协议定义的 USD-M 永续/季度期限价差 family。

CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。
