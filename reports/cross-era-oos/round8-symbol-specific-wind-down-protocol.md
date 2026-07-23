# Round 8：按标的固定 Wind-down 时序协议

生成日期：2026-07-23

## 研究定位

Round 5 表明统一提前 wind-down 存在明显的标的异质性：

- BTC 在 `DEV_COST50` 下需要早于 W1440 进入 wind-down，W2160/W2880 均把 BTC 六种子合计收益修复为正；
- ETH 的 W1440 在 Validation 仍为正，而统一 W2880 会使 Validation ETH 转负；
- Round 6、Round 7 的库存条件触发仍不能同时通过 Development/Validation 与 BASE/COST50。

本轮只检验一个结构假设：BTC 与 ETH 使用不同、且已在 Round 5 冻结过的固定 wind-down 时序，是否能同时保留 ETH 的 W1440 表现并修复 BTC 的 `DEV_COST50`。

原 Validation 已消费，本轮只能作为扩展开发证据。即使候选通过，也不得称为稳定收益；Final OOS 54 个窗口继续封存，不计算收益、市场状态或入口标签。

## 冻结证据

- Round 5 结果：`round5-early-wind-down-results.json`
  - SHA-256：`c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084`
- Round 7 结果：`round7-loss-conditioned-inventory-wind-down-results.json`
  - SHA-256：`c72f9a291574843d8b6b0fa67ba0e9b87329aa7aee70c224730a76c2f65dcf46`

执行前必须确认两个冻结结果均无合格候选、Final OOS 均为 `SEALED_NOT_EVALUATED`，且生产默认值未改变。

## 固定策略

- 方向：`NEUTRAL`；
- 固定种子：3、10、17、31、59、97；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- BTC 入口：`de0.40_ve1.05_rr0.35`；
- ETH 入口：`de0.35_ve1.05_rr0.55`；
- BTC/ETH 本金、库存上限、unpaired 规则、reduce target、Maker wind-down 重挂间隔/偏移/比例保持 Round 5 不变；
- `unpaired_lot_cap_enforcement=BAR_BOUNDARY`；
- 成交概率、费用、滑点与数据切分保持 Round 5 不变；
- 全局 `ResearchConfig.wind_down_bars` 固定为 W1440，仅允许研究联合策略中的按标的覆盖改变实际时序。

## 预注册候选

W1440/W1440 仅作为重算参考，不参与选择。

1. `SW_BTC2160_ETH1440`：BTC W2160，ETH W1440；
2. `SW_BTC2880_ETH1440`：BTC W2880，ETH W1440。

不得在查看结果后新增中间时长、交换 ETH 时序、修改入口过滤器或放宽门槛。

## 四个独立验收单元

每个候选分别评估：

1. `DEV_BASE`；
2. `DEV_COST50`；
3. `VAL_BASE`；
4. `VAL_COST50`。

每个单元继续使用 Round 5 的 12 项门槛：

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

1. 相对 W1440/W1440 参考，负的 `stop_exit_pnl` 绝对值至少改善 20%；
2. `paired_grid_pnl` 至少保留 W1440/W1440 参考的 60%。

该门槛防止候选仅通过停止交易获得表面改善。

## 执行完整性

1. 脚本必须重算 W1440/W1440，并验证四单元关键汇总与 Round 5 冻结参考一致；
2. 必须验证每个标的实际传入的 `wind_down_bars` 与候选注册值一致；
3. 研究覆盖字段默认值必须为 `None`，默认路径解析为全局 W1440；
4. 任何完整性检查失败都必须停止，不允许继续选择候选；
5. Final OOS 窗口 ID 不得进入任何评估单元。

## 唯一选择规则

只有四单元与机制门槛全部通过的候选才合格。若两个候选均合格，沿用 Round 5 minimax 顺序：

1. 最大化四单元最小的最差种子总 PnL；
2. 最大化四单元、两个标的中最小的标的总 PnL；
3. 最大化四单元最小的平均总 PnL；
4. 最大化四单元最小的种子 Profit Factor；
5. 最小化四单元最大的最佳窗口集中度；
6. 最小化四单元最大的回撤；
7. 按候选 ID 字典序打破完全相同的并列。

## 停止条件

- 无候选通过：记录 `NO_ROBUST_CANDIDATE`，Final OOS 保持封存；
- 选出唯一候选：先写包含 BTC/ETH 精确 wind-down 时序与 BASE/COST50 门槛的 Final OOS 协议，再一次性评估 Final OOS；
- Final OOS 任一场景失败：不得围绕该 Final OOS 调参或重跑；
- 本协议不授权修改生产默认值。
