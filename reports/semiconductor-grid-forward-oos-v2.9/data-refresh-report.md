# Semiconductor Grid v2.9.1 Forward OOS Data Refresh Report

1. 数据抓取时间：`2026-08-22T17:34:58.690630+00:00`。
2. 各标的旧/新数据截止与增量如下。

| Symbol | Old latest bar | New latest bar | New 1m bars | Old latest funding | New latest funding | New funding events |
|---|---|---|---:|---|---|---:|
| SNDKUSDT | 2026-07-25T08:34:00+00:00 | 2026-08-22T17:33:00+00:00 | 40859 | 2026-07-25T08:00:00+00:00 | 2026-08-22T16:00:00.002000+00:00 | 85 |
| MUUSDT | 2026-07-25T08:34:00+00:00 | 2026-08-22T17:33:00+00:00 | 40859 | 2026-07-25T08:00:00+00:00 | 2026-08-22T16:00:00.002000+00:00 | 85 |
| SOXLUSDT | 2026-07-25T08:34:00+00:00 | 2026-08-22T17:33:00+00:00 | 40859 | 2026-07-25T08:00:00+00:00 | 2026-08-22T16:00:00.002000+00:00 | 85 |
| SKHYNIXUSDT | 2026-07-25T08:34:00+00:00 | 2026-08-22T17:33:00+00:00 | 40859 | 2026-07-25T08:00:00+00:00 | 2026-08-22T16:00:00.002000+00:00 | 170 |

3. Historical data revision：`NONE`。
4. Exposure cutoff 仍固定为：`2026-08-08T20:45:23.438783+00:00`。
5. cutoff 后理论 closed-market windows：`4`。
6. 其中符合 Forward OOS 的完整 portfolio windows：`2`。
7. 本次 append 的主候选 ledger rows：`72`。
8. 当前 Forward OOS：`2/8`。
9. PRIMARY net PnL：`-4.03406218985135`。
10. EXECUTION_STRESS net PnL：`-4.805861863343044`。
11. MAKER_PROMO_OFF net PnL：`-4.236835439181509`。

## Symbol Breakdown

| Symbol | Net PnL | Profit factor | Positive window ratio | Max drawdown | Inventory drag | Complete windows |
|---|---:|---:|---:|---:|---:|---:|
| MUUSDT | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1 |
| SKHYNIXUSDT | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1 |
| SNDKUSDT | -4.03406218985135 | 0.0 | 0.0 | 6.3168050875581745 | 5.700604726512897 | 1 |
| SOXLUSDT | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1 |

- PRIMARY 最大回撤：`6.3168050875581745` / `0.012633610175116348`。
- PRIMARY inventory drag ratio：`2.89435069529515`。
- 规则观察：`RULES_UNCHANGED`。
- 数据质量：`OK`；未发现 duplicate candles、gaps、invalid bars 或 historical revision。
- 自动交易仍关闭：`True`。
- 当前结论：`INSUFFICIENT_FORWARD_OOS`。

## Latest Execution / Idempotency

- 最近执行时间：`2026-08-22T17:34:58.690630+00:00`。
- 新增 1m bars：`0`。
- 新增 funding events：`0`。
- 新完整窗口：`0`。
- 新增 ledger rows：`0`。
- Ledger SHA（执行前/后）：`2c034bc92bda12da193faf4ef5e2edabb624976a69edfddfa7b3b551190ed814` / `2c034bc92bda12da193faf4ef5e2edabb624976a69edfddfa7b3b551190ed814`。
- 历史前缀未变化：`True`。
