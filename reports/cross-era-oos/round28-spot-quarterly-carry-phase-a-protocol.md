# Round 28：现货/季度交割合约现金套利单一候选协议

协议日期：2026-07-24

## 唯一候选

候选 ID：`SPOT_QUARTERLY_CARRY_30D_50BPS_V1`。

该规则只做正向现金套利，不搜索相邻持有天数、基差阈值、资产权重、费用或退出时点。

前置结果：

- Round 27 结果 SHA-256：`3a15ae8a970f1ab54fde8e87a8303b2448737a6c618b56df4e78b3522e63c9f0`；
- 结论必须为 `NO_PREREGISTERED_ABSOLUTE_TREND_CANDIDATE`；
- CURRENT Final OOS 必须保持 `SEALED_NOT_EVALUATED`。

## 因果入场与退出

1. 对每个注册季度合约，只在交割前 30 日 `08:00 UTC` 检查一次；
2. 入场基差定义为 `quarterly_open / spot_open - 1`；
3. 入场基差严格大于 `0.005` 时，同时做多现货、做空同币种季度合约；
4. 入场基差不高于 `0.005` 时，该窗口保持现金，不反向交易、不借币做空现货；
5. 已入场后不止盈、不止损、不追加保证金、不提前退出；
6. 在交割日 `07:00` Kline close 同步退出两腿；
7. 方向和是否入场只使用入场整点的现货与季度合约 open，不使用未来基差、最优退出或交割结果。

## 拆分与状态隔离

- DEVELOPMENT：5 个交割窗口，`210625, 210924, 211231, 220325, 220624`；
- VALIDATION：3 个交割窗口，`220930, 221230, 230630`；
- POSTHISTORY：8 个交割窗口，`240927..260626`；
- 三个拆分、BTC/ETH 和成本情景之间权益、数量与头寸完全重置；
- 每个窗口使用固定目标总名义，不跨窗口复利；
- `2023-07..2024-07` 不用于信号、路径或收益。

## 仓位与 PnL

- 初始权益/固定双腿总名义：BTC `500 USDT`，ETH `300 USDT`；
- `qty = initial_capital / (spot_entry + quarterly_entry)`；
- 入场总毛名义等于初始权益，现货与季度腿近似各占一半；
- 价格 PnL 为 `qty * ((spot_exit - spot_entry) - (quarterly_exit - quarterly_entry))`；
- 小时路径按两腿 `1h open` 标记，最终退出使用两腿最后一根 `07:00` Kline close；
- 不持有永续合约，因此 funding 固定为 0；
- 忽略现货托管收益、闲置现金收益和税费。

## 执行成本

每次入场和退出均按主动成交处理，每腿每侧费用与滑点合并为固定比例：

- BASE：现货 `0.0015`（taker `0.0010` + 5 bps），季度合约 `0.0010`（taker `0.0005` + 5 bps）；
- COST50：现货 `0.0020`（taker `0.0010` + 10 bps），季度合约 `0.00175`（taker `0.00075` + 10 bps）；
- 一个完整窗口最多四个成交侧：现货入场、季度入场、现货退出、季度退出；
- 保持现金的窗口不收费。

不对 Maker 成交率、返佣或未来手续费折扣作有利假设。

## 12 个严格单元

DEVELOPMENT、VALIDATION、POSTHISTORY × BASE/COST50 × BTC/ETH，共 12 个 cell。每个 cell 必须分别满足：

- 总净收益严格为正；
- 基于已入场交割窗口净收益的 Profit Factor 大于 1；
- 最大小时路径回撤不高于 `5%`；
- 已入场窗口正收益比例不低于 `75%`；
- 最佳盈利窗口占全部正收益比例不高于 `35%`；
- DEVELOPMENT 至少 3 个、VALIDATION 至少 2 个、POSTHISTORY 至少 6 个窗口实际入场；
- 现货与季度价格覆盖率均为 `100%`；
- 所有入场决定因果，所有已入场窗口四个执行侧全部计入冻结成本；
- 最终无现货或季度遗留仓位，funding PnL 必须严格为 0。

不得用资产、年代或成本情景间收益互相抵消。

## 结论

- 12/12 全部通过：记录 `SPOT_QUARTERLY_CARRY_WORTH_EXECUTION_PREREGISTRATION`，随后只允许冻结交易所最小数量、保证金、交割机制、盘口冲击和现货托管风险；
- 任一 cell 失败：记录 `NO_PREREGISTERED_SPOT_QUARTERLY_CARRY_CANDIDATE`，排除本协议定义的 30 日、50 bps、正向现金套利 family；
- 看到结果后禁止调整持有天数、最低基差、费用、资产或退出时点；
- 不修改生产默认值，`direction_mode` 保持 `NEUTRAL`；
- `final_oos_authorized = false`，`stable_profit_claimed = false`。
