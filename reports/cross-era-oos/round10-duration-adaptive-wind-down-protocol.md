# Round 10：按窗口时长自适应 Wind-down 协议

生成日期：2026-07-23

## 目的与证据边界

Round 5 的固定 `W2160` 是现有退出防御中最接近全部门槛的候选：

- `DEV_BASE`、`DEV_COST50`、`VAL_COST50` 的 12 项检查全部通过；
- `VAL_BASE` 只失败“平均收益至少保留原始基线 75%”，实际约为 72.13%；
- 退出损失改善为 19.5156%，距离 20% 机制门槛约 0.4844 个百分点；
- 配对网格收益保留为 89.1982%。

固定 bar 数没有考虑休市窗口时长。Development 与已消费 Validation 的可交易窗口以
`3300` bars 为众数，同时存在约 `4740` bars 的长窗口和少量不足 `2160` bars 的短窗口。
固定 `W2160` 对长窗口退出准备不足，对短窗口则可能从首根可交易 bar 就停止新增库存。

本轮只检验一个结构假设：保持典型 `3300` bars 窗口与既有 `W2160` 完全对齐，按窗口
可交易长度同比例调整，并限制在既有 `W1440` 与 `W2880` 之间。该假设不新增固定
wind-down 时长，不搜索比例网格，也不修改入口、网格几何、库存或 Maker 参数。

- 原 Development 108 个窗口与已消费 Validation 54 个窗口继续仅作为扩展开发集；
- Final OOS 54 个窗口保持 `SEALED_NOT_EVALUATED`；
- 时长诊断只输出 Development/Validation 分布，未输出或使用 Final OOS 时长、收益、状态或入口特征；
- Development/Validation 正收益不得表述为稳定收益证据。

冻结来源：

- Round 5 结果：`c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084`；
- Round 9 结果：`1891a96aae528048296d8b4fe84434d39566dd3e89c220a66dff673c11b4e914`；
- BTC manifest：`2f26775d33cd2010cf2ac0a0837d1f1dd11540953d8a70c8dafa737c7db86d57`；
- ETH manifest：`8d41db529b1ae6fc6340c5d28d728148a82edc7aea6e754e43c20c438cd4e76f`。

## 唯一候选

候选 ID：`DAW_1440_2160_2880`。

对每个标的、每个 Development/Validation 窗口，令 `tradable_rows` 为观察期之后、强制
平仓之前的可交易 K 线数，实际 wind-down bar 数固定按下式解析：

```text
scaled = floor(tradable_rows * 2160 / 3300 + 0.5)
resolved_wind_down_bars = min(2880, max(1440, scaled))
```

关键映射必须满足：

- `tradable_rows = 3300` -> `2160`；
- `tradable_rows = 4740` -> `2880`；
- `tradable_rows = 1860` -> `1440`。

不得在查看结果后新增其他参考长度、比例、上下界或按标的变体。

## 固定策略

- `direction_mode`：`NEUTRAL`；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- BTC 入口：`de0.40_ve1.05_rr0.35`；
- ETH 入口：`de0.35_ve1.05_rr0.55`；
- BTC/ETH 本金、库存上限、unpaired lot 与 reduce target 保持 Round 5 不变；
- Maker 重挂间隔：5 bars；
- Maker 初始偏移：1.10 个网格步长；
- Maker unwind fraction：1.00；
- 成交概率、费用、滑点、资金费与 L0 保守撮合保持 Round 5 不变；
- 固定种子：`3, 10, 17, 31, 59, 97`。

## 四个独立验收单元

候选分别评估：

1. `DEV_BASE`：原 Development 108 个窗口，BASE 成本；
2. `DEV_COST50`：原 Development 108 个窗口，COST50 成本；
3. `VAL_BASE`：已消费 Validation 54 个窗口，BASE 成本；
4. `VAL_COST50`：已消费 Validation 54 个窗口，COST50 成本。

每个单元必须同时通过以下 12 项：

1. 6/6 种子组合净收益为正；
2. BTC 与 ETH 六种子合计净收益均为正；
3. 六个种子的 Profit Factor 均大于 1；
4. 最大回撤不高于 5%；
5. 最大回撤不比同拆分、同成本原始基线恶化超过 5%；
6. 最佳窗口集中度不高于 35%；
7. 最差 5% 窗口平均 PnL 不差于同拆分、同成本原始基线；
8. 六种子平均净收益至少保留同拆分、同成本原始基线的 75%；
9. 最差种子组合净收益为正；
10. BTC 至少保留原始基线已交易窗口的 25%；
11. ETH 至少保留原始基线已交易窗口的 25%；
12. 手续费/毛利润不高于原始基线的 1.25 倍。

任一单元失败即淘汰候选，不允许跨拆分或成本场景抵消。

## 机制门槛

以 Round 5 的固定 `W1440` 参考机制为基准，候选必须同时满足：

1. `stop_exit_pnl` 亏损绝对值改善至少 20%；
2. `paired_grid_pnl` 至少保留 60%。

## 执行完整性

1. 每个 worker 只能访问 Development/Validation 窗口 ID；
2. 每个已交易窗口缓存键中的实际 `wind_down_bars` 必须等于协议公式；
3. 两个标的在同一窗口必须解析为同一 `wind_down_bars`；
4. 默认研究策略不启用时长自适应，既有固定 `wind_down_bars` 行为必须保持不变；
5. 候选入口资格、网格参数、资本、库存规则和 Maker 参数必须与 Round 5 一致；
6. Final OOS 窗口 ID 不得进入任何评估、汇总或诊断输出；
7. Round 5 冻结结果与 manifest 哈希必须在运行前验证；
8. 生产默认配置不得修改。

## 停止条件

- 候选未通过任一四单元或机制门槛：记录 `NO_ROBUST_CANDIDATE`，不再围绕比例、参考长度或上下界继续调参；
- 候选全部通过：先写入包含确切公式、四单元结果、BASE/COST50 门槛和唯一候选哈希的 Final OOS 协议，再一次性评估 54 个 Final OOS 窗口；
- Final OOS 任一场景失败：记录 `NO_ROBUST_CANDIDATE`，不得围绕 Final OOS 调参或重跑；
- 本协议及 Development/Validation 结果不授权修改生产默认值，也不构成稳定收益声明。
