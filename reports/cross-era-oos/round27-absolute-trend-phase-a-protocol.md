# Round 27：BTC/ETH SMA50/200 绝对趋势单一候选协议

协议日期：2026-07-23

## 唯一候选

候选 ID：`ABS_TREND_SMA50_200_1X_V1`。

该规则使用经典日线 SMA50/200，不搜索其他均线、确认天数、波动缩放、止损、资产权重或杠杆。

前置结果：

- Round 26 结果 SHA-256：`171598de0aa94607f1b05ea6bfd4f79f3c58252b0eb39e3b3c2290cbe78965cf`；
- 结论必须为 `NO_PREREGISTERED_QUARTERLY_CALENDAR_SPREAD_CANDIDATE`；
- CURRENT Final OOS 必须保持 `SEALED_NOT_EVALUATED`。

## 因果信号

1. UTC 日 close 定义为当日 `23:00` Kline close；
2. 对评估日 `D`，只使用 `D-200..D-1` 的 200 个已完成 UTC 日 close；
3. `SMA50` 为最近 50 日 close 均值，`SMA200` 为最近 200 日 close 均值；
4. `SMA50 >= SMA200`：目标仓位为 long 永续；
5. `SMA50 < SMA200`：目标仓位为 short 永续；
6. 每日只在 `01:00 UTC` 检查并执行信号；信号未变化时不交易；
7. 不使用当日 `01:00` 之后的价格、funding 或未来数据；
8. 不允许空仓、阈值、延迟确认或双资产联动。

## 拆分与状态隔离

- DEVELOPMENT：2021-02-06 `01:00 UTC` 开始，2022-06-30 `23:00` close 强制退出；510 个信号日；
- VALIDATION：2022-07-01 `01:00 UTC` 独立重置，2023-06-30 `23:00` close 强制退出；365 个信号日；
- POSTHISTORY：2024-08-01 起独立累积 200 日 warmup，2025-02-17 `01:00 UTC` 首次交易，2026-06-30 `23:00` close 强制退出；499 个信号日；
- 三个拆分、BTC/ETH 和成本情景之间的权益、数量与头寸完全重置；
- VALIDATION 只可使用此前已消费 HISTORY close 作 warmup；POSTHISTORY 不得跨隔离区间取 warmup。

## 仓位、价格与 Funding

- 初始权益/固定目标名义：BTC `500 USDT`，ETH `300 USDT`；
- 杠杆：`1x`；
- 每次开仓数量：`qty = initial_capital / execution_price`；
- 信号翻转时先平旧仓、再按固定目标名义开反向仓；
- 不随盈利扩大目标名义，不跨拆分复利；
- 小时路径按 1h open 标记；最终退出使用最后一根 `23:00` Kline close；
- Binance funding 纳入持仓期间全部实际事件；正 funding 时多头支付、空头收取；
- 官方 funding_time 向下规范化到同一整点，偏移必须小于 1 秒且不得碰撞；funding notional 使用该整点的 1h open；
- `00:00` funding 在次日 `01:00` 信号切换前结算到旧仓；首次建仓前的 funding 不计入。

## 执行成本

信号交易按主动成交处理，费用与滑点合并为每一侧成交名义的固定成本：

- BASE：taker fee `0.0005` + slippage `5 bps`，合计 `0.0010`；
- COST50：taker fee `0.00075` + slippage `10 bps`，合计 `0.00175`；
- 翻转包含平仓与新开仓两次单侧成本；
- 最终强平只收一次平仓成本。

忽略强平、ADL、API 延迟和极端跳空超出小时 OHLC 的影响；1x 与保守主动成本只用于 Phase A，不能直接声明生产稳定性。

## 12 个严格单元

DEVELOPMENT、VALIDATION、POSTHISTORY × BASE/COST50 × BTC/ETH，共 12 个 cell。每个 cell 必须分别满足：

- 总净收益严格为正；
- 基于日度权益变化的 Profit Factor 大于 1；
- 最大回撤不高于 `20%`；
- 日度年化 Sharpe 大于 `0.5`；
- 正收益日历月比例不低于 `50%`；
- 最佳盈利月占全部正收益比例不高于 `35%`；
- 小时价格覆盖率与 funding 映射覆盖率均为 `100%`；
- 所有信号只使用已完成的 50/200 日 close；
- 所有开平仓均计入冻结成本，最终无遗留仓位。

不得用资产、年代或成本情景间收益互相抵消。

## 结论

- 12/12 全部通过：记录 `ABSOLUTE_TREND_WORTH_EXECUTION_PREREGISTRATION`，随后只允许冻结真实盘口冲击、交易所最小数量和逐笔执行；
- 任一 cell 失败：记录 `NO_PREREGISTERED_ABSOLUTE_TREND_CANDIDATE`，排除本协议定义的 SMA50/200、1x、每日 01:00 执行 family；
- 看到结果后禁止增加相邻均线、波动缩放、止损或资产特例；
- 不修改生产默认值，`direction_mode` 保持 `NEUTRAL`；
- `final_oos_authorized = false`，`stable_profit_claimed = false`。
