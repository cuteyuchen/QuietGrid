# Round 23：Binance Premium Index 官方归档冻结协议

协议日期：2026-07-23

## 目的

为独立的现货-永续基差收敛收益上界冻结 BTCUSDT 与 ETHUSDT 的官方 `premiumIndexKlines`。本协议只定义数据下载、校验与窗口裁剪，不计算收益、不选择方向、不筛选窗口，也不授权读取 CURRENT Final OOS。

数据可得性审计：`reports/cross-era-oos/round23-independent-yield-data-audit.md`，SHA-256：`8e4e218b8a8000d9ab87053e555027e22f74b2ce70beb5131e8483c5c8db25c0`。

窗口语义沿用 Round 22 funding carry 协议：`reports/cross-era-oos/round22-funding-carry-upper-bound-protocol.md`，SHA-256：`d5df0db9557946b06efa2e8990fe483a6cf8e9a8806fff892f8da53cb6652579`。

用于重建授权窗口的冻结输入：

- `reports/cross-era-oos/round12-quadratic-volatility-defense-results.json`，SHA-256：`d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d`；
- `reports/cross-era-oos/round13-prehistory-quadratic-w2160-results.json`，SHA-256：`1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3`。

## 唯一授权月份

每个标的只能请求以下完整月：

1. `AUTHORIZED_COMPLETE_MONTHS`：`2020-01` 至 `2023-06`，含首尾，共 42 个月；
2. `POSTHISTORY_COMPLETE_MONTHS`：`2024-08` 至 `2026-06`，含首尾，共 23 个月。

显式禁止请求、下载或解析 `2023-07` 至 `2024-07` 的任何 `premiumIndexKlines` 文件。该 13 个月覆盖 CURRENT Final OOS 与隔离缓冲，必须保持未读取。

标的固定为 BTCUSDT、ETHUSDT；周期固定为 `1m`。官方 URL 模板固定为：

`https://data.binance.vision/data/futures/um/monthly/premiumIndexKlines/{symbol}/1m/{symbol}-1m-{YYYY-MM}.zip`

每个 ZIP 必须同时下载并验证 `{zip_url}.CHECKSUM`。

## 唯一授权窗口

窗口必须由上述冻结输入按 Round 22 的既有函数和 `force_close_minutes=120` 重建，数量固定为：

- PREHISTORY：28 个成对完整窗口；
- CURRENT Development：108 个窗口；
- CURRENT Validation Complete Months：49 个窗口，`force_close_at` 不晚于 `2023-07-01T00:00:00Z`；
- POSTHISTORY：108 个窗口，边界范围固定为 `2024-08-01T00:00:00Z` 至 `2026-07-01T00:00:00Z`。

原始授权集合合计为 293 个互不重叠的窗口。官方源完整性审计在任何 PnL 计算前发现两个标的共同存在两个无法由日档补回的窗口缺口：

- `nyse_20200117T210000Z`：缺失 29 分钟；
- `nyse_20260626T200000Z`：缺失 360 分钟。

这两个窗口必须同时从 BTCUSDT 与 ETHUSDT 排除，不允许按标的分别删样本，也不允许插值。正式冻结集合因此固定为：PREHISTORY 27、CURRENT Development 108、CURRENT Validation Complete Months 49、POSTHISTORY 107，合计 291 个窗口。除此之外出现任何不完整窗口都必须终止并重写协议。

每个保留窗口只冻结 `[market_close, force_close_at)` 内的每分钟 premium close；窗口外数据在源缺口审计完成后丢弃。

## 月度归档校验

每个月必须满足：

- ZIP 和 `.CHECKSUM` HTTP 状态均为 200；
- ZIP 实际 SHA-256 与官方 checksum 完全一致，且 checksum 中的文件名匹配；
- ZIP 只包含一个 `{symbol}-1m-{YYYY-MM}.csv`，不得包含目录或额外文件；
- CSV 只能是 Binance 标准 12 列 Kline 顺序；允许 2020 年无表头，存在表头时第一列必须为 `open_time`；
- `open_time` 必须为 Unix 毫秒；若官方值为 Unix 微秒，只允许整除 1000 后规范化为毫秒并记录规范化行数；
- `open_time` 必须严格递增且位于文件名对应 UTC 月份；月档首尾缺失、内部缺口和重复均必须逐段记录，禁止把月档本身伪报为完整；
- 第五列 `close` 必须为有限 premium index 小数；
- 不允许插值、补齐、删除合法极值或改变 premium 符号。

## 官方日档补源规则

月档时间戳审计已冻结以下汇总事实：

- BTCUSDT：月档共缺 11,707 分钟；
- ETHUSDT：月档共缺 11,704 分钟；
- 对每个缺口覆盖的 UTC 日期请求同一官方源的日度 `premiumIndexKlines` ZIP 与 `.CHECKSUM` 后，两个标的均可补回 10,080 分钟；
- 补源后月档仍缺 BTCUSDT 1,627 分钟、ETHUSDT 1,624 分钟，其中落入原始 293 个授权窗口的缺失均为上述两个共同窗口，共 389 分钟；其余缺失均在窗口外。

日档 URL 模板固定为：

`https://data.binance.vision/data/futures/um/daily/premiumIndexKlines/{symbol}/1m/{symbol}-1m-{YYYY-MM-DD}.zip`

补源必须遵守：

- 只允许请求月档已证明缺失分钟所覆盖的 UTC 日期；
- HTTP 200 的日档必须验证官方 checksum、唯一 CSV 文件名、标准 12 列、时间范围、严格递增与有限 close；
- 日档与月档重叠分钟的 premium close 必须完全一致；
- 只允许用日档中与月档缺失时间戳完全相同的行补源，不得覆盖月档已有行；
- 日档同样缺失的分钟保持缺失；HTTP 404 必须记录，不得改用非官方第三方数据；
- 两个固定排除窗口之外，日档补源后的 291 个窗口必须逐分钟完整。

## 窗口裁剪完整性

两个标的分别必须满足：

- 291 个保留窗口全部存在且顺序一致，两个固定排除窗口均不得出现在 CSV；
- 每个窗口第一行 `open_time` 等于 `market_close`，最后一行等于 `force_close_at - 60,000ms`；
- 每个窗口行数严格等于 `(force_close_at - market_close) / 60,000`；
- 合并后的 `open_time` 严格递增、无重复，窗口之间不重叠；
- 每一行记录唯一 `window_id`、`source_month` 与官方 ZIP SHA-256；
- 任一窗口不完整时必须终止，不得生成冻结 manifest。

## 冻结产物

输出目录固定为 `data/backtests/round23_premium_index`。每个标的生成：

1. UTF-8 CSV，字段固定为：
   - `window_id`；
   - `open_time`；
   - `premium_close`；
   - `source_month`；
   - `source_granularity`，只能为 `monthly` 或 `daily`；
   - `source_period`，记录 `YYYY-MM` 或 `YYYY-MM-DD`；
   - `source_zip_sha256`；
2. manifest JSON，至少记录：
   - schema version、数据协议 SHA-256、symbol、市场、数据类型与周期；
   - 两个授权月段与明确排除的 `2023-07..2024-07`；
   - 291 个保留窗口的边界、行数与完整性状态，以及两个固定排除窗口和缺失分钟数；
   - CSV SHA-256、总行数、首尾时间、重复数量；
   - 65 个源月度归档的 URL、ZIP SHA-256、实际行数、缺口范围、保留行数、表头状态、微秒规范化数量与 checksum 状态；
   - 所有被请求日档的 URL、HTTP 状态、ZIP SHA-256、实际行数、缺口范围、补回行数与 checksum 状态；
   - 月档缺失、日档补回、最终仍缺的分钟汇总；
   - `official_monthly_checksums_verified: true`、`available_daily_checksums_verified: true`、`source_gaps_recorded: true`、`authorized_windows_complete: true`。

正式基差收敛上界必须在另一个结果冻结协议中写入两个 premium manifest 的实际 SHA-256，并同时锁定方向、入场、离场、资金费、费用和门槛。当前协议不得产生任何盈利结论，不得修改生产默认值，`direction_mode` 保持 `NEUTRAL`。
