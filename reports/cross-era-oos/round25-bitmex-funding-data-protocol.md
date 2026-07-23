# Round 25：BitMEX 官方 Funding API 冻结协议

协议日期：2026-07-23

## 目的

为 Binance USD-M 与 BitMEX 双永续 funding spread 建立独立、可复核的 XBTUSD 与 ETHUSD 历史 funding 数据集。本协议只定义官方 API 请求、字段校验、分页与冻结，不计算策略收益、不选择交易方向、不读取 CURRENT Final OOS。

数据可得性审计：`reports/cross-era-oos/round25-cross-venue-funding-data-audit.md`，SHA-256：`e2aee11005bef69beb2faa0f6ecced8154fac154be5c828817b33467c6d88d18`。

## 官方端点与标的

唯一允许的 API：

`https://www.bitmex.com/api/v1/funding`

标的固定为：

- `XBTUSD`，映射 Binance `BTCUSDT`；
- `ETHUSD`，映射 Binance `ETHUSDT`。

API 为公开只读端点，不使用账户凭据。

## 唯一授权时间段

每个标的只允许请求：

1. `AUTHORIZED_HISTORY`：`2020-01-01T00:00:00.000Z` 至 `2023-07-01T00:00:00.000Z`，不含终点；
2. `POSTHISTORY`：`2024-08-01T00:00:00.000Z` 至 `2026-07-01T00:00:00.000Z`，不含终点。

显式禁止请求、解析或保存 `2023-07-01T00:00:00Z` 至 `2024-08-01T00:00:00Z` 的任何 funding 数据。该区间覆盖 CURRENT Final OOS 与隔离缓冲。

## 固定分页

每个时间段独立分页，参数固定为：

- `symbol={XBTUSD|ETHUSD}`；
- `count=500`；
- `reverse=false`；
- `startTime` 为段起点或上一页最后事件时间加 1 毫秒；
- `endTime` 为段终点减 1 毫秒。

每页必须：

- HTTP 状态为 200；
- 响应为 JSON 数组；
- 事件按 `timestamp` 严格递增；
- 最后一事件必须推动下一页游标；
- 记录实际请求 URL、请求起止、原始响应字节 SHA-256、事件数量与首尾时间；
- 返回少于 500 个事件时结束该段分页。

按当前冻结数据，每个标的第一段固定 8 页、第二段固定 5 页，共 13 页。页数或事件总数变化必须终止并重写协议，不得静默接受上游历史改写。

## 事件字段校验

每个事件必须满足：

- `symbol` 与请求标的一致；
- `timestamp` 为带 `Z` 的 UTC ISO-8601 时间；
- `timestamp` 位于当前授权段内；
- `fundingInterval` 固定为 `2000-01-01T08:00:00.000Z`；
- `fundingRate` 与 `fundingRateDaily` 均为有限数；
- `fundingRateDaily` 必须在浮点容差内等于 `3 * fundingRate`；
- 同一标的、同一授权段相邻事件严格间隔 8 小时；
- 不插值、不补齐、不裁剪合法极值、不改变 funding 符号。

## 固定完整性结果

每个标的必须：

- `AUTHORIZED_HISTORY` 恰好 3,831 个事件；
- `POSTHISTORY` 恰好 2,097 个事件；
- 总计恰好 5,928 个事件；
- 第一事件为 `2020-01-01T04:00:00.000Z`；
- 第一段最后事件为 `2023-06-30T20:00:00.000Z`；
- 第二段第一事件为 `2024-08-01T04:00:00.000Z`；
- 最后一事件为 `2026-06-30T20:00:00.000Z`；
- 段内无缺口、无重复；两个段之间不检查 8 小时连续性，也不读取隔离区间。

## 冻结产物

输出目录固定为 `data/backtests/round25_cross_venue_funding`。每个标的生成：

1. UTF-8 CSV，字段固定为：
   - `funding_time`，Unix 毫秒；
   - `funding_interval_hours`；
   - `funding_rate`；
   - `funding_rate_daily`；
   - `segment`；
   - `source_page_sha256`；
2. manifest JSON，至少记录：
   - schema version、数据协议与审计 SHA-256；
   - provider、API URL、symbol 与 Binance 映射；
   - 两个授权段与明确排除的隔离区间；
   - CSV SHA-256、事件数量、首尾时间、重复数量；
   - 每段事件数量、首尾时间、8h 连续性；
   - 13 页请求参数、实际 URL、原始响应 SHA-256、事件数量与首尾时间；
   - `official_api_pages_verified: true`、`segment_cadence_verified: true`、`excluded_interval_not_requested: true`。

正式 cross-venue funding spread 上界必须在另一个冻结协议中写入 XBTUSD、ETHUSD 两个 manifest 的实际 SHA-256，并同时锁定 Binance funding manifest、方向、费用、资本、窗口和门槛。本数据协议不得产生盈利结论，不得修改生产默认值，`direction_mode` 保持 `NEUTRAL`。
