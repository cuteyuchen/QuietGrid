# Round 22：Binance 官方资金费归档冻结协议

协议日期：2026-07-23

## 目的

为现货-永续 funding carry 理论上界建立独立、可复核的 BTCUSDT 与 ETHUSDT 历史资金费数据集。当前仓库没有任何 `.funding.json` sidecar，Binance REST 在当前直连和配置代理路由均返回 HTTP 451；`data.binance.vision` 的官方月度 `fundingRate` ZIP 与 `.CHECKSUM` 可访问。

本协议只定义数据冻结，不计算策略收益，不筛选月份，不读取 CURRENT Final OOS 区间。

## 唯一授权月份

每个标的只能下载以下完整月：

1. `AUTHORIZED_COMPLETE_MONTHS`：`2020-01` 至 `2023-06`，含首尾，共 42 个月；
2. `POSTHISTORY_COMPLETE_MONTHS`：`2024-08` 至 `2026-06`，含首尾，共 23 个月。

显式禁止请求、下载或解析 `2023-07` 至 `2024-07` 的任何 fundingRate 月度文件。该 13 个月区间覆盖既有 CURRENT Final OOS 及隔离缓冲，必须保持未读取。

标的固定为：

- BTCUSDT；
- ETHUSDT。

官方 URL 模板固定为：

`https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{YYYY-MM}.zip`

并必须同时下载对应 `{zip_url}.CHECKSUM`。

## 校验与规范化

每个月必须：

- HTTP 状态为 200；
- `.CHECKSUM` 第一列与 ZIP 文件实际 SHA-256 完全一致；
- ZIP 只能包含一个 `{symbol}-fundingRate-{YYYY-MM}.csv`；
- CSV 表头必须为 `calc_time,funding_interval_hours,last_funding_rate`；
- `calc_time` 必须位于文件名对应 UTC 月份内；
- `funding_interval_hours` 必须为正数；
- `last_funding_rate` 必须为有限数；
- 同一标的所有月份合并后 `calc_time` 严格递增且不得重复。

不得插值、补齐、删除合法极值或变更 funding rate 符号。

## 冻结产物

输出目录固定为 `data/backtests/round22_funding_carry`。每个标的生成：

1. 一个 UTF-8 CSV，字段固定为：
   - `funding_time`；
   - `funding_interval_hours`；
   - `funding_rate`；
   - `source_month`；
   - `source_zip_sha256`；
2. 一个 manifest JSON，至少记录：
   - schema version；
   - symbol 与市场 `USDS_M`；
   - 两个授权月段；
   - 明确排除的 `2023-07..2024-07`；
   - event count、首尾时间、重复数量；
   - CSV SHA-256；
   - 每个月 URL、ZIP SHA-256、event count、checksum verified；
   - `official_checksums_verified: true`。

正式 funding carry 评估必须在另一个冻结协议中写入两个 manifest 的实际 SHA-256；本数据协议本身不得生成任何收益结论。
