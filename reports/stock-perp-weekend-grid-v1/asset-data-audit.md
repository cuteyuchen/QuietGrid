# Stock Perpetual 数据质量审计

- Manifest：`E:\project\QuietGrid\reports\stock-perp-weekend-grid-v1\asset-data-manifest.json`
- 审计时间：`2026-07-24T19:03:09.504756+00:00`
- 缺口处理：观察期/交易期缺口标记为不可交易，未做线性插值。

| Symbol | Tier | Status | 1m rows | 1m gaps | Missing minutes | Future/unclosed | Funding | Mark | Premium | AggTrades |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| `AMZNUSDT` | TIER_A_CORE | **PASS** | 236720 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |
| `COINUSDT` | TIER_A_CORE | **PASS** | 236700 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |
| `CRCLUSDT` | TIER_A_CORE | **PASS** | 236710 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |
| `HOODUSDT` | TIER_A_CORE | **PASS** | 246795 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |
| `INTCUSDT` | TIER_A_CORE | **PASS** | 246810 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |
| `MSTRUSDT` | TIER_A_CORE | **PASS** | 236730 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |
| `PLTRUSDT` | TIER_A_CORE | **PASS** | 236690 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |
| `TSLAUSDT` | TIER_A_CORE | **PASS** | 254010 | 0 | 0 | 0/0 | PASS | PASS | PASS | PASS |

Funding 只在真实结算时间进入后续回测；本审计不把费率摊入 K 线。
所有结果均保留 `data_previously_viewed` 语义，不将本轮数据宣称为未查看 Final OOS。
