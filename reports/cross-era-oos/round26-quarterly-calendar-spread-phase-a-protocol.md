# Round 26：USD-M 季度期限价差单一因果候选协议

协议日期：2026-07-23

## 研究目的

Round 23 已排除周末永续 premium basis 收敛，Round 25 已排除 Binance–BitMEX 周末 funding spread。本轮切换到不同的合约结构：同一 Binance USD-M 市场内，交易永续与有固定交割日的季度合约之间的期限价差，并完整计入永续 funding。

本轮只有一个因果规则，不搜索 entry threshold、持有期、换约日、资产权重或退出点。季度合约的机械交割期限是该 family 的独立经济来源。

冻结前置结果：

- Round 25 结果 SHA-256：`5177f0137714cf26574da31a8ad1c3bc48776789f88edac46a37f943bb8c0eda`；
- Round 25 结论必须为 `NO_PREREGISTERED_CROSS_VENUE_FUNDING_SPREAD_CANDIDATE`；
- CURRENT Final OOS 必须为 `SEALED_NOT_EVALUATED`。

## 唯一交易规则

每个资产、每个冻结周窗口独立执行一次双腿 round trip：

1. 入场：窗口起点周五 `20:00 UTC` 的 1h Kline open；
2. 退出：恰好 168 小时后的周五 `20:00 UTC` 的 1h Kline open；
3. 合约：数据协议预先选择的最近、且交割时刻严格晚于退出时刻的季度合约；
4. 入场基差：`basis_entry = quarterly_open - perpetual_open`；
5. `basis_entry >= 0`：long 永续、short 季度；
6. `basis_entry < 0`：short 永续、long 季度；
7. `basis_entry = 0` 仍按第 5 条交易，不允许跳过；
8. BTC gross capital 为 `500 USDT`，ETH 为 `300 USDT`；
9. 两腿使用相同 base quantity：`qty = gross_capital / (perpetual_entry + quarterly_entry)`，使入场双腿名义总和严格等于 gross capital；
10. 不同资产、窗口、拆分和成本情景之间不传递头寸或权益状态。

## 价格与 Funding PnL

令 `position_sign = +1` 表示 long 永续/short 季度，`-1` 表示反向。任一小时 `t` 的价格价差 PnL：

`price_pnl_t = position_sign * qty * ((perpetual_t - perpetual_entry) - (quarterly_t - quarterly_entry))`

永续 funding 使用 Round 22 已冻结的实际 Binance USD-M funding 事件：

- 纳入 `window_start <= funding_time < window_end`；
- 官方 funding_time 可比整点晚 `0..47 ms`；固定令 `funding_hour = funding_time - funding_time % 3,600,000`，并使用该整点的永续 1h open 作为 funding notional 价格；
- 任一事件偏移达到 1 秒、两个事件映射到同一 funding_hour、或 funding_hour 无对应 K 线都必须终止；
- `funding_pnl_event = -position_sign * qty * perpetual_open_at_event * funding_rate`；
- 正 funding 时多头支付、空头收取；
- 所有 funding 事件按时间累加，不使用预测值。

首次正式评估在生成任何收益结果前因上述毫秒偏移与整点 K 线未直接相等而终止。两资产 5,928 个官方事件的最大偏移均为 47 ms，没有事件达到 1 秒。该规范化只确定同一小时价格引用，不改变事件归属、rate、方向、费用、窗口或持有期，并在任何 PnL 计算前冻结。

每个小时路径值为 `price_pnl_t + cumulative_funding_pnl - entry_fee`。退出时再扣除 exit fee；`minimum_path_pnl` 为入场扣费、169 个小时路径点和最终退出扣费后的最小值。

## 费用

两腿均按 USD-M Maker 成交：

- BASE：每次成交费率 `0.0002`；
- COST50：每次成交费率 `0.0003`；
- `entry_fee = maker_fee_rate * qty * (perpetual_entry + quarterly_entry)`；
- `exit_fee = maker_fee_rate * qty * (perpetual_exit + quarterly_exit)`；
- 不假设费率返佣或负手续费。

明确忽略 Maker 排队失败、腿间延迟、滑点、保证金分配、强平、ADL、交易所风险和季度合约流动性冲击。这些忽略只会抬高结果，因此本轮仍不能直接授权生产。

## 数据与隔离

- 只读取 Round 26 数据协议冻结的 206 个非交割周窗口；
- DEVELOPMENT 67、VALIDATION 48、POSTHISTORY 91；
- 初始 224 周中的 18 个季度交割周按统一生命周期规则排除，不进入收益分母；
- BTC/ETH 每窗口必须各有 169 个完全对齐的永续/季度小时点；
- 使用 Round 22 的 BTCUSDT、ETHUSDT funding manifest 与 CSV；
- 禁止读取 `2023-07-01..2024-08-01`；
- 禁止读取现有 robustness/spot_robustness 价格数据；
- 禁止读取 CURRENT Final OOS。

## 严格门槛

三个年代/拆分 × BASE/COST50 × BTC/ETH，共 12 个 cell。每个 cell 必须分别满足：

- 总净收益严格为正；
- Profit Factor 大于 1；无亏损窗口时以总净收益为正判定；
- 最大回撤不高于 `5%`；
- 最佳盈利窗口占全部正收益比例不高于 `35%`；
- 净收益为正的窗口比例不低于 `25%`；
- 价格窗口覆盖率为 `100%`，每窗口恰好 169 个小时点；
- funding 窗口覆盖率为 `100%`，每个窗口至少一个实际事件；
- 每个窗口完成一次两腿 round trip；
- 所有方向仅由入场基差符号决定。

不得用资产、年代或成本情景之间的收益相互抵消。

## 结论规则

- 12/12 全部通过：记录 `QUARTERLY_CALENDAR_SPREAD_WORTH_EXECUTION_PREREGISTRATION`；只允许随后冻结真实盘口滑点、Maker 成交率、保证金和季度流动性约束；
- 任一 cell 失败：记录 `NO_PREREGISTERED_QUARTERLY_CALENDAR_SPREAD_CANDIDATE`，排除本协议定义的固定一周 USD-M 永续/季度期限价差 family；
- 看到结果后禁止调整起点、持有期、换约、方向、费用、资本、资产或 funding 处理；
- 本轮不修改生产默认值，`direction_mode` 保持 `NEUTRAL`；
- `final_oos_authorized` 保持 false，`stable_profit_claimed` 保持 false。
