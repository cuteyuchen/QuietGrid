# Round 10 按窗口时长自适应 Wind-down 扩展开发筛选

- 证据角色：Development + 已消费 Validation，仅用于扩展开发
- 稳定收益声明：否
- 候选数：1
- 合格候选数：0
- 选中候选：`NONE`
- Final OOS：`SEALED_NOT_EVALUATED`

公式：`clamp(round(tradable_rows × 2160 / 3300), 1440, 2880)`。

| 候选 | DEV BASE | DEV COST50 | VAL BASE | VAL COST50 | 退出损失改善 | 配对收益保留 | 最弱种子 | 最大集中度 | 全过 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| DAW_1440_2160_2880 | 12/12 | 11/12 | 10/12 | 10/12 | 16.25% | 90.73% | 6.9611 | 26.53% | FAIL |

结论：NO_ROBUST_CANDIDATE：时长自适应 wind-down 未通过四单元与机制门槛。
