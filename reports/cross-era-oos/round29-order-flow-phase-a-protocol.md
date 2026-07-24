# Round 29：小时主动买量不平衡单一候选协议

协议日期：2026-07-24

## 唯一候选

候选 ID：`ORDER_FLOW_IMBALANCE_8H_15PCT_1X_V1`。

前置结果：Round 28 结果 `reports/cross-era-oos/round28-spot-quarterly-carry-results.json`，SHA-256：`f0134d155d6f8435aeca66f66d826c7ff24eb05f46d295050aec93ec00f30f8f`，结论必须为 `NO_PREREGISTERED_SPOT_QUARTERLY_CARRY_CANDIDATE`；CURRENT Final OOS 必须保持 `SEALED_NOT_EVALUATED`。

## 因果信号与执行

1. 定义单小时主动买量不平衡 `I_t = 2 * taker_buy_quote_volume_t / quote_volume_t - 1`；仅对协议登记的全零量平价维护小时定义 `I_t = 0`，其他 quote volume 为 0 时终止；
2. 在每个小时 `t` 的 `1h open` 执行前，只使用 `t-8..t-1` 八根已完成 Kline 的 `I` 算术均值；
3. 均值 `>= +0.15` 目标仓位为 long，`<= -0.15` 目标仓位为 short，介于两者之间目标为空仓；
4. 信号变化时先按当前 open 平旧仓，再按同一 open 建新仓；无变化不交易；
5. 固定 1x 名义：每次主动仓位数量为 `initial_capital / execution_price`；
6. 纳入持仓期间每个真实 Binance funding 事件；正 funding 多头支付、空头收取；
7. 不使用当小时 close、未来 volume、未来 funding 或跨资产联动。

## 拆分

沿用 Round 27 的三个独立拆分：DEVELOPMENT 510 日、VALIDATION 365 日、POSTHISTORY 499 日；每资产、拆分和成本情景权益与头寸重置，Final OOS 不用于 warmup 或收益。

## 成本与门槛

BASE 单侧 taker+滑点 `0.0010`，COST50 单侧 `0.00175`；每次平仓和开仓均计费，最终强平计一次。每个 cell 必须满足总收益>0、日 PF>1、最大回撤≤20%、日年化 Sharpe>0.5、正收益月≥50%、最佳盈利月集中度≤35%、价格/volume/funding 覆盖率100%、信号全因果且最终空仓。

12 个 cell 任一失败即记录 `NO_PREREGISTERED_ORDER_FLOW_CANDIDATE`，排除本协议定义的 8 小时、15%、1x 主动买量不平衡 family；不得围绕阈值、窗口、顺逆势或资产特例搜索相邻版本。12/12 全部通过才允许另写执行冻结协议；`stable_profit_claimed = false`，生产默认值不变。
