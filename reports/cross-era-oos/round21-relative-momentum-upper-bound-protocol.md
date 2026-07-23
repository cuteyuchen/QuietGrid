# Round 21：BTC/ETH 观察期相对动量方向上界协议

协议日期：2026-07-23

## 动机与前置证据

Round 20 的唯一注册均值回归候选 `PAIR_Z2_STOP4_TAKER_V1` 在全部 `8/8` 个 Phase A pair cell 中失败。BASE 情景的策略毛收益已经为负，且所有 `STOP_Z4` 交易均亏损，说明失败不只是手续费问题：观察期价差偏离后经常继续扩张，固定均值回归方向与实际路径不匹配。

Round 20 结果 SHA-256：`d57b021867c59f290ca68d8c79250bafc23e8efd62a3e38d280ff8a247afc63b`。

本轮测试结构相反、且不使用 Z-score 阈值的观察期相对动量方向。为了避免再次消耗正式 Phase A 候选，本轮仍是不可部署乐观上界：交易方向完全由观察期决定，只有离场时点使用未来 oracle。若该上界仍失败，则直接排除本协议定义的相对动量家族。

## 固定方向与上界定义

每个完整授权窗口独立执行以下唯一规则：

1. 固定使用前 `180` 根已闭合 1m K 线；
2. 用观察期相邻收盘价对数收益计算：
   - `beta = Cov(BTC_return, ETH_return) / Var(BTC_return)`；
   - β 必须有限且严格为正；
3. 观察期相对价差为 `spread_t = log(ETH_t) - beta * log(BTC_t)`；
4. 观察期相对动量固定为 `momentum = spread_179 - spread_0`；
5. `momentum > 0`：固定选择 long-spread，即 long ETH、short BTC；
6. `momentum < 0`：固定选择 short-spread，即 short ETH、long BTC；
7. momentum 等于零或非有限值视为完整性失败，不允许用阈值跳过；
8. 双腿总 gross notional 固定为 `800 USDT`，`q = 800 / (1 + beta)`；ETH 理论腿名义为 `q`，BTC 理论腿名义为 `beta * q`；
9. 固定在观察期最后一根 K 线 close 建立连续对数合约理论头寸；方向完全由当时已知观察数据决定；
10. Oracle 只允许在随后可交易 K 线的 close 中事后选择一个离场点，使已固定方向的收益最大；不得改变方向、不得延后入场、不得跳过亏损窗口、不得多次交易；
11. 理论 gross PnL 为：
    - long-spread：`q * (spread_exit - spread_entry)`；
    - short-spread：`q * (spread_entry - spread_exit)`；
12. BASE 与 COST50 均假设入场、离场双腿同步 Maker 成交：
    - BASE maker fee `0.0002`；
    - COST50 maker fee `0.0003`；
    - `fees = 2 * maker_fee_rate * 800`；
13. 忽略 Maker 排队失败、腿间延迟、再平衡换手、资金费、taker fee 和滑点；这些假设只会抬高上界；
14. Oracle 离场使用未来路径，因此结果不可部署，也不能用于稳定收益声明。

本轮没有 entry Z-score、止损、持有期、平滑窗口或动量阈值。看到结果后禁止追加其他 observation lookback、momentum threshold、β 截断、方向确认、多次入场或资产权重变体。

## Phase A 授权数据

仅允许读取：

1. CURRENT Development：108 个授权 window id，固定起点边界 skip 1 个，实际 107 个完整 pair；
2. 已消费 CURRENT Validation：54 个完整 pair；
3. PREHISTORY 2020H1：28 个完整 pair；
4. Spot 2018-03 至 2019：101 个完整 pair。

CURRENT 必须在 Validation 末端截断。54 个 CURRENT Final OOS 窗口继续保持 `SEALED_NOT_EVALUATED`，禁止读取其任何数据或统计量。

冻结输入：

- Round 12 结果 SHA-256：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`；
- Round 13 结果 SHA-256：`1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3`；
- Round 14 结果 SHA-256：`c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f`；
- Round 20 结果 SHA-256：`d57b021867c59f290ca68d8c79250bafc23e8efd62a3e38d280ff8a247afc63b`。

## 完整性审计

正式运行必须验证：

- 全部冻结结果、manifest 和依赖源码哈希匹配；
- Round 20 为 `NO_ROBUST_CROSS_ASSET_ZSCORE_CANDIDATE`，没有候选或 Final OOS 授权；
- CURRENT Final OOS 仍封存；
- CURRENT 只允许既知数据起点边界 skip，其余 pair 及 PREHISTORY/Spot pair 全部 READY；
- 每个 pair 的 BTC/ETH 分钟时间戳、行数、观察长度和窗口边界完全一致；
- β 和 momentum 只读取前 180 根观察 K 线；
- 每个完整窗口方向在观察结束时唯一确定，oracle 只能选择更晚的离场点；
- 每个完整窗口恰好产生一个双腿理论 round trip；
- 不同窗口、拆分和数据集之间不传递状态。

任何检查失败必须终止，不得生成家族结论。

## 上界门槛

本轮形成 CURRENT Development、CURRENT Validation、PREHISTORY、Spot 乘 BASE/COST50 共 8 个 pair cell。每个 cell 必须分别满足：

- 总净收益严格为正；
- Profit Factor 大于 1；
- 最大回撤不高于 `5%`；
- 最佳盈利窗口占全部正收益的比例不高于 `35%`；
- 净收益为正的完整窗口数量除以全部授权 window id 的比例不低于 `25%`；
- 完整 pair 数据覆盖率不低于 `99%`；
- 全部窗口 β 与 momentum 有效，全部方向只由观察期确定，全部窗口完成一次理论交易。

不得用某个年代或成本情景抵消其他 cell 失败。

## 结论规则

- 8/8 cell 全部通过：记录 `RELATIVE_MOMENTUM_FAMILY_WORTH_PREREGISTRATION`，只允许另写一个无未来信息、单一退出规则、真实执行成本的正式候选协议；
- 任一 cell 失败：记录 `NO_PREREGISTERED_RELATIVE_MOMENTUM_CANDIDATE`，排除本协议定义的观察期相对动量家族；
- 无论结果如何，本轮 `selected_candidate_id` 必须为 null，`final_oos_authorization_ready` 与 `final_oos_authorized` 必须为 false；
- 不修改生产默认值，`direction_mode` 保持 `NEUTRAL`，`stable_profit_claimed` 保持 false。
