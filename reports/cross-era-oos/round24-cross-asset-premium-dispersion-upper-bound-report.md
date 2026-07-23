# Round 24：BTC/ETH Cross-Asset Premium Dispersion 乐观上界结果

BTC 与 ETH 各自保持 Spot/永续 delta-neutral，两个 basis book 等名义、方向相反；Oracle 只事后选择四腿共同退出分钟。

| 单元 | 窗口 | 联合基差 | 联合 Funding | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `PREHISTORY_EXTERNAL_BASE` | 27 | 10.7321 | 2.1760 | 6.4800 | 6.4281 | 18.242 | 0.09% | 77.78% | 是 |  |
| `PREHISTORY_EXTERNAL_COST50` | 27 | 10.7321 | 2.1760 | 9.7200 | 3.1881 | 3.190 | 0.19% | 55.56% | 否 | best_window_concentration_le_35pct |
| `CURRENT_DEVELOPMENT_BASE` | 108 | 42.7191 | 3.9015 | 25.9200 | 20.7006 | 13.302 | 1.35% | 72.22% | 是 |  |
| `CURRENT_DEVELOPMENT_COST50` | 108 | 42.7191 | 3.9015 | 38.8800 | 7.7406 | 2.008 | 1.38% | 38.89% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_BASE` | 49 | 13.9559 | 0.8103 | 11.7600 | 3.0062 | 2.862 | 0.66% | 55.10% | 是 |  |
| `CURRENT_VALIDATION_COMPLETE_MONTHS_COST50` | 49 | 13.9559 | 0.8103 | 17.6400 | -2.8738 | 0.459 | 0.99% | 24.49% | 否 | total_pnl_positive, profit_factor_gt_1, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_BASE` | 107 | 20.2451 | 1.2793 | 25.6800 | -4.1556 | 0.544 | 1.00% | 19.63% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |
| `POSTHISTORY_EXTERNAL_COST50` | 107 | 20.2451 | 1.2793 | 38.5200 | -16.9956 | 0.169 | 3.10% | 7.48% | 否 | total_pnl_positive, profit_factor_gt_1, best_window_concentration_le_35pct, positive_window_ratio_ge_25pct |

通过单元：4/8。

结论：NO_PREREGISTERED_CROSS_ASSET_PREMIUM_DISPERSION_CANDIDATE：至少一个单元在等名义四腿、causal spread 方向和 Oracle 联合退出下仍失败，排除本协议定义的 dispersion family。

CURRENT Final OOS 未读取；没有调整 Round 23 相邻参数；生产默认值未修改。
