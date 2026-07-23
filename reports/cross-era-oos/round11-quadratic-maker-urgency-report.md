# Round 11 二次 Maker Wind-down 紧迫度扩展开发筛选

- 证据角色：Development + 已消费 Validation，仅用于扩展开发
- 稳定收益声明：否
- 候选数：1
- 合格候选数：0
- 选中候选：`NONE`
- Final OOS：`SEALED_NOT_EVALUATED`

公式：`offset_steps = 1.10 × remaining_ratio²`，固定 `W1440`。

| 候选 | DEV BASE | DEV COST50 | VAL BASE | VAL COST50 | 退出损失改善 | 配对收益保留 | 最弱种子 | 最大集中度 | 全过 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| W1440_QUADRATIC_E2 | 12/12 | 11/12 | 12/12 | 12/12 | 7.93% | 97.36% | 2.8178 | 32.78% | FAIL |

结论：NO_ROBUST_CANDIDATE：二次 Maker 紧迫度未通过四单元与机制门槛。
