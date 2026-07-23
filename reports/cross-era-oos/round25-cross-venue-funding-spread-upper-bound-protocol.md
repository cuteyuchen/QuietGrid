# Round 25：Binance–BitMEX Funding Spread 乐观上界协议

协议日期：2026-07-23

## 研究目的

Round 24 的 BTC/ETH 四腿 premium dispersion 仅通过 4/8 个年代与成本单元，禁止继续调整权重、观察期或退出范围。本轮切换到独立收益来源：在 Binance USD-M 与 BitMEX 永续之间持有方向相反、等 USD 名义的双永续仓位，只评估两家交易所实际 funding rate 差。

Round 24 结果：`reports/cross-era-oos/round24-cross-asset-premium-dispersion-upper-bound-results.json`，SHA-256：`c9dafcbf47770711a998b58bbb02f1c5a56d967bc408d0619ec93a04659f79b3`。结论必须为 `NO_PREREGISTERED_CROSS_ASSET_PREMIUM_DISPERSION_CANDIDATE`，Final OOS 必须保持 `SEALED_NOT_EVALUATED`。

窗口来源固定为 Round 22 funding carry 结果：`reports/cross-era-oos/round22-funding-carry-upper-bound-results.json`，SHA-256：`622d359710b3f4e6f6371211a946ae4f33ed24510d5d1262def7ada29c47ab41`。

## 冻结 Funding 数据

Binance USD-M：

- BTCUSDT manifest SHA-256：`a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57`；
- ETHUSDT manifest SHA-256：`19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f`。

BitMEX：

- 数据协议 SHA-256：`fa45fc7d07fea75a5bc98a0cfdd07773002c292d2c71732639e7f6f2834dbe53`；
- XBTUSD manifest SHA-256：`4474476261ec4cb9c815c74993dc4b83e57eec55dcf1b887006b81b45a93162c`；
- ETHUSD manifest SHA-256：`e052002c6308b226f22fc22f17c6de90b8f3cdad1fba4d42b575af40b55f03ed`。

每个 manifest 必须恰好包含 5,928 个 funding 事件、0 个重复事件，不得包含 `2023-07..2024-07`。BitMEX 每个标的必须包含 13 个已冻结官方 API 页面。

## 标的、资本与方向符号

标的固定映射：

- BTC：Binance `BTCUSDT` 对 BitMEX `XBTUSD`；
- ETH：Binance `ETHUSDT` 对 BitMEX `ETHUSD`。

BTC 与 ETH 分别独立评估：

- BTC gross capital：`500 USDT`，Binance 与 BitMEX 各 `250 USDT` 名义；
- ETH gross capital：`300 USDT`，Binance 与 BitMEX 各 `150 USDT` 名义。

两家交易所均采用“正 funding rate 时多头支付空头”的符号约定。定义窗口内：

`funding_spread_sum = sum(binance_rate) - sum(bitmex_rate)`

Oracle 方向固定为：

- `funding_spread_sum >= 0`：short Binance、long BitMEX；
- `funding_spread_sum < 0`：long Binance、short BitMEX。

方向使用完整未来窗口 funding 差，明确不可部署；`spread = 0` 时仍使用第一种方向并完成交易。

## 唯一窗口 PnL 定义

每个授权周末/长假窗口固定执行一次双腿 round trip：

1. Binance 纳入 `market_close <= funding_time < force_close_at` 的全部事件；
2. BitMEX 使用相同时间边界；Binance 事件通常在 UTC `00/08/16`，BitMEX 通常在 `04/12/20`，不要求事件时间对齐；
3. 每个交易所、每个窗口必须至少包含一个 funding event；
4. 每腿名义为 `gross_capital / 2`；
5. Funding 毛收益为：
   - `per_leg_notional * abs(funding_spread_sum)`；
6. BASE 假设 Binance 与 BitMEX 每次 Maker 成交费率均为 `0.0002`；
7. COST50 假设两家 Maker 费率均为 `0.0003`；
8. 两腿入场名义总和等于 gross capital，入场费为 `gross_capital * maker_fee_rate`，退出费相同；
9. 窗口净收益为 `funding_income - 2 * gross_capital * maker_fee_rate`；
10. 路径收益按两个交易所实际 funding timestamp 合并排序：
    - 入场后先记录 `-entry_fee`；
    - short Binance/long BitMEX 时，Binance 事件贡献 `+notional * rate`，BitMEX 事件贡献 `-notional * rate`；反向持仓时符号相反；
    - 每次 funding 结算后记录累计 funding PnL 减入场费；
    - 窗口结束支付退出费并记录最终净收益；
    - `minimum_path_pnl` 为上述序列最小值；
11. 忽略两家永续成交价格基差、反向合约币本位结算换算、抵押品价格、跨所转账、保证金、强平、Maker 排队、腿间延迟、滑点、API 延迟、交易所和托管风险；
12. Oracle 知道未来 funding spread，因此结果只能用于排除 family。

## 授权窗口

使用 Round 22 的 funding 完整窗口，不继承 Round 23 Premium Index 特有的数据缺口排除：

- PREHISTORY：28；
- CURRENT Development：108；
- CURRENT Validation Complete Months：49；
- POSTHISTORY：108。

合计 293 个窗口。不同年代/拆分、成本情景和标的之间不传递权益或方向状态。禁止读取 CURRENT Final OOS。

## 完整性审计

正式运行必须验证：

- 本协议、Round 22/24 结果、四个 funding manifest 与四个 CSV 的哈希全部匹配；
- Binance 与 BitMEX 各标的事件时间严格递增、rate 有限、interval 为 8 小时；
- BitMEX manifest 的 13 个原始 API 页面哈希与 CSV 行级 page SHA 一致；
- Round 22 的 293 个窗口在 BASE/COST50、BTC/ETH 记录中定义完全一致；
- 每个窗口、每家交易所至少包含一个事件，事件不得重复分配给多个窗口；
- 数据与窗口均不触碰 `2023-07..2024-07`；
- CURRENT Final OOS 状态保持 `SEALED_NOT_EVALUATED`。

任何完整性失败都必须终止，不得生成收益结论。

## 上界门槛

四个年代/拆分 × BASE/COST50 × BTC/ETH，共 16 个 cell。每个 cell 必须分别满足：

- 总净收益严格为正；
- Profit Factor 大于 1；
- 最大回撤不高于 `5%`；
- 最佳盈利窗口占全部正收益比例不高于 `35%`；
- 净收益为正的窗口比例不低于 `25%`；
- Binance 与 BitMEX funding event 覆盖率均为 `100%`；
- 每个窗口完成一次双腿 round trip。

不得用年代、成本情景或标的之间的收益相互抵消。

## 结论规则

- 16/16 cell 全部通过：记录 `CROSS_VENUE_FUNDING_SPREAD_WORTH_PREREGISTRATION`；只允许随后冻结跨所成交基差、历史实际费率、币本位换算和保证金风险，并定义单一因果方向候选；
- 任一 cell 失败：记录 `NO_PREREGISTERED_CROSS_VENUE_FUNDING_SPREAD_CANDIDATE`，排除本协议定义的周末双永续 funding spread family；
- 禁止在看到结果后调整交易所、方向、费用、资本、窗口、标的或持有区间；
- 本轮不选择生产候选、不授权 Final OOS、不修改生产默认值；
- `direction_mode` 保持 `NEUTRAL`，`stable_profit_claimed` 保持 false。
