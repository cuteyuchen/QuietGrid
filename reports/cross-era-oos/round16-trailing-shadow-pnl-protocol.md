# Round 16：滚动影子 PnL 适用性门禁协议

协议日期：2026-07-23

## 动机与边界

Round 15 的长期方向效率候选全部失败，最佳候选也只通过 `7/16` 个 Phase A 单元。所有候选共同无法修复以下结构性失效：

- CURRENT Development 的成本压力单元；
- 2020H1 PREHISTORY 的 BTC 单元；
- 2018-03 至 2019 Spot 的 ETH 单元。

这些失败呈现资产与年代轮换：固定策略在 2020H1 更适合 ETH，在更早 Spot 区间更适合 BTC，而 CURRENT Validation 又更适合 BTC。单一价格方向阈值不能表达“当前固定策略近期是否仍适用于该标的”。

本轮只测试一个此前未用于 QuietGrid 候选选择的新因果状态：对每个标的持续运行固定策略的纸面影子回测，并用目标窗口之前若干个已完成周末窗口的平均影子 PnL 决定是否允许当前窗口入场。

旧 CURRENT Final OOS 继续封存。Round 15 结果 SHA-256：`131dc847d60012a1dcdf5fc601d5e9a4918ca18e3ff1000fb5f75776f5443fc2`。

## 固定基础策略

- `direction_mode: NEUTRAL`；
- wind-down：`2160` bars；
- Maker 紧迫度指数：`2.0`；
- Maker 重挂间隔：`5` bars；
- 初始 Maker 偏移：`1.10` steps；
- unwind fraction：`1.0`；
- BTC/ETH 参数、资本与库存限制沿用 Round 13；
- 利润保护关闭；
- 波动减仓关闭；
- 移除旧三小时 BTC/ETH 入口过滤；
- 不使用 Round 15 长期方向效率过滤；
- 固定种子：`3, 10, 17, 31, 59, 97`；
- 成本情景：`BASE`、`COST50`。

## 因果滚动影子信号

对数据集、成本情景、标的分别计算，不跨数据集拼接历史。

对目标窗口 `t` 和标的 `s`：

1. 取同一数据集中严格早于 `t` 的最近 `K` 个成对授权 window id；
2. 对每个历史窗口，使用本协议固定的无入口过滤策略、相同成本情景和六个固定种子做影子回放；
3. 先对该历史窗口的六个种子 PnL 求均值，再对最近 `K` 个窗口求均值：

   `trailing_shadow_pnl(K) = mean_window(mean_seed(pnl))`

4. 当且仅当 `trailing_shadow_pnl(K) > 0` 时允许当前标的沿用基础策略结果；
5. 信号不够 `K` 个历史窗口、等于 0 或小于 0 时，当前标的窗口直接阻断；
6. 影子历史始终来自未应用本门禁的固定基础策略，禁止递归过滤；
7. 信号只读取目标入口前已完成窗口，不读取目标窗口交易期、后续窗口或 CURRENT Final OOS；
8. BASE 与 COST50 使用各自已知费用假设计算影子信号，实时部署时对应当前实际费用配置。

六种子平均仅用于降低纸面成交随机性；它是可在目标入口前从历史 K 线复算的固定模型输出，不使用真实未来成交。

## 候选集合

只注册以下三个自然时间尺度：

- `TRAIL_SHADOW_PNL_K4`：最近 4 个已完成周末窗口；
- `TRAIL_SHADOW_PNL_K8`：最近 8 个已完成周末窗口；
- `TRAIL_SHADOW_PNL_K13`：最近 13 个已完成周末窗口。

阈值固定为 `0`，禁止搜索正负邻近阈值、指数加权、资产特例、联合 top-1 选择或其他 lookback。

## Phase A 授权数据

只允许使用已消费区间：

1. CURRENT Development：108 个 window id；
2. 已消费 CURRENT Validation：54 个 window id；
3. PREHISTORY 2020H1：28 个成对完整 window id；
4. Spot 2018-03 至 2019：101 个成对连续 window id。

CURRENT Validation 的信号可以使用更早的 CURRENT Development 影子窗口；不得反向使用 Validation 生成 Development 信号。不同数据集之间不得传递状态。

冻结输入：

- Round 12 结果 SHA-256：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`；
- Round 13 结果 SHA-256：`1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3`；
- 资产范围审计 SHA-256：`3d4c1df25da45f37e9661ae0797baecf4a9e799b42e397687d6eeeb62ac6ab27`；
- Round 14 结果 SHA-256：`c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f`；
- Round 15 结果 SHA-256：`131dc847d60012a1dcdf5fc601d5e9a4918ca18e3ff1000fb5f75776f5443fc2`。

## Phase A 门槛

每个候选必须在四个区间、两个成本情景、BTC/ETH 两个标的组成的全部 `16` 个单元中分别满足：

- 6/6 种子总收益为正；
- 6/6 种子 Profit Factor 大于 1；
- 六种子最大回撤不高于 `5%`；
- 六种子最坏最佳窗口收益集中度不高于 `35%`；
- 六种子最低交易覆盖率不低于 `25%`。

此外：

- 每个数据集 worker 缓存只能覆盖授权 window id；
- 每个授权 window id 必须同时覆盖 BTC/ETH；
- 影子信号每个历史窗口必须恰好聚合六个固定种子；
- 目标窗口不得出现在自身信号历史中；
- 历史不足导致的阻断必须计入覆盖率分母，不能删除样本。

## 选择与 Final OOS 授权

- 无候选通过全部 16 个单元：记录 `NO_ROBUST_TRAILING_SHADOW_CANDIDATE`，不得围绕本信号继续调邻近窗口或阈值；
- 仅一个候选通过：锁定该候选；
- 多个候选通过：先最大化全部单元中的最差种子 PnL，再最大化最低覆盖率，再选择更长 lookback；
- 锁定候选后，只允许先生成包含候选、协议哈希、源码哈希和全部 Phase A 结果哈希的独立授权文件；
- 没有授权文件时，CURRENT Final OOS 必须保持 `SEALED_NOT_EVALUATED`；
- Final OOS 只允许执行一次，失败后不得据此回调参数。

无论 Phase A 结果如何，生产默认值均不自动修改，`stable_profit_claimed` 保持 `false`，直到独立 Final OOS 和后续多种子验收全部通过。
