# Stock Perpetual 数据冻结审计

- 协议：`docs/codex-stock-perp-weekend-grid-backtest-v2.5.md`
- 冻结时间：`2026-07-24T18:00:16.019000+00:00`
- Git：`1cec0efe54947d5f6a18314b669c2db808f1a21c`
- 分支：`codex/profit-protection-backtest-v2.3`
- `data_previously_viewed`：`True`

所有官方 ZIP 均在写入前校验相邻 `.CHECKSUM`；缺失归档被记录为缺口，未做插值。

| Symbol | Tier | 1m rows | Funding events | Mark rows | Premium rows | Agg rows | Gaps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `AMZNUSDT` | TIER_A_CORE | 236720 | 493 | 235280 | 228080 | 2758342 | 0 |
| `COINUSDT` | TIER_A_CORE | 236700 | 493 | 235260 | 230940 | 9957111 | 0 |
| `CRCLUSDT` | TIER_A_CORE | 236710 | 493 | 235270 | 232390 | 14933692 | 0 |
| `HOODUSDT` | TIER_A_CORE | 246795 | 514 | 245355 | 242475 | 5520978 | 0 |
| `INTCUSDT` | TIER_A_CORE | 246810 | 514 | 245370 | 241050 | 12988085 | 0 |
| `MSTRUSDT` | TIER_A_CORE | 236730 | 493 | 235290 | 229530 | 13035657 | 0 |
| `PLTRUSDT` | TIER_A_CORE | 236690 | 493 | 235250 | 230930 | 3440727 | 0 |
| `TSLAUSDT` | TIER_A_CORE | 254010 | 535 | 252570 | 248250 | 14037852 | 0 |

冻结资产数：`8`；跳过的短样本：`123`。
