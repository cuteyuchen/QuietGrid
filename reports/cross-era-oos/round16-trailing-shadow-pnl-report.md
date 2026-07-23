# Round 16：滚动影子 PnL Phase A 结果

固定策略过去 K 个已完成窗口的六种子平均影子 PnL 必须大于 0；CURRENT Final OOS 未评估。

| 候选 | Lookback | 通过单元 | 最差种子 PnL | 最低覆盖 | 全通过 |
| --- | ---: | ---: | ---: | ---: | --- |
| `TRAIL_SHADOW_PNL_K4` | 4 | 6/16 | -8.5729 | 10.71% | 否 |
| `TRAIL_SHADOW_PNL_K8` | 8 | 3/16 | -14.4080 | 3.57% | 否 |
| `TRAIL_SHADOW_PNL_K13` | 13 | 4/16 | -14.1479 | 0.00% | 否 |

选中候选：无。

结论：NO_ROBUST_TRAILING_SHADOW_CANDIDATE：3 个滚动影子 PnL 候选均未通过全部 16 个 Phase A 单元。

生产默认值未修改；没有独立授权文件时，CURRENT Final OOS 继续封存。
