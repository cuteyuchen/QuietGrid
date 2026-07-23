# Round 25：Binance–BitMEX Funding Spread 乐观上界结果

Oracle 事后使用完整窗口 funding spread 符号选择跨所方向；忽略成交基差、反向合约换算、保证金、腿间延迟与交易所风险。

| 单元 | 资产/交易对 | 窗口 | Funding 收入 | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | B/M 覆盖 | 通过 | 失败检查 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `PREHISTORY_EXTERNAL_BASE` | BTC `BTCUSDT/XBTUSD` | 28 | 11.8411 | 5.6000 | 6.2411 | 4.091 | 0.24% | 46.43% | 100%/100% | 否 | best_window_concentration_le_35pct |
| `PREHISTORY_EXTERNAL_BASE` | ETH `ETHUSDT/ETHUSD` | 28 | 10.0854 | 3.3600 | 6.7254 | 25.020 | 0.09% | 75.00% | 100%/100% | 是 |  |
| `PREHISTORY_EXTERNAL_COST50` | BTC `BTCUSDT/XBTUSD` | 28 | 11.8411 | 8.4000 | 3.4411 | 1.969 | 0.45% | 42.86% | 100%/100% | 否 | best_window_concentration_le_35pct |
| `PREHISTORY_EXTERNAL_COST50` | ETH `ETHUSDT/ETHUSD` | 28 | 10.0854 | 5.0400 | 5.0454 | 7.628 | 0.14% | 67.86% | 100%/100% | 是 |  |
| `CURRENT_DEVELOPMENT_BASE` | BTC `BTCUSDT/XBTUSD` | 108 | 22.6081 | 21.6000 | 1.0081 | 1.125 | 0.50% | 37.04% | 100%/100% | 是 |  |
| `CURRENT_DEVELOPMENT_BASE` | ETH `ETHUSDT/ETHUSD` | 108 | 57.4303 | 12.9600 | 44.4703 | 44.409 | 0.16% | 82.41% | 100%/100% | 是 |  |
| `CURRENT_DEVELOPMENT_COST50` | BTC `BTCUSDT/XBTUSD` | 108 | 22.6081 | 32.4000 | -9.7919 | 0.377 | 1.96% | 22.22% | 100%/100% | 否 | total_pnl_positive, profit_factor_gt_1, positive_window_ratio_ge_25pct |
| `CURRENT_DEVELOPMENT_COST50` | ETH `ETHUSDT/ETHUSD` | 108 | 57.4303 | 19.4400 | 37.9903 | 17.448 | 0.27% | 77.78% | 100%/100% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_BASE` | BTC `BTCUSDT/XBTUSD` | 49 | 8.9865 | 9.8000 | -0.8135 | 0.824 | 0.38% | 28.57% | 100%/100% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_BASE` | ETH `ETHUSDT/ETHUSD` | 49 | 13.6689 | 5.8800 | 7.7889 | 14.151 | 0.19% | 83.67% | 100%/100% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_COST50` | BTC `BTCUSDT/XBTUSD` | 49 | 8.9865 | 14.7000 | -5.7135 | 0.328 | 1.14% | 18.37% | 100%/100% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_COST50` | ETH `ETHUSDT/ETHUSD` | 49 | 13.6689 | 8.8200 | 4.8489 | 4.526 | 0.29% | 61.22% | 100%/100% | 是 |  |
| `POSTHISTORY_EXTERNAL_BASE` | BTC `BTCUSDT/XBTUSD` | 108 | 8.5239 | 21.6000 | -13.0761 | 0.036 | 2.64% | 6.48% | 100%/100% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_BASE` | ETH `ETHUSDT/ETHUSD` | 108 | 23.6884 | 12.9600 | 10.7284 | 11.738 | 0.15% | 74.07% | 100%/100% | 是 |  |
| `POSTHISTORY_EXTERNAL_COST50` | BTC `BTCUSDT/XBTUSD` | 108 | 8.5239 | 32.4000 | -23.8761 | 0.008 | 4.78% | 0.93% | 100%/100% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_COST50` | ETH `ETHUSDT/ETHUSD` | 108 | 23.6884 | 19.4400 | 4.2484 | 2.262 | 0.60% | 49.07% | 100%/100% | 是 |  |

通过单元：9/16。

结论：NO_PREREGISTERED_CROSS_VENUE_FUNDING_SPREAD_CANDIDATE：至少一个单元在未来已知 funding spread 方向、零成交基差和同步 Maker 假设下仍失败，排除本协议定义的周末双永续 funding spread family。

CURRENT Final OOS 保持封存；未注册生产候选；direction_mode 仍为 NEUTRAL；生产默认值未修改。
