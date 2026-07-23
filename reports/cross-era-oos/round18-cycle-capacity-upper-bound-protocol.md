# Round 18 前置评估：网格步长振荡容量乐观上界协议

协议日期：2026-07-23

## 目的与边界

Round 17 的对称配对纪律只通过 `4/16` 个 Phase A 单元，证明继续调整 `max_unpaired_lots_per_side` 或 `reduce_target_step_fraction` 没有资格进入下一轮。Round 17 结果 SHA-256：`9dfff4caaa30cc78e47c350c2f45d12183f5bb223353c86193bc56f5c4faa969`。

本评估不注册可部署候选，也不选择生产阈值。它只检验一个新因果特征家族是否存在理论上的跨周期可行性：入场前价格是否完成过足够多、幅度至少达到本窗口实际网格步长的往返振荡。

评估故意允许每个历史单元使用事后最有利阈值，是不可部署、明显偏乐观的上界。如果这种上界仍不能通过全部 16 个单元，则该特征家族直接排除，不允许围绕 lookback、阈值或资产特例继续搜索。

旧 CURRENT Final OOS 必须保持封存；本评估不得读取其 K 线、特征、window id 或收益。

## 与旧特征的区别

旧 `reversal_ratio` 只统计最近 15 个一分钟收益的符号翻转比例，不考虑翻转幅度是否足以触及一个真实网格层级。

本评估只确认幅度达到实际 `GridParams.step_pct` 的反向摆动，并统计完整往返。小幅噪声不会增加容量；单边趋势即使分钟收益频繁变号，也不会自动形成大量完整网格循环。

## 固定基础策略

使用 Round 16 的未过滤固定基础策略：

- `direction_mode: NEUTRAL`；
- wind-down：`2160` bars；
- Maker 紧迫度指数：`2.0`；
- Maker 重挂间隔：`5` bars；
- 初始 Maker 偏移：`1.10` steps；
- unwind fraction：`1.0`；
- BTC：库存名义上限 `200`、每侧未配对 lot 上限 `1`、减仓目标 `0.50`；
- ETH：库存名义上限 `120`、未配对 lot 不设上限、减仓目标 `1.00`；
- `unpaired_lot_cap_enforcement=BAR_BOUNDARY`；
- 移除旧短窗、长期方向效率、滚动影子 PnL 和 Round 17 配对变体；
- 利润保护关闭；
- 波动减仓关闭；
- 固定种子：`3, 10, 17, 31, 59, 97`；
- 成本情景：`BASE`、`COST50`。

## 因果特征定义

对每个标的、每个目标窗口和每个 lookback：

1. 只读取目标窗口实际交易入口前已经闭合且逐分钟连续的 close；
2. lookback 固定为 `180 / 720 / 1440` 个一分钟区间，因此分别需要 `181 / 721 / 1441` 个连续 close；
3. 使用该窗口入场时已经确定的实际 `step_pct`，令 `h = log(1 + step_pct)`；
4. 在 log close 序列上运行固定 zig-zag：持续更新当前方向的局部极值；当价格相对该极值反向移动至少 `h` 时，确认一条 step-sized reversal leg，并切换跟踪方向；
5. `completed_cycles = floor(confirmed_reversal_legs / 2)`；
6. 对成本情景计算 `cycle_capacity = completed_cycles * max(step_pct - 2 * maker_fee_rate, 0)`；
7. 历史不足、存在分钟缺口、step 缺失或非正时，容量记为不可用；若基础结果原本为 `TRADED`，应用容量门槛时必须阻断并计入覆盖率分母。

同一窗口的 `step_pct` 必须在全部六个种子中一致。特征不得使用目标交易期、强制平仓结果、PnL 或后续窗口。

## 乐观 oracle 规则

对四个区间、两个成本情景和两个标的形成的每个单元分别执行：

1. 对每个 lookback 独立生成候选阈值：`0` 加上该单元所有可用 `cycle_capacity` 唯一值；
2. 当且仅当容量 `>= threshold` 时保留该窗口的固定基础策略结果；
3. 阈值选择可以读取该单元全部六种子的历史结果，这是故意不可部署的事后 oracle；
4. 每个阈值仍必须满足完整 Phase A 门槛，不能删除被阻断窗口；
5. 单元 oracle 先选择通过全部检查的阈值；若有多个，最大化最差种子 PnL，再最大化最低覆盖率，再选择更长 lookback、更高阈值；
6. 若没有通过阈值，仍记录最差种子 PnL 最大的失败上界，便于确认失败原因。

oracle 可以为不同数据集、成本情景、标的选择完全不同的 lookback 和阈值，因此结果只能作为特征家族的乐观上界，不能作为候选或授权文件。

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
- Round 16 结果 SHA-256：`990ee916758de7f89cf6b7d6801d3887dbcce43c35764c48dde07994f9714f9d`；
- Round 17 结果 SHA-256：`9dfff4caaa30cc78e47c350c2f45d12183f5bb223353c86193bc56f5c4faa969`。

## 执行完整性

每个 worker 缓存必须满足：

- 只包含授权 window id，且每个 id 同时覆盖 BTC/ETH；
- 固定 W2160、E2、Maker 重挂/偏移/unwind 参数；
- BTC 使用 cap `1`、target `0.50`，ETH 使用 cap `0`、target `1.00`；
- 所有入口过滤均为 `None`；
- 费用、滑点和 seed 与任务一致；
- 利润保护与波动减仓关闭。

特征审计必须记录每个目标窗口的入口时间、最大读取时间、连续历史可用性以及任何自引用计数；自引用计数必须为 0。

## Phase A 门槛

每个 oracle 单元仍必须分别满足：

- 6/6 种子总收益为正；
- 6/6 种子 Profit Factor 大于 1；
- 六种子最大回撤不高于 `5%`；
- 六种子最坏最佳窗口收益集中度不高于 `35%`；
- 六种子最低交易覆盖率不低于 `25%`。

## 判定

- 任一单元在全部 lookback 和全部 oracle 阈值下仍无通过解：记录 `NO_PREREGISTERED_CYCLE_CAPACITY_CANDIDATE`，排除本特征家族；
- 全部 16 个单元均存在 oracle 通过解：记录 `CYCLE_CAPACITY_FAMILY_WORTH_PREREGISTRATION`，但不得直接选择阈值、修改默认值或运行 Final OOS；
- 只有上界通过后，才允许另写正式 Round 18 协议，并且阈值只能在 CURRENT Development 中校准，再由其他已消费区间裁决。

无论结果如何，`final_oos_status` 保持 `SEALED_NOT_EVALUATED`，`stable_profit_claimed=false`，生产默认值不变。
