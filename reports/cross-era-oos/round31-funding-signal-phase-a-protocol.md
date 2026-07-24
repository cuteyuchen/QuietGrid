# Round 31：因果 Funding Rate 方向信号单一候选协议

协议日期：2026-07-24

## 研究定位

Round 22 评估的是使用未来已知 funding 总和、理想现货对冲和忽略基差风险的 delta-neutral carry 上界，不是可部署方向信号。本轮切换到不同定义：在 funding event 已发生并可观测后，使用该 event 的费率作为因果方向信号，持有固定时间的单腿 1x 方向仓位。不得使用未来 funding 选择方向，也不把 BTC 与 ETH 的收益互相抵消。

## 数据与隔离

1. 价格输入复用 Round 29 已冻结的 Binance USD-M 1h Kline manifest/CSV；只读取 `open_time/open/close`，但必须重新验证官方 archive checksum、行级 source SHA、小时连续性和 segment 行数。
2. funding 输入复用 Round 22 已冻结的 Binance USD-M funding manifest/CSV，重新验证 manifest、CSV SHA、事件顺序和事件到整点小时的唯一映射。
3. 只使用三个授权段：DEVELOPMENT（2021-02-06 01:00 至 2022-06-30 23:00 UTC）、VALIDATION（2022-07-01 01:00 至 2023-06-30 23:00 UTC）、POSTHISTORY（2025-02-17 01:00 至 2026-06-30 23:00 UTC）。
4. 2023-07 至 2024-07 隔离区间、CURRENT Final OOS 和既有 robustness/spot_robustness 结果不参与选参或收益计算。

## Development-only 选择规则

在查看 Validation、Posthistory 或任何封存结果前，固定筛选集合为：

- 方向：`CONTRARIAN`、`MOMENTUM`；
- 费率阈值：`1/2/5/10/20` bps（`0.0001/0.0002/0.0005/0.001/0.002`）；
- 固定持有：`8/24/48/72` 小时；
- funding event 发生后固定等待 1 小时，在下一根 1h open 执行；
- 持仓期间忽略新 event，不重叠、不加仓、不反向翻仓。

每个候选只在 DEVELOPMENT 的 COST50 情景中选择：BTC、ETH 净收益都必须严格为正，再按以下字典序排序：

1. 两资产中较小的 COST50 净收益，降序；
2. 两资产中较小的日年化 Sharpe，降序；
3. 两资产中较小的正收益月比例，降序；
4. 候选 ID，字典序升序。

该规则选出的唯一候选为：`FUNDING_RATE_CONTRARIAN_1BP_HOLD72_1X_V1`。

## 因果信号与执行

对每个 funding event：

1. 只在 event 已发生后读取该 event 的 `funding_rate`；正费率目标为空头，负费率目标为多头；绝对值低于 1bp 时不入场；
2. 在 event 所在整点之后的下一根 1h open 执行，固定名义为该 cell 初始资本，杠杆 1x；
3. 持有 72 小时，在计划退出小时的 open 平仓；若 cell 到达末端则在最后一根 Kline close 强平；
4. 持仓期间按真实 funding event 与该小时 open 计入 funding PnL；不使用未来 funding 方向或未来价格；
5. 平仓后的同一小时不重新入场，跨 split 不传递权益、头寸或方向状态。

## 成本与严格门槛

每次开仓和平仓均按单侧费率计入：`BASE=0.0010`，`COST50=0.00175`。12 个 cell（3 个时期 × 2 个成本 × BTC/ETH）必须全部满足：

- 净收益严格大于 0；
- 日 PF > 1；
- 最大回撤 ≤ 20%；
- 日年化 Sharpe > 0.5；
- 正收益月比例 ≥ 50%；
- 最佳盈利月集中度 ≤ 35%；
- 价格/funding 映射覆盖率 100%；
- 信号全因果、所有执行边计费、最终空仓；
- PnL 分解误差为 0。

任一 cell 失败即记录 `NO_PREREGISTERED_FUNDING_SIGNAL_CANDIDATE`，排除本协议定义的因果 funding-rate 方向信号 family，不围绕相邻阈值、持有期、等待期或方向继续调参。全部通过也只允许另写执行冻结和独立稳健性协议，不得直接修改生产默认值。

`stable_profit_claimed=false`；`direction_mode=NEUTRAL`；CURRENT Final OOS 保持封存。
