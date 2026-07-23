# Round 20：BTC/ETH β 中性 Z-score Taker Phase A 结果

固定 180 分钟观察、2σ 入场、均值退出、4σ 止损、同步 Taker；CURRENT Final OOS 未读取。

| 单元 | 交易/授权 | 总净收益 | PF | 最大回撤 | 最大集中度 | 覆盖率 | 平均持有 | 通过 | 失败检查 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `CURRENT_DEVELOPMENT_BASE` | 107/108 | -189.0335 | 0.189 | 23.63% | 16.12% | 99.07% | 189.6m | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct |
| `CURRENT_DEVELOPMENT_COST50` | 107/108 | -403.1682 | 0.034 | 50.40% | 35.85% | 99.07% | 189.6m | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |
| `CURRENT_VALIDATION_BASE` | 54/54 | -127.9306 | 0.026 | 16.15% | 46.65% | 100.00% | 156.8m | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |
| `CURRENT_VALIDATION_COST50` | 54/54 | -235.9952 | 0.000 | 29.63% | 100.00% | 100.00% | 156.8m | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |
| `PREHISTORY_EXTERNAL_BASE` | 28/28 | -33.8040 | 0.142 | 4.38% | 67.81% | 100.00% | 138.9m | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct |
| `PREHISTORY_EXTERNAL_COST50` | 28/28 | -89.7659 | 0.019 | 11.34% | 100.00% | 100.00% | 138.9m | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |
| `SPOT_EXTERNAL_BASE` | 99/101 | -207.7671 | 0.157 | 26.40% | 22.13% | 98.02% | 140.7m | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct |
| `SPOT_EXTERNAL_COST50` | 99/101 | -405.7976 | 0.029 | 51.15% | 54.43% | 98.02% | 140.7m | 否 | total_pnl_positive, profit_factor_gt_1, max_drawdown_le_5pct, best_window_concentration_le_35pct |

通过单元：0/8。

选中候选：无。

结论：NO_ROBUST_CROSS_ASSET_ZSCORE_CANDIDATE：唯一注册候选未通过全部 8 个 Phase A pair cell，禁止搜索相邻 Z-score 或执行参数。

生产默认值未修改；没有独立授权文件时 CURRENT Final OOS 继续封存。
