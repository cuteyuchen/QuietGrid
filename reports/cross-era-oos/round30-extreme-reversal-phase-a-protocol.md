# Round 30：短周期价格极端反转单一候选协议

协议日期：2026-07-24

## 研究定位

Round 27 已排除每日 SMA50/200 绝对趋势，Round 29 已排除主动买量不平衡。本轮切换到未单独评估的价格形态收益来源：在因果的短周期大幅波动后做固定持有期反转。信号只使用已完成 Kline 的 OHLC，不使用主动买量、跨资产信息、premium、季度合约或未来 funding 方向；真实 funding 只计入持仓 PnL。

## 数据与隔离

1. 价格输入复用 Round 29 已冻结的 Binance USD-M 1h Kline manifest/CSV，读取字段仅为 `open_time/open/close`；其官方 archive checksum、行级 source SHA、小时连续性和数据审计必须重新验证。
2. funding 输入复用 Round 22 已冻结的 Binance USD-M funding manifest/CSV，并重新验证 manifest、CSV SHA 和事件映射。
3. 只使用 `DEVELOPMENT`、`VALIDATION`、`POSTHISTORY` 三个授权段：
   - DEVELOPMENT：2021-02-06 01:00 UTC 至 2022-06-30 23:00 UTC；
   - VALIDATION：2022-07-01 01:00 UTC 至 2023-06-30 23:00 UTC；
   - POSTHISTORY：2025-02-17 01:00 UTC 至 2026-06-30 23:00 UTC。
4. 2023-07 至 2024-07 隔离区间、CURRENT Final OOS 和既有 robustness/spot_robustness 结果不参与选参或收益计算。

## Development-only 选择规则

在查看 Validation、Posthistory 或任何封存结果前，固定筛选集合为：

- 方向：`CONTRARIAN`、`MOMENTUM`；
- 回看长度：`4/8/12/24/48` 个完整小时；
- 固定持有：`4/8/12/24/48` 个小时；
- 极端阈值：`0.5%/1%/1.5%/2%/3%`。

每个候选只在 DEVELOPMENT 的 COST50 情景中选择：必须先在 BTC、ETH 上均有严格正净收益，再按以下字典序排序：

1. 两资产中较小的 COST50 净收益，降序；
2. 两资产中较小的日年化 Sharpe，降序；
3. 两资产中较小的正收益月比例，降序；
4. 候选 ID，字典序升序。

本规则选出的唯一候选为：`EXTREME_REVERSAL_12H_3PCT_HOLD48_1X_V1`。

## 因果信号与执行

对每个小时 `t` 的 `1h open`：

1. 使用 `t-13` 至 `t-1` 的 13 个已完成收盘价，计算跨 12 个小时的收益 `R_t = close[t-1] / close[t-13] - 1`；
2. `R_t >= +3%` 且当前空仓时，在 `t` 的 open 建立 1x 空头；`R_t <= -3%` 且当前空仓时，在 `t` 的 open 建立 1x 多头；否则不入场；
3. 每笔仓位固定名义为该 cell 初始资本，持有恰好 48 小时，在 `t+48` 的 open 平仓；若到达 cell 末端，则在最后一根 Kline 的 close 强平；
4. 持仓期间忽略新信号，不重叠、不加仓、不反向翻仓；平仓后的同一小时不重新入场；
5. 正 funding 多头支付、空头收取，按真实 funding event 与该小时 open 计入；不使用未来 funding 选择方向。

## 成本与严格门槛

每次开仓和平仓均按单侧费率计入：`BASE=0.0010`，`COST50=0.00175`。12 个 cell（3 个时期 × 2 个成本 × BTC/ETH）必须全部满足：

- 净收益严格大于 0；
- 日 PF > 1；
- 最大回撤 ≤ 20%；
- 日年化 Sharpe > 0.5；
- 正收益月比例 ≥ 50%；
- 最佳盈利月集中度 ≤ 35%；
- OHLC/funding 映射覆盖率 100%；
- 信号全因果、所有执行边计费、最终空仓；
- PnL 分解误差为 0。

任一 cell 失败即记录 `NO_PREREGISTERED_EXTREME_REVERSAL_CANDIDATE`，排除本协议定义的 12h/3%/48h 价格极端反转 family，不围绕相邻回看、阈值、持有期或方向继续调参。全部通过也只允许另写执行冻结与独立稳健性协议，不得直接修改生产默认值。

`stable_profit_claimed=false`；`direction_mode=NEUTRAL`；CURRENT Final OOS 保持封存。
