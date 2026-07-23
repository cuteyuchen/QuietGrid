# Round 26：USD-M 季度期限价差数据冻结协议

协议日期：2026-07-23

## 冻结边界

本协议只冻结 Round 26 数据可用性审计中预先定义的 206 个非交割周窗口。数据源、时间、资产、换约规则和字段均在计算基差或收益前固定。

允许区间：

- DEVELOPMENT：初始 73 周，排除 6 个交割周，冻结 67 个窗口；
- VALIDATION：初始 52 周，排除 4 个交割周，冻结 48 个窗口；
- POSTHISTORY：初始 99 周，排除 8 个交割周，冻结 91 个窗口。

每个窗口为 `[window_start, window_end]` 两端都包含的 1h 观测路径，共 169 个整点；实际持有时间为 168 小时。

禁止请求或保存 `2023-07-01T00:00:00Z..2024-08-01T00:00:00Z` 的任何数据。禁止读取现有 `data/backtests/robustness` 或 `data/backtests/spot_robustness` 冻结集。

## 标的与换约

- BTC：永续 `BTCUSDT`，季度 `BTCUSDT_YYMMDD`；
- ETH：永续 `ETHUSDT`，季度 `ETHUSDT_YYMMDD`；
- 季度合约交割时刻按代码日期 `08:00 UTC`；
- 每个窗口选择交割时刻严格晚于窗口退出时刻的最近季度合约；
- `window_end` 日期等于任一季度交割日的周统一排除，因为下一季度合约在窗口入场时尚未形成完整官方 K 线路径；
- BTC/ETH 使用相同交割日期；
- 禁止依据价格、基差、成交量、收益或资产表现改变合约。

预期使用的每资产季度代码：

`210326, 210625, 210924, 211231, 220325, 220624, 220930, 221230, 230331, 230630, 240927, 241227, 250328, 250627, 250926, 251226, 260327, 260626`。

## 官方归档

归档 URL：

`https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{YYYY-MM}.zip`

每个 ZIP 必须同时下载同名 `.CHECKSUM`，并满足：

- CHECKSUM 文件名与 ZIP 文件名一致；
- 实际 ZIP SHA-256 等于官方 checksum；
- ZIP 只包含一个无目录穿越的同名 CSV；
- ZIP CRC 校验通过；
- 解压体积不超过 10 MiB；
- 合计恰好 240 个唯一月档，BTC/ETH 各 120 个；
- 所有归档均来自允许月份，隔离区间归档数必须为 0。

## K 线解析

每行必须是 Binance 标准 12 列 Kline：

`open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore`

要求：

- `open_time` 为 Unix 毫秒，或可无损除以 1000 规范化为毫秒的微秒；
- 每个源月档内 `open_time` 严格递增、无重复、对齐整小时；
- 授权窗口所需行的 `open/high/low/close` 必须均为有限正数；
- 授权窗口所需行必须满足 `high >= max(open, close)`、`low <= min(open, close)`、`high >= low`；
- 授权窗口外的官方异常 OHLC 行不得进入冻结 CSV，必须在源月档审计中记录行号、时间和原始 OHLC；该规则统一应用于所有源档；
- 源月档中的缺口必须记录；任何冻结窗口需要的整点缺失都必须终止；
- 同一窗口的永续和季度 K 线必须拥有完全相同的 169 个 `open_time`。

## 冻结输出

每个资产输出一个窗口化 CSV，共 34,814 行，即 `206 × 169`。主键为 `(window_id, open_time)`，相邻非交割周窗口共享的边界时刻允许以不同 `window_id` 各出现一次。

CSV 固定字段：

`role,window_id,window_start,window_end,open_time,perpetual_symbol,quarterly_symbol,perpetual_open,perpetual_high,perpetual_low,perpetual_close,quarterly_open,quarterly_high,quarterly_low,quarterly_close,perpetual_source_month,perpetual_source_zip_sha256,quarterly_source_month,quarterly_source_zip_sha256`

manifest 必须记录：

- 本协议和数据可用性审计 SHA-256；
- 资产、窗口、合约选择和隔离区间；
- 120 个官方源月档的 URL、ZIP SHA、原始行数、缺口、窗口外异常 OHLC 行和 checksum 状态；
- 初始 224 周、统一排除的 18 个交割周及正式 206 个窗口；
- 每个窗口的季度合约、169 行完整性、首尾时间和两个 venue/source 哈希集合；
- 输出 CSV SHA-256、34,814 行、0 个主键重复；
- `CURRENT Final OOS = SEALED_NOT_EVALUATED`。

任何完整性失败必须终止且不得生成可用于收益评估的 manifest。冻结数据不授权修改窗口、合约、频率或隔离区间。
