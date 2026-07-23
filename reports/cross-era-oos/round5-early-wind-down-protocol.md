# Round 5：提前计划性 Wind-down 协议

生成日期：2026-07-23

## 研究假设

Round 4 已证明继续扩入口过滤网格不能同时通过 Development/Validation 与 BASE/COST50。窗口级诊断进一步显示：

- 实际平均格距约为 0.72%–0.81%，明显高于固定 `min_step_pct=0.18%`，因此“格距过窄”不是主要机制；
- `DEV_COST50_SEED97` 的 BTC 配对网格收益为正，但库存退出损失与手续费将净收益压为负；
- Validation 的剩余失败主要是 ETH 单标的最佳窗口集中度约 38%–39%。

本轮唯一新假设是：比当前“最后 24 小时”更早停止新增库存并进入既有 Maker wind-down，可在不修改网格几何、方向和退出算法的前提下减少最终库存损失，同时保留足够的配对收益与交易覆盖。

本轮不测试新的入口阈值、利润保护、波动触发、防御减仓、库存上限或格距参数。

## 冻结证据

- Round 4 结果：`round4-extended-development-results.json`
  - SHA-256：`003ef0c486edbbd0bda27b06301ec66de95691924a59559e927cb36a09eb9045`
- Round 4 窗口级诊断：`round4-diagnostics.json`
  - SHA-256：`f10bed52850af45c0abeb10a95c1ffe86d97c3dcef40e53793d3a47506c6698d`

原 Validation 已消费并降级为扩展开发证据。Final OOS 54 个窗口继续封存，不计算收益、市场状态或入口标签。

## 固定策略

- 方向：`NEUTRAL`；
- 固定种子：3、10、17、31、59、97；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- BTC 入口：`de0.40_ve1.05_rr0.35`；
- ETH 入口：`de0.35_ve1.05_rr0.55`；
- BTC/ETH 本金、库存上限、unpaired 规则、reduce target、Maker wind-down 重挂间隔/偏移/比例保持 Round 4 不变；
- `unpaired_lot_cap_enforcement=BAR_BOUNDARY`；
- 成交概率、费用、滑点与数据切分保持 Round 4 不变。

固定入口组合来自 Round 4：它在 `DEV_BASE` 通过 12/12 门槛，并在 `VAL_BASE`、`VAL_COST50` 各通过 11/12，两个 Validation 单元唯一失败均为集中度；本轮不再比较其他入口组合。

## 预注册候选

当前 `wind_down_bars=1440` 仅作为重算参考，不参与选择。

1. `W2160`：最后 2160 根 1m bar（36 小时）进入 wind-down；
2. `W2880`：最后 2880 根 1m bar（48 小时）进入 wind-down。

不得在查看结果后新增中间值、延长值或修改 Maker wind-down 的其他字段。

## 四个独立验收单元

每个候选分别评估：

1. `DEV_BASE`；
2. `DEV_COST50`；
3. `VAL_BASE`；
4. `VAL_COST50`。

每个单元继续使用 Round 4 的 12 项门槛：

1. 6/6 种子组合净收益为正；
2. 最差种子净收益为正；
3. BTC 与 ETH 六种子合计净收益均为正；
4. 六个种子的组合 Profit Factor 均大于 1；
5. 最大回撤不高于 5%；
6. 最大回撤不比同拆分、同成本的原始 W1440 基线恶化超过 5%；
7. 最佳窗口集中度不高于 35%；
8. 最差 5% 窗口平均 PnL 不差于同拆分、同成本原始 W1440 基线；
9. 平均净收益至少保留同拆分、同成本原始 W1440 基线的 75%；若基线不为正，则候选必须为正；
10. BTC 至少保留原始 W1440 基线已交易窗口的 25%；
11. ETH 至少保留原始 W1440 基线已交易窗口的 25%；
12. 手续费/毛利润不高于原始 W1440 基线的 1.25 倍。

## 机制门槛

候选还必须在 `DEV_COST50` 同时满足：

1. 相对相同入口组合的 W1440 参考，负的 `stop_exit_pnl` 绝对值至少改善 20%；
2. `paired_grid_pnl` 至少保留 W1440 参考的 60%。

该门槛用于防止候选仅通过大面积停止交易获得表面改善。

## 执行完整性

脚本必须重算 W1440，并验证固定入口组合的四单元关键汇总与 Round 4 冻结结果一致；不一致时停止，不允许继续选择候选。

## 唯一选择规则

只有四单元与机制门槛全部通过的候选才合格。若两个候选均合格，沿用 Round 4 minimax 顺序：

1. 最大化四单元最小的最差种子总 PnL；
2. 最大化四单元、两个标的中最小的标的总 PnL；
3. 最大化四单元最小的平均总 PnL；
4. 最大化四单元最小的种子 Profit Factor；
5. 最小化四单元最大的最佳窗口集中度；
6. 最小化四单元最大的回撤；
7. 按候选 ID 字典序打破完全相同的并列。

## 停止条件

- 无候选通过：记录 `NO_ROBUST_CANDIDATE`，Final OOS 保持封存；
- 选出唯一候选：先写包含精确 wind-down 参数与 BASE/COST50 门槛的 Final OOS 协议，再一次性评估 Final OOS；
- Final OOS 任一场景失败：不得围绕该 Final OOS 调参或重跑；
- 本协议不授权修改生产默认值。
