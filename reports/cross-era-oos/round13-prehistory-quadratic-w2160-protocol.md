# Round 13：2020H1 独立区间 W2160 二次 Maker 紧迫度协议

生成日期：2026-07-23

## 目的与证据边界

Round 12 的 `W1440 + E2 + V1.50/N10/F20` 组合被淘汰：波动减仓实际触发，
但 `DEV_COST50`、`VAL_COST50` 与退出损失机制门槛没有全部通过。不得继续调整
该组合的 V/N/F、方向、亏损条件或冻结参数。

既有扩展开发证据仍显示两个互补事实：

- Round 5 的固定 `W2160` 线性 Maker wind-down 将退出损失改善到 `19.52%`，
  但 `VAL_BASE` 仍有一个门槛失败；
- Round 11 的固定 `W1440` 二次紧迫度在 Validation 四单元全部通过，并保留
  `97.36%` 配对收益，但退出损失改善只有 `7.93%`。

本轮只检验一个未运行过的组合假设：使用 Round 5 已冻结的 `W2160` 与 Round 11
已冻结的二次紧迫度 `E2`，让 Maker 去库存更早开始，并在接近终场时非线性加速。
不新增或搜索任何时长、指数、偏移、重挂间隔或比例。

为避免继续在已消费 Validation 上选择，本轮首先只使用一个新冻结、此前未进入本轮
策略设计的共同历史区间：

- BTCUSDT / ETHUSDT Binance USD-M 1m；
- 请求区间：`2020-01-01T00:00:00Z` 至 `2020-07-19T00:00:00Z`；
- 每个标的 `288000` 行，缺失率 `0`，重复行 `0`；
- `28` 个 BTC/ETH 成对 `READY` 周末/节假日窗口；
- 另外两个首尾边界窗口只因区间外观察/交易 Bar 不足而跳过；
- 在本协议锁定前只检查了哈希、行数、缺失、重复、归档段与窗口可用数量，未读取
  或计算该区间的收益、成交、回撤、Profit Factor 或机制结果。

当前跨周期 Final OOS 的 54 个窗口继续保持 `SEALED_NOT_EVALUATED`。即使本轮通过，
也只授权用冻结候选重跑已经消费的 Development/Validation 四单元；不得直接打开
Final OOS。

## 冻结来源

- Round 5 结果：`c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084`；
- Round 11 结果：`ab769bc64cb4e4b8fd3294bbeb02b8e673be1b3cd1033efc33695bf47c840f0a`；
- Round 12 结果：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`；
- 2020H1 BTC manifest：`995b32ad2693f785020838b0f5a907460e455ff64fc1a8e685c765ed6416c57d`；
- 2020H1 ETH manifest：`42ff31fa5189fd676324f4c2383ab42c1173320bda7205e4326bc7dde660647c`。

## 唯一候选

参考 ID：`EXT_W1440_LINEAR_E1`，只用于新独立区间基线和机制对照。

候选 ID：`EXT_W2160_QUADRATIC_E2`。

### 参考 Maker wind-down

```text
wind_down_bars = 1440
remaining_ratio = clamp(remaining_bars / 1440, 0, 1)
offset_steps = 1.10 * remaining_ratio
reprice_interval_bars = 5
unwind_fraction = 1.00
```

### 候选 Maker wind-down

```text
wind_down_bars = 2160
remaining_ratio = clamp(remaining_bars / 2160, 0, 1)
offset_steps = 1.10 * remaining_ratio ^ 2
reprice_interval_bars = 5
unwind_fraction = 1.00
```

候选不得启用利润保护、因果波动市场减仓、库存条件式提前 wind-down、按标的覆盖、
动态方向或其他新机制。

## 固定策略

- `direction_mode = NEUTRAL`；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、
  `stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、
  `stop_buffer_pct=0.02`；
- BTC 入口：`de0.40_ve1.05_rr0.35`；
- ETH 入口：`de0.35_ve1.05_rr0.55`；
- BTC/ETH 本金、库存上限、unpaired lot、reduce target 与
  `unpaired_lot_cap_enforcement=BAR_BOUNDARY` 保持 Round 12 不变；
- 成交概率、费用、滑点、资金费与 L0 保守撮合保持不变；
- 固定种子：`3, 10, 17, 31, 59, 97`。

## 两个独立验收单元

候选在全部 28 个成对 `READY` 窗口上分别评估：

1. `EXT_BASE`：BASE 成本；
2. `EXT_COST50`：COST50 成本。

每个单元必须同时通过以下 12 项：

1. 6/6 种子组合净收益为正；
2. BTC 与 ETH 六种子合计净收益均为正；
3. 六个种子的 Profit Factor 均大于 1；
4. 最大回撤不高于 5%；
5. 最大回撤不比同成本原始参考恶化超过 5%；
6. 最佳窗口集中度不高于 35%；
7. 最差 5% 窗口平均 PnL 不差于同成本原始参考；
8. 六种子平均净收益至少保留同成本原始参考的 75%；若参考不为正，候选必须为正；
9. 最差种子组合净收益为正；
10. BTC 至少保留原始参考已交易窗口的 25%；
11. ETH 至少保留原始参考已交易窗口的 25%；
12. 手续费/毛利润不高于原始参考的 1.25 倍。

任一单元失败即淘汰候选，不允许 BASE 与 COST50 互相抵消。

## 机制门槛

相对 `EXT_W1440_LINEAR_E1` 的 `EXT_COST50` 过滤后机制，候选必须同时满足：

1. `stop_exit_pnl` 亏损绝对值改善至少 20%；
2. `paired_grid_pnl` 至少保留 60%；
3. 候选实际产生 Maker wind-down 成交，证明机制执行。

## 执行完整性

1. 每个 worker 只能访问 2020H1 新冻结数据中的 28 个成对 `READY` 窗口；
2. 每个窗口必须同时覆盖 BTCUSDT 与 ETHUSDT；
3. 参考实际参数必须为 W1440、线性指数 1.0；
4. 候选实际参数必须为 W2160、二次指数 2.0；
5. 两者入口资格、网格参数、资本、库存规则与费用必须一致；
6. 利润保护与波动市场减仓必须保持关闭；
7. 当前跨周期 Development、Validation 和 Final OOS 窗口 ID 不得进入缓存、
   汇总或诊断输出；
8. 所有冻结结果与 manifest 哈希必须在运行前验证；
9. 生产默认配置不得修改。

## 停止条件

- 任一外部单元、机制门槛或执行完整性失败：记录 `NO_ROBUST_CANDIDATE`，不围绕
  2020H1 结果调整 W、指数、偏移或重挂间隔；
- 全部通过：只授权编写 Phase B 协议，用完全相同候选重跑已消费的跨周期
  Development/Validation 四单元；
- Phase B 任一门槛失败：记录 `NO_ROBUST_CANDIDATE`；
- 只有外部区间与 Phase B 全部通过，才允许先锁定精确 Final OOS 协议和候选哈希，
  然后一次性评估当前 54 个 Final OOS 窗口；
- 本协议及其结果不授权修改生产默认值，也不构成稳定收益声明。
