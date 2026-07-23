# Round 15：长期方向效率入场替代协议

协议日期：2026-07-23

## 动机与边界

Round 14 的固定 `W2160 + E2` 在 2018-03 至 2019 Spot 压力区间中表现为：BTC 两个成本单元全部通过，ETH 两个单元六种子全亏且最低覆盖率只有 `18.81%`。结合 2020H1 BTC 失败、CURRENT ETH Validation 失败，现有三小时入口过滤不能提供跨资产、跨年代的稳定状态识别。

本轮引入一个此前未用于 QuietGrid 候选选择的新因果状态：入场前 `1/3/7` 天的长期方向效率。它用于替代现有短窗 `directional_efficiency + volatility_expansion + reversal_ratio` 入口过滤，而不是叠加在其上。退出、Maker 紧迫度和资产范围不再调参。

旧 CURRENT Final OOS 继续保持封存；只有本协议 Phase A 出现完整合格候选时，才允许生成单独的 Final OOS 授权文件。

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
- 固定种子：`3, 10, 17, 31, 59, 97`；
- 成本情景：`BASE`、`COST50`。

## 长期方向效率

对每个标的、每个 window，在实际交易入口前最后一根已闭合观察 K 线处计算：

`long_de(L) = abs(sum(log_return[-L:]))) / max(sum(abs(log_return[-L:])), 1e-12)`

其中：

- `L ∈ {1440, 4320, 10080}`，分别代表 1、3、7 天；
- 只使用入口时已经闭合的分钟 K 线；
- lookback 内必须逐分钟连续；缺少历史或存在缺口时，该标的窗口直接阻断；
- 不使用窗口交易期内的任何价格或收益；
- 不读取 CURRENT Final OOS 的特征、window id 或结果。

## 候选集合

只注册以下 `3 × 4 = 12` 个候选：

- lookback：`1440 / 4320 / 10080`；
- CURRENT Development 内、按标的独立校准的经验分位数：`0.60 / 0.70 / 0.80 / 0.90`；
- 阈值使用向上经验分位数：排序后索引 `ceil(q * n) - 1`；
- BTC 与 ETH 使用相同 lookback 和分位数，但各自锁定自己的绝对 `long_de` 阈值；
- 当某标的 `long_de > threshold` 时，只阻断该标的，不联动阻断另一标的；
- 候选 ID：`LONG_DE_L{lookback}_Q{quantile}`。

禁止增加相邻 lookback、分位数、联合 veto、额外波动条件或资产特例。

## Phase A 授权数据

只允许使用以下已消费区间进行候选开发与排除：

1. CURRENT Development：108 个 window id；用于校准绝对阈值并参与门槛；
2. 已消费 CURRENT Validation：54 个 window id；
3. PREHISTORY 2020H1：28 个成对完整 window id；
4. Spot 2018-03 至 2019：101 个成对连续 window id。

Round 12 结果 SHA-256：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`。

Round 13 冻结结果 SHA-256：`1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3`；固定策略来源以该结果和执行指纹为准。

资产范围审计结果 SHA-256：`3d4c1df25da45f37e9661ae0797baecf4a9e799b42e397687d6eeeb62ac6ab27`。

Round 14 结果 SHA-256：`c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f`。

## Phase A 门槛

每个候选必须在四个区间、两个成本情景、BTC/ETH 两个标的组成的全部 `16` 个单元中分别满足：

- 6/6 种子总收益为正；
- 6/6 种子 Profit Factor 大于 1；
- 六种子最大回撤不高于 `5%`；
- 六种子最坏最佳窗口收益集中度不高于 `35%`；
- 六种子最低交易覆盖率不低于 `25%`。

此外：

- 每个数据集的 worker 缓存必须只覆盖授权 window id；
- 每个 window id 必须同时覆盖 BTC/ETH；
- 1/3/7 天特征计算不得读取交易期或 Final OOS；
- 缺失长期历史的窗口按阻断计入覆盖率分母，不能从样本中删除。

## 选择与 Final OOS 授权

- 无候选通过全部 16 个单元：记录 `NO_ROBUST_LONG_HORIZON_CANDIDATE`，不得围绕本特征继续调相邻阈值；
- 仅一个候选通过：锁定该候选；
- 多个候选通过：先最大化全部单元中的最差种子 PnL，再最大化最低覆盖率，再选择更长 lookback，最后选择更高分位数；
- 只有锁定候选后，才允许生成包含候选 ID、绝对 BTC/ETH 阈值、协议哈希、源码哈希和全部 Phase A 结果哈希的独立授权文件；
- 没有授权文件时，CURRENT Final OOS 必须保持 `SEALED_NOT_EVALUATED`；
- Final OOS 只允许执行一次，任何失败都记录 `NO_ROBUST_CANDIDATE`，不得据此回调参数。

无论 Phase A 结果如何，默认值均不自动修改，`stable_profit_claimed` 保持 `false`，直到独立 Final OOS 和后续多种子验收全部通过。
