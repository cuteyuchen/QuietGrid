# Round 27：BTC/ETH 绝对趋势数据可用性审计

审计日期：2026-07-23

## 研究切换

Round 26 固定一周 USD-M 永续/季度期限价差在全部 `0/12` 个单元通过。各单元的季度价差价格 PnL 均为正，但永续 funding 与交易费用在所有年代和资产中将其完全抵消，结论固定为 `NO_PREREGISTERED_QUARTERLY_CALENDAR_SPREAD_CANDIDATE`。

本轮切换到独立收益来源：BTCUSDT、ETHUSDT 永续的绝对时间序列趋势。它不交易跨资产价差、funding carry、premium basis、季度期限结构或中性网格库存。

Round 26 结果：`reports/cross-era-oos/round26-quarterly-calendar-spread-results.json`，SHA-256：`171598de0aa94607f1b05ea6bfd4f79f3c58252b0eb39e3b3c2290cbe78965cf`。

## 官方源与允许月份

- 数据源：Binance Data Vision；
- 路径：`data/futures/um/monthly/klines/{symbol}/1h`；
- 标的：`BTCUSDT`、`ETHUSDT`；
- 类型：USD-M 永续 `1h` Kline；
- HISTORY：`2020-07..2023-06`，36 个月；
- POSTHISTORY：`2024-08..2026-06`，23 个月；
- 每资产 59 个 ZIP，总计 118 个 ZIP；
- 每个 ZIP 均要求同名官方 `.CHECKSUM`。

存在性审计共检查 118 个 ZIP 和 118 个 CHECKSUM，236 个对象全部返回 HTTP 200，`Content-Length` 合计 4,514,631 bytes，缺失对象 0。

没有请求 `2023-07..2024-07` 的任何归档；禁止读取现有 `data/backtests/robustness` 与 `data/backtests/spot_robustness`。

## 预期数据量

- HISTORY：2020-07-01 至 2023-06-30，共 1,095 个 UTC 日、26,280 个小时；
- POSTHISTORY：2024-08-01 至 2026-06-30，共 699 个 UTC 日、16,776 个小时；
- 合计每资产 1,794 日、43,056 个小时；
- BTC/ETH 时间戳必须完全一致。

## 预注册评估边界

经典 SMA50/200 需要 200 个完整 UTC 日的先验 close：

- DEVELOPMENT：2021-02-06 至 2022-06-30，共 510 个评估日；使用 2020-07-01 起的历史作 warmup；
- VALIDATION：2022-07-01 至 2023-06-30，共 365 个评估日；允许使用此前已消费 HISTORY close 作因果 warmup，但权益和头寸重置；
- POSTHISTORY：2024-08-01 重新开始 warmup，前 200 日不交易；2025-02-17 至 2026-06-30 共 499 个评估日；
- CURRENT Final OOS 与隔离区间不用于 warmup、信号或收益。

结论：`ROUND27_ABSOLUTE_TREND_DATA_AVAILABLE`。允许冻结上述 118 个官方月档并生成严格连续的小时数据，不授权调整 SMA 周期、费用、方向或评估边界。
