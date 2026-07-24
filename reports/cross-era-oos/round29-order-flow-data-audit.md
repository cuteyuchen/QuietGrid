# Round 29：小时主动买量不平衡数据可用性审计

审计日期：2026-07-24

Round 28 的现货/季度现金套利候选在固定数据排除后仍为 `0/12`，Validation 没有达到预注册基差门槛，结论固定为 `NO_PREREGISTERED_SPOT_QUARTERLY_CARRY_CANDIDATE`。

本轮切换到未使用过的成交量信息收益来源：Binance USD-M 永续 Kline 的 quote volume 与 taker-buy quote volume。价格方向仍只由冻结的小时主动买量不平衡产生，不使用 SMA、季度基差、cross-venue funding、premium 或网格库存。

## 官方源

- 数据源：Binance Data Vision；
- 路径：`data/futures/um/monthly/klines/{symbol}/1h`；
- 标的：BTCUSDT、ETHUSDT；
- HISTORY：`2020-07..2023-06`；POSTHISTORY：`2024-08..2026-06`；
- 每资产 59 个 ZIP，总计 118 个 ZIP，每个同名 CHECKSUM；
- 明确不读取 `2023-07..2024-07`，不读取已有 robustness/spot_robustness 冻结集。

复用同一官方月份边界的可用性审计：118 个 ZIP 与 118 个 CHECKSUM 全部 HTTP 200，缺失 0；字段使用标准 12 列 Kline 的 `quote_volume` 与 `taker_buy_quote_volume`。全量字段扫描发现 BTC/ETH 在 `2024-10-28 20:00 UTC` 同时存在一根 OHLC 完全平价且 volume、quote volume、taker-buy volume、taker-buy quote volume 全为 0 的维护小时；该唯一形态登记为中性不平衡 `I=0`，其他零 quote volume 或字段不一致仍视为错误。

## 预期数据量

- 每资产 43,056 根连续小时 Kline；
- HISTORY 26,280 行、POSTHISTORY 16,776 行；
- BTC/ETH 的 `open_time` 必须完全一致；
- 允许记录窗口外官方缺口/坏行，但授权段内缺小时或字段异常必须终止。

## 结论

`ROUND29_ORDER_FLOW_DATA_AVAILABLE`。允许冻结官方 Kline 的成交量字段并评估唯一主动买量不平衡候选；不授权读取 CURRENT Final OOS 或事后改变信号方向、窗口、阈值和成本。
