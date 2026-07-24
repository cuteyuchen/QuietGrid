# Stock Perpetual 数据质量审计

- Manifest：`E:\project\QuietGrid\reports\stock-perp-weekend-grid-v1\asset-data-manifest.json`
- 审计时间：`2026-07-24T18:58:01.028727+00:00`
- 缺口处理：观察期/交易期缺口标记为不可交易，未做线性插值。

| Symbol | Tier | Status | 1m rows | 1m gaps | Missing minutes | Future/unclosed | Funding | Mark | Premium | AggTrades |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| `AMZNUSDT` | TIER_A_CORE | **DATA_INVALID** | 236720 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |
| `COINUSDT` | TIER_A_CORE | **DATA_INVALID** | 236700 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |
| `CRCLUSDT` | TIER_A_CORE | **DATA_INVALID** | 236710 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |
| `HOODUSDT` | TIER_A_CORE | **DATA_INVALID** | 246795 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |
| `INTCUSDT` | TIER_A_CORE | **DATA_INVALID** | 246810 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |
| `MSTRUSDT` | TIER_A_CORE | **DATA_INVALID** | 236730 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |
| `PLTRUSDT` | TIER_A_CORE | **DATA_INVALID** | 236690 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |
| `TSLAUSDT` | TIER_A_CORE | **DATA_INVALID** | 254010 | 0 | 0 | 0/0 | PASS | PASS | DATA_INVALID | PASS |

Funding 只在真实结算时间进入后续回测；本审计不把费率摊入 K 线。
所有结果均保留 `data_previously_viewed` 语义，不将本轮数据宣称为未查看 Final OOS。
