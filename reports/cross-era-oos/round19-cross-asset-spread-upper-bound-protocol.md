# Round 19：跨资产 β 中性价差反事实上界协议

协议日期：2026-07-23

## 研究动机

Round 17 的对称未配对 lot 纪律仅改变单标的网格内部的库存配对，仍在 BTCUSDT、ETHUSDT 各自独立运行；Round 18 的实际网格步长振荡容量 oracle 仅通过 `6/16` 个单元，且 CURRENT Development、PREHISTORY 和 Spot ETH 仍存在不可修复失败。继续搜索入口 lookback、阈值、cap、target 或资产特例会重复已失败的参数邻域。

本轮改为根本不同的经济结构：把 BTCUSDT 与 ETHUSDT 作为一个同步双腿头寸，直接交易观察期估计出的 β 中性相对价差。本轮只做不可部署、明显偏乐观的反事实上界，不注册生产候选；目的仅是判断跨资产相对价值家族是否具备足够的跨年代、费后经济容量。

冻结输入 Round 18 结果 SHA-256：`25a2b1d178a9b6072b3e864762b41c51f3d9f7c0f0a9566df88836cf08312818`。

## 运行前技术修订

首次完整性启动在生成任何收益结果前终止：CURRENT Development 的首个授权日历窗口 `nyse_20200717T200000Z` 早于冻结数据实际起点，BTC/ETH 均只有 `1800/3480` 根 K 线并被既有加载器标记为 `SKIPPED`。该启动没有生成结果 JSON 或报告，也没有计算任何 pair PnL。

本协议据此只做机械边界修订：保留 108 个 Development 授权 window id 作为覆盖率分母，允许并且只允许上述一个已冻结边界窗口被排除；实际评估 107 个完整 Development pair。Validation、PREHISTORY 和 Spot 的授权完整窗口数量不变。修订后重新冻结协议哈希，之后才允许正式计算收益。

## 固定反事实定义

每个完整授权周末窗口都固定使用以下规则，不允许搜索任何参数：

1. BTCUSDT 与 ETHUSDT 必须拥有完全相同的 `window_id`、分钟时间戳和连续 K 线数量；
2. 只用窗口最前面的固定 `180` 根观察 K 线估计 β：
   - 计算 BTC、ETH 相邻收盘价的对数收益；
   - `beta = Cov(BTC_return, ETH_return) / Var(BTC_return)`；
   - β 必须有限且严格为正；
3. 固定双腿总 gross notional 为 `800 USDT`，等于现有 BTC `500` 与 ETH `300` 资本之和；
4. 令 `q = 800 / (1 + beta)`，连续对数价差为：
   - `spread_t = log(ETH_t) - beta * log(BTC_t)`；
   - ETH 理论腿名义为 `q`；
   - BTC 理论腿名义为 `beta * q`；
5. Oracle 可以在观察期结束后的同一授权窗口内，事后选择任意一对满足 `entry_time < exit_time` 的分钟，并选择 long-spread 或 short-spread，使 `abs(spread_exit - spread_entry)` 最大；
6. 每个完整窗口必须执行且只能执行一次双腿 round trip，不允许跳过亏损窗口，不允许跨窗口持仓；
7. 理论 gross PnL 为 `q * abs(spread_exit - spread_entry)`；该连续对数合约并非可直接部署的固定数量现货/永续组合；
8. BASE 与 COST50 均假设双腿入场、双腿离场全部同步 Maker 成交，手续费按入场和离场各一次 gross notional 收取：
   - `fees = 2 * maker_fee_rate * 800`；
   - BASE maker fee 为 `0.0002`；
   - COST50 maker fee 为 `0.0003`；
9. 明确忽略排队失败、腿间延迟、再平衡换手、资金费、taker fee 和滑点；这些忽略只会抬高理论上界；
10. Oracle 使用未来路径决定进出点和方向，因此结果不可部署、不可用于稳定收益声明。

禁止在看到结果后追加其他观察 lookback、β 截断、动态资本、替代币对、多次往返、进出阈值或费率特例。

## Phase A 授权数据

只允许读取已经消费的区间：

1. CURRENT Development：108 个授权 window id，其中固定数据起点边界 skip 1 个，实际 107 个成对完整窗口；
2. 已消费 CURRENT Validation：54 个成对窗口；
3. PREHISTORY 2020H1：28 个成对完整窗口；
4. Spot 2018-03 至 2019：101 个成对连续窗口。

CURRENT 数据加载必须在 Validation 末端截断。禁止读取 CURRENT Final OOS 的 K 线、window id、β、价差路径、收益或任何统计量。

冻结依赖：

- Round 12 结果 SHA-256：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`；
- Round 13 结果 SHA-256：`1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3`；
- Round 14 结果 SHA-256：`c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f`；
- Round 18 结果 SHA-256：`25a2b1d178a9b6072b3e864762b41c51f3d9f7c0f0a9566df88836cf08312818`。

## 完整性审计

正式运行必须在生成结果前验证：

- 所有冻结结果和 manifest 哈希匹配；
- Round 18 结论为 `NO_PREREGISTERED_CYCLE_CAPACITY_CANDIDATE`，且没有选中候选；
- CURRENT Final OOS 状态仍为 `SEALED_NOT_EVALUATED`，授权状态为 false；
- CURRENT 授权窗口集合严格等于 108 个 Development 加 54 个 Validation；仅允许 `nyse_20200717T200000Z` 的 BTC/ETH 因固定数据起点边界处于 `SKIPPED`，其余 107 个 Development 和 54 个 Validation pair 必须为 `READY`；
- PREHISTORY 与 Spot 使用已冻结的成对完整窗口集合；
- 每个窗口的 BTC/ETH 行数、`open_time`、观察长度和 force-close 边界完全一致；
- β 只读取前 180 根观察 K 线；oracle 进出点只位于随后可交易 K 线内；
- 每个完整窗口恰好产生一个双腿 round trip；
- 不同数据集之间不传递 β、价差状态或权益状态。

任何完整性检查失败都必须终止，不得生成可选择结论。

## 上界门槛

本轮形成 8 个 pair cell：CURRENT Development、CURRENT Validation、PREHISTORY、Spot，分别乘 BASE 与 COST50。

每个 cell 都必须分别满足：

- 总净收益严格为正；
- Profit Factor 大于 1；若没有亏损窗口，则总净收益为正可视为通过；
- 按 `800 USDT` 初始权益计算的最大回撤不高于 `5%`；
- 最佳窗口占全部正收益的比例不高于 `35%`；
- 净收益为正的完整窗口数量除以全部授权 window id 的比例不低于 `25%`；
- 完整 pair 数据覆盖率不低于 `99%`；
- 全部完整窗口 β 均有效，且全部完整窗口都完成一次反事实交易。

不得用某个年代或成本情景的盈利抵消其他 cell 失败。

## 结论规则

- 8/8 cell 全部通过：记录 `CROSS_ASSET_SPREAD_FAMILY_WORTH_PREREGISTRATION`。这只允许另写一个完全前视隔离、可部署、单一候选的正式协议，不得直接运行 Final OOS；
- 任一 cell 失败：记录 `NO_PREREGISTERED_CROSS_ASSET_SPREAD_CANDIDATE`，并排除本协议定义的 BTC/ETH β 中性价差家族，不得围绕 β、lookback 或进出时点搜索邻近版本；
- 无论结果如何，`selected_candidate_id` 必须为 null，`final_oos_authorization_ready` 与 `final_oos_authorized` 必须为 false；
- `direction_mode` 与生产默认值保持 `NEUTRAL`，`production_defaults_changed` 为 false；
- `stable_profit_claimed` 保持 false。只有后续独立、可部署候选通过全部 Phase A、一次性 Final OOS 和多种子执行验收后，才允许讨论稳定收益。
