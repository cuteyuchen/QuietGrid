# Round 9：按标的 Maker Wind-down 偏移协议

生成日期：2026-07-23

## 研究定位

Round 8 已证明：BTC W2880、ETH W1440 可以让四个单元的组合与逐标的收益保持为正，且 `DEV_COST50` 的退出损失改善和配对收益保留门槛均通过；剩余主要失败是：

- Validation 的 ETH 单标的最佳窗口集中度约为 38%–39%，高于 35%；
- W2880 使 Validation 平均收益保留低于 75%。

窗口级诊断确认集中度由 ETH 驱动。`VAL_COST50_SEED17` 中 ETH 仅有 9 个正收益窗口，最佳窗口贡献约 38.58%；其配对网格收益为正，但终场退出损失消耗了大部分收益。

本轮只检验一个结构假设：保持 BTC/ETH 已冻结的不同 wind-down 时序，通过给 ETH 使用此前 Maker 偏移稳健平台中已出现的更远挂单偏移，改善 ETH 的终场去库存结果和正收益分布；BTC Maker 偏移保持不变。

原 Validation 已消费，本轮只能作为扩展开发证据。即使候选通过，也不得称为稳定收益；Final OOS 54 个窗口继续封存。

## 冻结证据

- Round 8 结果：`round8-symbol-specific-wind-down-results.json`
  - SHA-256：`2a98ad21d638cc82fa3e9556b1a4a40b53658205c0fc274c2d269d0569eb7a5e`
- 既有 Maker 偏移平台报告：`../robustness/maker-unwind-local-platform-r090-s255-20260719.md`
  - SHA-256：`995fc316600c0573255e57b3d7eda37b81ddfca44a2dff1644205991ad7821de`

旧报告只用于冻结候选数值 `1.20/1.30`，不作为本轮跨周期通过证据。

## 固定策略

- 方向：`NEUTRAL`；
- 固定种子：3、10、17、31、59、97；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- BTC 入口：`de0.40_ve1.05_rr0.35`；
- ETH 入口：`de0.35_ve1.05_rr0.55`；
- BTC wind-down：W2880；
- ETH wind-down：W1440；
- Maker 重挂间隔：5 bar；
- Maker unwind fraction：1.00；
- BTC Maker 初始偏移：1.10 个网格步长；
- BTC/ETH 本金、库存上限、unpaired 规则、reduce target 保持 Round 8 不变；
- `unpaired_lot_cap_enforcement=BAR_BOUNDARY`；
- 成交概率、费用、滑点与数据切分保持 Round 8 不变。

## 预注册候选

`SMO_ETH110` 仅作为重算参考，不参与选择。

1. `SMO_ETH120`：ETH Maker 初始偏移 1.20 个网格步长；
2. `SMO_ETH130`：ETH Maker 初始偏移 1.30 个网格步长。

不得在查看结果后新增 1.15、1.25 或其他中间值，不得修改重挂间隔、unwind fraction、入口过滤器或 wind-down 时序。

## 四个独立验收单元

每个候选分别评估：

1. `DEV_BASE`；
2. `DEV_COST50`；
3. `VAL_BASE`；
4. `VAL_COST50`。

每个单元继续使用 Round 8 的 12 项门槛：

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

1. 相对 W1440/W1440、Maker 偏移 1.10 的参考，负的 `stop_exit_pnl` 绝对值至少改善 20%；
2. `paired_grid_pnl` 至少保留该参考的 60%。

## 执行完整性

1. 脚本必须重算 `SMO_ETH110`，并验证四单元关键汇总与 Round 8 的 `SW_BTC2880_ETH1440` 一致；
2. 必须验证每个标的实际使用的 `wind_down_bars` 与 Maker 初始偏移等于候选注册值；
3. 按标的覆盖字段默认值必须为 `None`，默认路径继续使用联合策略的全局 Maker 偏移 1.10；
4. 候选的入口资格不变，交易覆盖必须与 Round 8 的 W2880/ETH W1440 参考一致；
5. Final OOS 窗口 ID 不得进入任何评估单元；
6. 任一完整性检查失败必须停止，不允许继续选择候选。

## 唯一选择规则

只有四单元与机制门槛全部通过的候选才合格。若两个候选均合格，沿用 Round 8 minimax 顺序：

1. 最大化四单元最小的最差种子总 PnL；
2. 最大化四单元、两个标的中最小的标的总 PnL；
3. 最大化四单元最小的平均总 PnL；
4. 最大化四单元最小的种子 Profit Factor；
5. 最小化四单元最大的最佳窗口集中度；
6. 最小化四单元最大的回撤；
7. 按候选 ID 字典序打破完全相同的并列。

## 停止条件

- 无候选通过：记录 `NO_ROBUST_CANDIDATE`，Final OOS 保持封存；
- 选出唯一候选：先写包含 BTC/ETH 精确 wind-down 与 Maker 偏移的 Final OOS 协议，再一次性评估 Final OOS；
- Final OOS 任一场景失败：不得围绕该 Final OOS 调参或重跑；
- 本协议不授权修改生产默认值。
