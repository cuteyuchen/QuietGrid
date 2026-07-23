# Round 12：二次 Maker 紧迫度与因果波动减仓整合协议

生成日期：2026-07-23

## 目的与证据边界

Round 11 的固定 `W1440` 二次 Maker 紧迫度候选取得了当前最好的 Validation 收益保持：

- `VAL_BASE`、`VAL_COST50` 均为 12/12；
- Validation 平均收益分别保留原始基线的 `96.5799%` 与 `111.7057%`；
- BTC/ETH、六个种子、Profit Factor、尾部、集中度与费用门槛均通过。

但它仍有两个明确缺口：

- `DEV_COST50` 最大回撤为 `5.3579%`，高于 `5%`；
- 相对 `W1440` 线性参考的退出损失改善只有 `7.9306%`，低于 `20%`。

因此不得再调整紧迫度指数、曲线或 wind-down 时长。本轮只检验一个整合假设：保持
Round 11 的固定 `W1440` 与二次 Maker 紧迫度不变，叠加此前独立研究中已冻结的 P4
因果波动减仓 `V1.50/N10/F20`，在连续波动扩张时主动减少 20% 库存，以降低回撤与
终场残余库存。P4 数值不根据当前跨周期结果重新搜索。

- 原 Development 108 个窗口与已消费 Validation 54 个窗口继续仅作为扩展开发集；
- Final OOS 54 个窗口保持 `SEALED_NOT_EVALUATED`；
- 不读取或输出 Final OOS 收益、状态、窗口特征或路径；
- Development/Validation 通过也不得表述为稳定收益证据。

冻结来源：

- Round 11 结果：`ab769bc64cb4e4b8fd3294bbeb02b8e673be1b3cd1033efc33695bf47c840f0a`；
- P4 波动防御结果：`d7a6dabfa637a9c24d095f1e153b9c0af6ef0278db554d53d811e113e8eb4bc8`；
- Round 5 结果：`c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084`；
- BTC manifest：`2f26775d33cd2010cf2ac0a0837d1f1dd11540953d8a70c8dafa737c7db86d57`；
- ETH manifest：`8d41db529b1ae6fc6340c5d28d728148a82edc7aea6e754e43c20c438cd4e76f`。

## 唯一候选

参考 ID：`W1440_LINEAR_NO_VOL`，只用于重算一致性，不参与选择。

候选 ID：`Q2_V150_N10_F20`。

### 固定 Maker wind-down

```text
wind_down_bars = 1440
remaining_ratio = clamp(remaining_bars / active_wind_down_bars, 0, 1)
offset_steps = 1.10 * remaining_ratio ^ 2
reprice_interval_bars = 5
unwind_fraction = 1.00
```

### 固定因果波动减仓

```text
volatility_reduce_expansion_ratio = 1.50
volatility_reduce_after_breaches = 10
volatility_reduce_fraction = 0.20
volatility_reduce_mode = BOTH
volatility_reduce_only_when_losing = false
volatility_wind_down_after_reduce = false
volatility_resume_after_normal_bars = 0
```

波动扩张值必须只使用当前 Bar 开盘前已经闭合的历史 K 线，保持现有无前视实现。

不得在查看结果后修改 `V/N/F`、紧迫度指数、重挂间隔、偏移、wind-down 时长、按标的
参数或只亏损/冻结/恢复模式，也不得新增第二个组合候选。

## 固定策略

- `direction_mode`：`NEUTRAL`；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- BTC 入口：`de0.40_ve1.05_rr0.35`；
- ETH 入口：`de0.35_ve1.05_rr0.55`；
- BTC/ETH 本金、库存上限、unpaired lot 与 reduce target 保持 Round 5 不变；
- `unpaired_lot_cap_enforcement=BAR_BOUNDARY`；
- 成交概率、费用、滑点、资金费与 L0 保守撮合保持 Round 11 不变；
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

相对 `W1440_LINEAR_NO_VOL` 的 `DEV_COST50` 机制，候选必须同时满足：

1. `stop_exit_pnl` 亏损绝对值改善至少 20%；
2. `paired_grid_pnl` 至少保留 60%；
3. `volatility_reduce_count > 0`，证明因果波动减仓实际执行。

## 执行完整性

1. 参考路径必须重算，并与 Round 5 的 W1440 四单元汇总、交易覆盖和机制汇总一致；
2. 每个 worker 只能访问 Development/Validation 窗口 ID；
3. 参考实际参数必须为 W1440、线性指数 1.0、波动减仓关闭；
4. 候选实际参数必须为 W1440、二次指数 2.0、V1.50/N10/F20/BOTH；
5. 候选入口资格、网格参数、资本、库存规则与参考一致；
6. Final OOS 窗口 ID 不得进入任何评估、缓存、汇总或诊断输出；
7. Round 5、Round 11、P4 与 manifest 哈希必须在运行前验证；
8. 默认生产路径的紧迫度指数仍为 1.0，波动减仓仍关闭；
9. 生产默认配置不得修改。

## 停止条件

- 候选未通过任一四单元或机制门槛：记录 `NO_ROBUST_CANDIDATE`，不再围绕该组合调参；
- 候选全部通过：先写入包含精确组合公式、四单元结果、BASE/COST50 门槛和唯一候选哈希的 Final OOS 协议，再一次性评估 54 个 Final OOS 窗口；
- Final OOS 任一场景失败：记录 `NO_ROBUST_CANDIDATE`，不得围绕 Final OOS 调参或重跑；
- 本协议及 Development/Validation 结果不授权修改生产默认值，也不构成稳定收益声明。
