# Round 27：BTC/ETH 绝对趋势小时数据冻结协议

协议日期：2026-07-23

## 输入边界

只允许下载：

- BTCUSDT、ETHUSDT USD-M 永续 `1h` Kline；
- `2020-07..2023-06`；
- `2024-08..2026-06`。

明确禁止下载、读取或补尾 `2023-07..2024-07`，禁止读取现有 robustness/spot_robustness 冻结价格集。

## 官方校验

URL：

`https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{YYYY-MM}.zip`

每个 ZIP 必须：

- 同时获取同名 `.CHECKSUM`；
- 实际 SHA-256 等于官方 checksum；
- 只包含一个同名 CSV，无目录穿越；
- ZIP CRC 通过，解压体积不超过 10 MiB；
- 每资产恰好 59 个源月档，合计 118 个。

## 行级规则

每行必须是 Binance 标准 12 列 Kline。要求：

- `open_time` 为毫秒，或可无损除以 1000 的微秒；
- 对齐整小时、严格递增、无重复；
- OHLC 均为有限正数且关系合法；
- 每个允许月必须覆盖完整 UTC 月；
- HISTORY 与 POSTHISTORY 各自严格每小时连续；两个段之间只允许协议定义的隔离缺口；
- BTC/ETH 的全部 43,056 个 `open_time` 完全相同。

任何允许段内缺小时、异常 OHLC、重复、checksum 失败或隔离区间行都必须终止。

## 输出

每资产输出一个 CSV，固定 43,056 行：

`segment,open_time,open,high,low,close,source_month,source_zip_sha256`

manifest 必须记录：

- 本协议与数据可用性审计 SHA-256；
- 59 个源月档 URL、ZIP SHA、行数、首尾时间、timestamp 规范化数、缺口和 checksum 状态；
- HISTORY 26,280 行、POSTHISTORY 16,776 行；
- 总计 43,056 行、1,794 个完整 UTC 日、0 重复、0 段内缺小时；
- CSV SHA-256；
- `final_oos_status = SEALED_NOT_EVALUATED`。

冻结数据不授权收益计算以外的参数搜索。
