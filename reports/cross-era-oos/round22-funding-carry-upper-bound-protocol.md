# Round 22：现货-永续 Funding Carry 乐观上界协议

协议日期：2026-07-23

## 研究目的

Round 20 的 BTC/ETH 相对价差均值回归候选失败，Round 21 的观察期相对动量方向上界因 CURRENT Development 路径回撤超过 `5%` 而被排除。继续使用价格方向预测无法满足严格风险门槛。

Round 21 结果 SHA-256：`e244d00bb3488868483f79ccee93c465f8d1972778a5307cd357df10d989b0f0`。

本轮切换到根本不同的收益来源：现货-永续 delta-neutral funding carry。只评估实际历史 funding rate 在理想对冲、未来已知方向和忽略基差风险时，是否足以覆盖双市场 Maker 往返成本。若这种乐观上界仍失败，则无需下载同期 Spot K 线或开发正式 carry 执行器。

## 冻结资金费数据

数据协议：`reports/cross-era-oos/round22-funding-archive-data-protocol.md`，SHA-256：`4ccabf8de9df47b0090f8506a2172141ee6d51a7f9b8cadc29c4a4c93bce4b3e`。

冻结 manifest：

- BTCUSDT：`data/backtests/round22_funding_carry/binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json`，SHA-256：`a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57`；
- ETHUSDT：`data/backtests/round22_funding_carry/binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json`，SHA-256：`19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f`。

每个 manifest 必须记录 65 个官方月度归档、5928 个事件、官方 checksum 全通过，并明确排除 `2023-07..2024-07`。禁止请求或读取被排除月份。

## 唯一反事实定义

BTCUSDT 与 ETHUSDT 分别独立评估，不允许用一个标的抵消另一个标的失败。

每个授权周末窗口固定执行一次 carry round trip：

1. BTC 总 gross capital 固定为 `500 USDT`；ETH 固定为 `300 USDT`；
2. 每个标的一半 gross capital 为 Spot 腿，一半为永续腿：
   - BTC 永续名义 `250 USDT`；
   - ETH 永续名义 `150 USDT`；
3. 汇总窗口持仓期间全部实际 funding rate：`funding_sum = sum(rate_i)`；
4. Oracle 事后选择唯一方向：
   - `funding_sum > 0`：long Spot、short 永续，收取正 funding；
   - `funding_sum < 0`：short Spot、long 永续，收取负 funding；
   - `funding_sum = 0` 时方向任意，但仍必须交易并支付费用；
5. Funding 收益固定为 `perpetual_notional * abs(funding_sum)`；
6. BASE 与 COST50 都假设 Spot 与永续双腿同步 Maker 成交：
   - BASE maker fee `0.0002`；
   - COST50 maker fee `0.0003`；
7. 入场和离场各交易一次完整 gross capital，费用为 `2 * gross_capital * maker_fee_rate`；
8. 每个窗口净收益为 `funding_income - round_trip_fees`；
9. 忽略 Spot/永续基差变化、借币利息、负 funding 方向的 Spot 做空可得性、保证金占用、强平风险、Maker 排队失败、腿间延迟、滑点和资金费预测误差；
10. Oracle 使用未来 funding_sum 决定方向，因此结果不可部署，只能用于排除 family。

禁止在看到结果后追加更高杠杆、单腿资本、只做正 funding、事件级翻转、不同持有时段、费率阈值、资产选择或月份过滤。

## 授权窗口

窗口均使用既有 NYSE 周末/长假定义与 `force_close_minutes=120`：

1. PREHISTORY：既有 2020H1 的 28 个成对完整 window id；
2. CURRENT Development：既有 108 个 Development window id；
3. CURRENT Validation Complete Months：从既有 54 个 Validation 中只保留 force-close 不晚于 `2023-07-01T00:00:00Z` 的 49 个窗口；明确排除最后 5 个跨入或位于 2023-07 的窗口；
4. POSTHISTORY：`2024-08-01T00:00:00Z` 至 `2026-07-01T00:00:00Z` 之间的 108 个周末/长假窗口。

`2023-07..2024-07` 的 funding archive 与既有 CURRENT Final OOS 均不得读取。POSTHISTORY 与既有 Final OOS 不重叠，并在其后开始。

## 完整性审计

正式运行必须验证：

- 数据协议、两个 funding manifest、两个 CSV 的哈希全部匹配；
- 每个 manifest 恰好包含两个授权月段、65 个 source archive、5928 个事件、0 个重复事件；
- 全部 source archive 的官方 checksum 已验证；
- manifest 与 CSV 不包含任何 `2023-07..2024-07` source month；
- funding_time 严格递增，rate 有限，interval hours 为正；
- 每个授权窗口至少包含一个 funding event；
- 每个事件只能归属一个授权窗口；
- PREHISTORY、CURRENT Development、CURRENT Validation 和 POSTHISTORY 之间不传递权益或方向状态；
- CURRENT Final OOS 状态继续为 `SEALED_NOT_EVALUATED`。

任何完整性失败都必须终止，不得生成 family 结论。

## 上界门槛

四个年代/拆分 × 两个成本情景 × BTC/ETH 两个标的，共 16 个 cell。每个 cell 必须分别满足：

- 总净收益严格为正；
- Profit Factor 大于 1；
- 最大回撤不高于 `5%`；
- 最佳盈利窗口占全部正收益比例不高于 `35%`；
- 净收益为正的窗口比例不低于 `25%`；
- funding event 覆盖率为 `100%`，且每个窗口完成一次理论 carry round trip。

不得用年代、成本情景或标的之间的收益相互抵消。

## 结论规则

- 16/16 cell 全部通过：记录 `FUNDING_CARRY_FAMILY_WORTH_PREREGISTRATION`，只允许随后冻结同期 Spot/永续价格与借币成本数据，并另写单一可部署候选协议；
- 任一 cell 失败：记录 `NO_PREREGISTERED_FUNDING_CARRY_CANDIDATE`，排除本协议定义的周末 funding carry 家族；
- 本轮不选择候选，不授权 Final OOS，不修改生产默认值；
- `direction_mode` 保持 `NEUTRAL`，`stable_profit_claimed` 保持 false。
