# Round 28：现货/季度交割合约现金套利数据冻结协议

协议日期：2026-07-24

## 输入边界

只允许下载数据可用性审计列出的 BTCUSDT、ETHUSDT 现货 `1h` Kline，以及剩余 16 个对应 USD-M 季度交割合约的 `1h` Kline。`210326` 和 `230331` 两个窗口因官方现货缺行已登记剔除，禁止补回。

明确禁止下载、读取或补尾 `2023-07..2024-07`，禁止读取现有 robustness/spot_robustness 冻结价格集，禁止加入审计未列出的交割合约或相邻持有窗口。

## 官方校验

现货 URL：

`https://data.binance.vision/data/spot/monthly/klines/{symbol}/1h/{symbol}-1h-{YYYY-MM}.zip`

季度合约 URL：

`https://data.binance.vision/data/futures/um/monthly/klines/{contract}/1h/{contract}-1h-{YYYY-MM}.zip`

每个 ZIP 必须：

- 同时获取同名官方 `.CHECKSUM`；
- 实际 SHA-256 等于官方 checksum；
- 只包含一个同名 CSV，无目录穿越；
- ZIP CRC 通过，解压体积不超过 10 MiB；
- 每资产恰好 62 个唯一授权源月档，合计 124 个；被剔除窗口的非共享源档不进入冻结输出。

## 行级规则

每行必须是 Binance 标准 12 列 Kline。要求：

- `open_time` 为毫秒，或可无损除以 1000 的微秒；
- 对齐整小时、严格递增、无重复；
- 授权窗口内 OHLC 均为有限正数且关系合法；窗口外官方坏行必须记录，且不得被选入冻结窗口；
- 现货与季度合约月档均允许在授权窗口外存在官方缺行或上市/交割边界，解析器必须记录缺口；全部授权窗口小时必须存在且连续；
- 每个窗口从交割前 30 日 `08:00 UTC` 到交割日 `07:00 UTC` 恰好 720 行；
- 窗口内现货与季度合约时间戳必须完全一致；
- 任何授权窗口内缺小时、异常 OHLC、重复、checksum 失败或隔离区间行都必须终止。

## 输出

每资产输出一个 CSV，固定 11,520 行：

`role,window_id,entry_time,expiry_time,open_time,spot_symbol,quarterly_symbol,spot_open,spot_high,spot_low,spot_close,quarterly_open,quarterly_high,quarterly_low,quarterly_close,spot_source_month,spot_source_zip_sha256,quarterly_source_month,quarterly_source_zip_sha256`

manifest 必须记录：

- 本协议与数据可用性审计 SHA-256；
- 62 个授权源月档 URL、ZIP SHA、行数、首尾时间、timestamp 规范化数、缺口和 checksum 状态；
- 16 个窗口各 720 个对齐小时；
- 每资产 11,520 行、0 重复主键、100% 窗口覆盖；
- CSV SHA-256；
- `final_oos_status = SEALED_NOT_EVALUATED`。

冻结数据不授权参数搜索或 Final OOS 评估。
