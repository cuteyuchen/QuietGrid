# Round 25：Cross-Venue Funding 数据可得性审计

审计日期：2026-07-23

## Binance COIN-M Funding

候选结构为 Binance USD-M 与 COIN-M 双永续 funding spread。官方 `data.binance.vision` 路径：

`data/futures/cm/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{YYYY-MM}.zip`

审计结果：

- `BTCUSD_PERP` 与 `ETHUSD_PERP` 的月档在 `2022-01..2022-06` 均为 HTTP 404；
- 两个标的均从 `2022-07` 月档开始可用；
- `2023-06`、`2024-08`、`2026-06` 月档与 `.CHECKSUM` 均为 HTTP 200，CSV 表头为 `calc_time,funding_interval_hours,last_funding_rate`；
- 抽查 `2020-08`、`2021-07` 等日档同样为 HTTP 404；
- 官方 `https://dapi.binance.com/dapi/v1/fundingRate` 在当前直连与配置代理下均返回 HTTP 451；
- `data-api.binance.vision` 不提供 `/dapi/v1/fundingRate`。

既有 CURRENT Development 覆盖 `2020-07-17` 至 `2022-07-25`，因此 COIN-M 官方可用历史无法覆盖 PREHISTORY，也几乎不能覆盖 Development。结论：不冻结 COIN-M funding，不用仅有 Validation/Posthistory 的样本声称跨年代稳定性。

## BitMEX Funding

官方公共 API：

`https://www.bitmex.com/api/v1/funding`

固定标的映射：

- BTC：Binance `BTCUSDT` 对 BitMEX `XBTUSD`；
- ETH：Binance `ETHUSDT` 对 BitMEX `ETHUSD`。

只请求两个授权时间段：

1. `2020-01-01T00:00:00Z` 至 `2023-07-01T00:00:00Z`，不含终点；
2. `2024-08-01T00:00:00Z` 至 `2026-07-01T00:00:00Z`，不含终点。

请求固定使用 `count=500`、`reverse=false` 并按最后事件时间分页。审计结果：

| 标的 | 第一段事件 | 第二段事件 | 合计 | 第一事件 | 最后一事件 | 8h 缺口 |
| --- | ---: | ---: | ---: | --- | --- | ---: |
| XBTUSD | 3,831 | 2,097 | 5,928 | `2020-01-01T04:00:00Z` | `2026-06-30T20:00:00Z` | 0 |
| ETHUSD | 3,831 | 2,097 | 5,928 | `2020-01-01T04:00:00Z` | `2026-06-30T20:00:00Z` | 0 |

每个标的共 13 页；所有响应为 HTTP 200。`fundingInterval` 固定 8 小时，事件在 UTC `04:00/12:00/20:00` 结算。审计时观察到：

- XBTUSD funding rate 范围：`-0.00375..0.002997`；
- ETHUSD funding rate 范围：`-0.006376..0.00661`。

API 不提供独立 `.CHECKSUM`；正式冻结必须记录每页原始 HTTP 响应 SHA-256、请求边界、事件数量与首尾事件，再对规范化 CSV 和 manifest 哈希冻结。

## 结论

选择 BitMEX XBTUSD/ETHUSD 作为 Binance USD-M 的跨交易所 funding spread 对手方。当前只确认数据完整性，不计算收益、不决定方向、不读取 `2023-07..2024-07`、不修改生产默认值。
