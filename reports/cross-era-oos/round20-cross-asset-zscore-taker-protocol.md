# Round 20：BTC/ETH β 中性 Z-score Taker 单一候选协议

协议日期：2026-07-23

## 前置证据与边界

Round 19 的不可部署反事实上界在 CURRENT Development、已消费 Validation、2020H1 PREHISTORY、2018-2019 Spot 及 BASE/COST50 共 `8/8` 个 pair cell 中通过，说明 BTC/ETH 相对价差在各年代均存在足以覆盖理想 Maker 成本的路径容量。Round 19 结果 SHA-256：`d90a031388032f8665c7af95055705d39fa98872dac7d858a7639c23738424f3`。

该上界使用未来路径、完美方向和完美进出，不能作为候选收益证据。本轮只注册一个完全前视隔离、确定性、同步 Taker 的候选，不搜索任何参数，不读取 CURRENT Final OOS。

候选 ID：`PAIR_Z2_STOP4_TAKER_V1`。

## 固定信号

每个完整授权周末窗口独立计算，不跨窗口传递状态：

1. 固定观察期为窗口最前面的 `180` 根已闭合 1m K 线；
2. 用观察期相邻收盘价对数收益估计：
   - `beta = Cov(BTC_return, ETH_return) / Var(BTC_return)`；
   - β 必须有限且严格为正；
3. 观察期每分钟价差为 `spread_t = log(ETH_t) - beta * log(BTC_t)`；
4. 固定使用观察期价差的均值与总体标准差计算后续 Z-score；标准差必须严格为正；
5. 从第一根可交易 K 线开始，只在该 K 线完全收盘后计算 Z-score：
   - `z <= -2.0`：产生 long-spread 信号，即 long ETH、short BTC；
   - `z >= +2.0`：产生 short-spread 信号，即 short ETH、long BTC；
6. 信号必须在下一根 K 线的 open 同步执行双腿；若没有下一根 K 线，则不得入场；
7. 每个窗口最多一次入场，离场后禁止重入；
8. 入场后，在每根已闭合 K 线检查：
   - long-spread 的 `z >= 0`，或 short-spread 的 `z <= 0`：均值回归离场；
   - `abs(z) >= 4.0`：风险止损离场；
9. 普通离场和止损均在信号后的下一根 K 线 open 同步执行双腿；
10. 若窗口结束前没有触发或没有下一根 K 线，必须在最后一根可交易 K 线 close 同步强制离场；
11. 每个窗口只使用自身观察期 β、均值和标准差；不使用未来窗口、其他数据集或任何收益反馈。

`2σ` 入场与 `4σ` 止损是本协议唯一固定结构。看到结果后禁止追加 `1.5/2.5σ`、`3/5σ`、动态阈值、滚动重估、二次入场或持有期变体。

## 固定头寸与执行模型

- 双腿总 gross notional 固定为 `800 USDT`；
- `q = 800 / (1 + beta)`；ETH 目标名义为 `q`，BTC 目标名义为 `beta * q`；
- 两腿数量在入场时冻结，持有期间不再平衡；
- `direction_mode` 与生产默认值保持 `NEUTRAL`；本候选的每次交易始终同时持有一条 long 腿和一条 short 腿，不转换生产网格为方向策略；
- BASE：taker fee `0.0005`，每次双腿执行均施加 `10 bps` 不利滑点；
- COST50：taker fee `0.00075`，每次双腿执行均施加 `20 bps` 不利滑点；
- long 腿买入价上浮、卖出价下调；short 腿卖出价下调、回补价上浮；
- 手续费按实际滑点后成交名义收取；
- 每跨过一个 UTC `00:00/08:00/16:00` 资金费结算点，额外扣除 `0.0001 * 800 USDT`；不假设 long/short 资金费相互抵消；
- 双腿假设同步成交，不模拟腿间延迟。该假设仍偏乐观，因此即使候选通过也需要后续独立执行不确定性验收；
- 不使用 Maker 成交概率，候选为确定性执行，不存在通过随机种子筛选候选的问题。

## Phase A 授权数据

只允许使用：

1. CURRENT Development：108 个授权 window id，其中固定数据起点边界 `nyse_20200717T200000Z` 为既知 skip，实际 107 个完整 pair；
2. 已消费 CURRENT Validation：54 个完整 pair；
3. PREHISTORY 2020H1：28 个成对完整 pair；
4. Spot 2018-03 至 2019：101 个成对连续 pair。

CURRENT 数据必须在 Validation 末端截断。54 个 CURRENT Final OOS 窗口继续保持 `SEALED_NOT_EVALUATED`，禁止读取其 K 线、window id、β、Z-score、交易或收益。

冻结输入：

- Round 12 结果 SHA-256：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`；
- Round 13 结果 SHA-256：`1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3`；
- Round 14 结果 SHA-256：`c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f`；
- Round 19 结果 SHA-256：`d90a031388032f8665c7af95055705d39fa98872dac7d858a7639c23738424f3`。

## 前视与执行完整性

正式运行必须验证：

- 全部冻结结果、协议依赖与 manifest 哈希匹配；
- Round 19 为 `8/8` 上界通过，且没有选中生产候选或授权 Final OOS；
- CURRENT Final OOS 仍封存；
- CURRENT 只允许既知起点边界 skip，其余窗口及 PREHISTORY/Spot pair 全部 READY；
- 每个 pair 的 BTC/ETH 行数、`open_time`、观察长度和窗口边界完全一致；
- β、价差均值和标准差只读取前 180 根观察 K 线；
- 入场与普通离场信号均使用已闭合 K 线，成交时间严格晚于信号时间；
- 每笔交易同时生成 BTC/ETH 两腿，gross notional 与 β 权重一致；
- 每个窗口最多一笔 pair trade，且所有已入场交易在同一窗口内平仓；
- 不同窗口、拆分和数据集之间不传递状态。

任何检查失败必须在候选结论生成前终止。

## Phase A 门槛

候选必须在 8 个 pair cell 中逐一满足：

- 总净收益严格为正；
- Profit Factor 大于 1；
- 最大回撤不高于 `5%`；
- 最佳盈利窗口占全部正收益的比例不高于 `35%`；
- 实际入场窗口数除以全部授权 window id 的交易覆盖率不低于 `25%`；
- 完整 pair 数据覆盖率不低于 `99%`；
- 不存在无效 β、无效价差标准差、未来信号、非因果成交、单腿成交或未平仓交易。

不得用某个年代、成本情景或组合总收益抵消其他 cell 失败。

## 选择与 Final OOS 规则

- 8/8 cell 全部通过：记录 `CROSS_ASSET_ZSCORE_CANDIDATE_READY_FOR_AUTHORIZATION`，选中候选 ID `PAIR_Z2_STOP4_TAKER_V1`；但仍不得直接读取或运行 Final OOS；
- 任一 cell 失败：记录 `NO_ROBUST_CROSS_ASSET_ZSCORE_CANDIDATE`，不得围绕 Z-score、止损、观察期、执行延迟或资本权重搜索相邻版本；
- 只有另行生成包含候选 ID、Round 20 协议哈希、源码哈希、结果哈希和全部 Phase A 门槛的独立授权文件后，才允许执行一次 CURRENT Final OOS；
- Final OOS 失败后不得据此回调参数；
- 即使 Final OOS 通过，也必须另做双腿延迟、成交偏差和执行故障的多情景验收后，才允许讨论生产切换；
- 本轮不修改生产默认值，`stable_profit_claimed` 始终为 false。
