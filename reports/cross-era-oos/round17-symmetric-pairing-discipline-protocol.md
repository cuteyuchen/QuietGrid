# Round 17：对称配对纪律跨周期协议

协议日期：2026-07-23

## 动机与边界

Round 16 的滚动影子 PnL 门禁在 Phase A 中最多只通过 `6/16` 个单元，并在 PREHISTORY BTC 上近乎停机。继续调整入口信号会增加滞后与覆盖率损失，不能修复固定网格在成本压力下的配对收益不足和单边库存累积。

本轮不增加任何入口预测状态，只测试一个核心执行结构：BTC/ETH 使用相同的未配对库存上限和相同的减仓目标步长。目标是让每次新增库存都受配对纪律约束，同时让完成配对时保留足够毛网格收益覆盖费用。

旧 CURRENT Final OOS 继续封存。Round 16 结果 SHA-256：`990ee916758de7f89cf6b7d6801d3887dbcce43c35764c48dde07994f9714f9d`。

## 旧诊断的有限用途

以下旧 CURRENT-only 十种子诊断只用于提出候选，不构成跨周期通过证据：

- BTC target `0.50`：`96b3fa688eec5fad6fa7cf2a623232235cb86eb6fc9d470e3efaeceeb68e403d`；
- BTC target `0.75`：`2b5ca2e70353d6e21eeefa7200765a2fae795e532f6d284f2aa5a026206f55b5`；
- ETH target `0.50`：`87863c7dbd3daf4e85bac23c61898fbba44b360476b0aebb0577c4474732001b`；
- ETH target `0.75`：`8267a8c16d5e039490daf583d43b9b690e72bdf5576544dfa9d77c0c0a016e8a`。

这些诊断使用较早的 CURRENT 配置，未覆盖 2020H1 和 2018-2019 Spot；不得据此宣称候选稳定。

## 唯一注册候选

候选 ID：`SYMMETRIC_PAIR_CAP1_TARGET075`。

固定策略：

- `direction_mode: NEUTRAL`；
- wind-down：`2160` bars；
- Maker 紧迫度指数：`2.0`；
- Maker 重挂间隔：`5` bars；
- 初始 Maker 偏移：`1.10` steps；
- unwind fraction：`1.0`；
- BTC 参数：`range_multiplier=1.25`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- ETH 参数：`range_multiplier=1.00`、`min_step_pct=0.0018`、`stop_buffer_pct=0.02`；
- BTC 资本 `500 USDT`、库存名义上限 `200 USDT`；
- ETH 资本 `300 USDT`、库存名义上限 `120 USDT`；
- BTC/ETH 均设置 `max_unpaired_lots_per_side=1`；
- BTC/ETH 均设置 `reduce_target_step_fraction=0.75`；
- `unpaired_lot_cap_enforcement=BAR_BOUNDARY`；
- 移除所有旧短窗、长期方向效率和滚动影子 PnL 入口过滤；
- 利润保护关闭；
- 波动减仓关闭；
- 固定种子：`3, 10, 17, 31, 59, 97`；
- 成本情景：`BASE`、`COST50`。

本轮只有这一个可选择候选。禁止追加 cap `2/3`、target `0.625/0.875/1.0`、资产专属 target、入口 veto 或其他联合变体。

## Phase A 授权数据

只允许使用已消费区间：

1. CURRENT Development：108 个 window id；
2. 已消费 CURRENT Validation：54 个 window id；
3. PREHISTORY 2020H1：28 个成对完整 window id；
4. Spot 2018-03 至 2019：101 个成对连续 window id。

冻结输入：

- Round 12 结果 SHA-256：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`；
- Round 13 结果 SHA-256：`1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3`；
- 资产范围审计 SHA-256：`3d4c1df25da45f37e9661ae0797baecf4a9e799b42e397687d6eeeb62ac6ab27`；
- Round 14 结果 SHA-256：`c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f`；
- Round 15 结果 SHA-256：`131dc847d60012a1dcdf5fc601d5e9a4918ca18e3ff1000fb5f75776f5443fc2`；
- Round 16 结果 SHA-256：`990ee916758de7f89cf6b7d6801d3887dbcce43c35764c48dde07994f9714f9d`。

CURRENT 数据加载必须在 Validation 末端截断，禁止读取 Final OOS 的 K 线、特征、window id 或收益。不同数据集之间不得传递状态。

## 执行完整性

正式执行必须审计每个 worker 的缓存键，并同时满足：

- 只包含授权 window id；
- 每个 window id 同时包含 BTCUSDT 和 ETHUSDT；
- 每个缓存执行均为 wind-down `2160`、Maker 指数 `2.0`、重挂 `5`、偏移 `1.10`、unwind `1.0`；
- BTC/ETH 的未配对 lot 上限均为 `1`；
- BTC/ETH 的减仓目标比例均为 `0.75`；
- BTC 库存名义上限为 `200`，ETH 为 `120`；
- 费用、滑点和 seed 与当前任务完全一致；
- 利润保护与波动减仓均关闭；
- 所有 SymbolResearchPolicy 的 `entry_filter` 均为 `None`。

任何完整性检查失败都必须在生成候选结果前终止。

## Phase A 门槛

候选必须在四个区间、两个成本情景、BTC/ETH 两个标的组成的全部 `16` 个单元中分别满足：

- 6/6 种子总收益为正；
- 6/6 种子 Profit Factor 大于 1；
- 六种子最大回撤不高于 `5%`；
- 六种子最坏最佳窗口收益集中度不高于 `35%`；
- 六种子最低交易覆盖率不低于 `25%`。

不得用组合净收益抵消任一标的或任一单元失败。

## 选择与 Final OOS 授权

- 候选未通过全部 16 个单元：记录 `NO_ROBUST_SYMMETRIC_PAIRING_CANDIDATE`，不得围绕 cap 或 target 搜索邻近值；
- 候选通过全部 16 个单元：记录 `SYMMETRIC_PAIRING_CANDIDATE_READY_FOR_AUTHORIZATION`，但仍不得直接运行 Final OOS；
- 只有另行生成包含候选 ID、协议哈希、源码哈希和全部 Phase A 结果哈希的独立授权文件后，才可执行一次 CURRENT Final OOS；
- Final OOS 失败后不得据此回调参数。

无论 Phase A 结果如何，生产默认值均不自动修改，`stable_profit_claimed` 保持 `false`，直到独立 Final OOS 和后续多种子验收全部通过。
