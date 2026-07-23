# Round 11：二次 Maker Wind-down 紧迫度协议

生成日期：2026-07-23

## 目的与证据边界

Round 5 的固定 `W2160` 是迄今最接近全部门槛的候选，但仍有两个明确缺口：

- `VAL_BASE` 平均收益仅保留原始基线的 `72.1321%`，低于 `75%`；
- 相对固定 `W1440` 参考的退出损失改善为 `19.5156%`，低于 `20%`。

Round 10 已证明按窗口时长延长 wind-down 不能解决该矛盾：其 Validation 平均收益只保留
约 `48.15% / 50.10%`，ETH 在 BASE/COST50 均为负，退出损失改善也只有 `16.2544%`。
因此不得继续调整 wind-down 比例、参考长度、上下界或按标的时长。

本轮只检验一个不同的执行结构假设：保持固定 `W1440`，不提前取消网格新增订单；进入
wind-down 后，Maker reduce 偏移由现有线性紧迫度改为二次紧迫度，使挂单在退出阶段更快
接近盘口，争取减少终场强平库存和滑点，同时保留 W1440 之前的配对收益。

- 原 Development 108 个窗口与已消费 Validation 54 个窗口继续仅作为扩展开发集；
- Final OOS 54 个窗口保持 `SEALED_NOT_EVALUATED`；
- 不读取或输出 Final OOS 收益、状态、窗口特征或路径；
- Development/Validation 通过也不得表述为稳定收益证据。

冻结来源：

- Round 5 结果：`c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084`；
- Round 10 结果：`49d0ba18bb3464aa7e14f32695478d95151bb0b9f71e8690a473939106f49ea8`；
- BTC manifest：`2f26775d33cd2010cf2ac0a0837d1f1dd11540953d8a70c8dafa737c7db86d57`；
- ETH manifest：`8d41db529b1ae6fc6340c5d28d728148a82edc7aea6e754e43c20c438cd4e76f`。

## 唯一候选

参考 ID：`W1440_LINEAR_E1`，只用于重算一致性，不参与选择。

候选 ID：`W1440_QUADRATIC_E2`。

固定 `wind_down_bars = 1440`。每次 Maker 重挂时令：

```text
remaining_ratio = clamp(remaining_bars / active_wind_down_bars, 0, 1)
offset_steps = 1.10 * remaining_ratio ^ 2
```

参考路径保持现有线性公式：

```text
offset_steps = 1.10 * remaining_ratio
```

候选指数精确冻结为 `2.0`。不得在查看结果后新增 `1.25`、`1.5`、`2.5`、`3.0`、
分段曲线、按标的指数或其他紧迫度函数。

## 固定策略

- `direction_mode`：`NEUTRAL`；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- BTC 入口：`de0.40_ve1.05_rr0.35`；
- ETH 入口：`de0.35_ve1.05_rr0.55`；
- BTC/ETH `wind_down_bars`：固定 `1440`；
- Maker 重挂间隔：`5` bars；
- Maker 初始偏移：`1.10` 个网格步长；
- Maker unwind fraction：`1.00`；
- BTC/ETH 本金、库存上限、unpaired lot 与 reduce target 保持 Round 5 不变；
- `unpaired_lot_cap_enforcement=BAR_BOUNDARY`；
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
8. 六种子平均净收益至少保留同拆分、同成本原始基线的 75%；若基线不为正，则候选必须为正；
9. 最差种子组合净收益为正；
10. BTC 至少保留原始基线已交易窗口的 25%；
11. ETH 至少保留原始基线已交易窗口的 25%；
12. 手续费/毛利润不高于原始基线的 1.25 倍。

任一单元失败即淘汰候选，不允许跨拆分或成本场景抵消。

## 机制门槛

相对 `W1440_LINEAR_E1` 的 `DEV_COST50` 机制，候选必须同时满足：

1. `stop_exit_pnl` 亏损绝对值改善至少 20%；
2. `paired_grid_pnl` 至少保留 60%。

## 执行完整性

1. 参考路径必须重算，并与 Round 5 的 W1440 四单元汇总、交易覆盖和机制汇总一致；
2. 每个 worker 只能访问 Development/Validation 窗口 ID；
3. 参考与候选实际 `wind_down_bars` 均必须为 `1440`；
4. 参考实际紧迫度指数必须为 `1.0`，候选必须为 `2.0`；
5. 候选入口资格、网格参数、资本、库存规则、Maker 间隔、初始偏移和 unwind fraction 必须与参考一致；
6. 默认 `wind_down_urgency_exponent` 必须为 `1.0`，既有线性行为保持不变；
7. Final OOS 窗口 ID 不得进入任何评估、缓存、汇总或诊断输出；
8. Round 5、Round 10 与 manifest 哈希必须在运行前验证；
9. 生产默认配置不得修改。

## 停止条件

- 候选未通过任一四单元或机制门槛：记录 `NO_ROBUST_CANDIDATE`，不再围绕指数或曲线调参；
- 候选全部通过：先写入包含精确二次公式、四单元结果、BASE/COST50 门槛和唯一候选哈希的 Final OOS 协议，再一次性评估 54 个 Final OOS 窗口；
- Final OOS 任一场景失败：记录 `NO_ROBUST_CANDIDATE`，不得围绕 Final OOS 调参或重跑；
- 本协议及 Development/Validation 结果不授权修改生产默认值，也不构成稳定收益声明。
