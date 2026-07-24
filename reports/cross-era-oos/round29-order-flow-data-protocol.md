# Round 29：小时主动买量不平衡数据冻结协议

协议日期：2026-07-24

只允许下载 BTCUSDT、ETHUSDT USD-M 永续 `1h` Kline 的授权月份 `2020-07..2023-06` 与 `2024-08..2026-06`。

每个 ZIP 必须同时获取同名 `.CHECKSUM` 并通过 SHA-256；ZIP 只含同名 CSV、CRC 通过、解压体积不超过 10 MiB。每行必须是 Binance 标准 12 列，`open_time` 整小时递增，OHLC、volume、quote_volume、taker_buy_volume、taker_buy_quote_volume 均为有限非负数。quote volume 为 0 只允许 OHLC 完全平价且四个成交量字段全为 0，并登记为中性不平衡；其他零量或不一致必须终止。授权段内不允许缺小时或重复。

每资产输出固定 43,056 行 CSV：

`segment,open_time,open,high,low,close,volume,quote_volume,taker_buy_volume,taker_buy_quote_volume,source_month,source_zip_sha256`

manifest 必须记录数据审计与本协议 SHA-256、59 个源档 URL/SHA/行数/缺口/checksum、HISTORY/POSTHISTORY 行数、跨资产时间对齐、CSV SHA-256，以及 `final_oos_status = SEALED_NOT_EVALUATED`。冻结后不得搜索参数或读取隔离区间。
