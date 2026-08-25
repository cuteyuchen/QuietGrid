# QuietGrid Semiconductor Grid v3.5

## Inventory Tail Formation & Early Warning Study

All observations are `RESEARCH_VALIDATION_EXPOSED`. The canonical formation source is `31111-NEUTRAL CONTROL`; S1/S2/S3 paths are not used. `SMALL_EVENT_SAMPLE` and `EXPLORATORY_SMALL_SAMPLE` apply throughout.

## Direct Answers

1. v3.4 parity: `PASS_V34_EVENT_PARITY`; CONTROL=`PASS_CONTROL_PARITY`, v3.3=`PASS_V33_PARITY`.
2. Unique events: 12.
3. TRUE: 2.
4. FALSE: 10.
5. Typical TRUE path: GRID_HEADROOM_COMPRESSION, LATE_SESSION_TIME_PRESSURE, INVENTORY_BUILD; this is descriptive, not validated.
6. FALSE self-healing mechanisms: LOW_DIRECTIONAL_PERSISTENCE=8, FAST_REVERSAL=6, EARLY_SESSION_RECOVERY=5, VOLATILITY_MEAN_REVERSION=5, SMALL_GROSS_EXPOSURE=4, OTHER=1, LOW_INVENTORY_AGE=1.
7. Earliest descriptive TRUE/FALSE group separation: `T-60_DESCRIPTIVE_ONLY`; no tested simple rule both preserves 2/2 TRUE and materially reduces FALSE alerts.
8. Strongest descriptive feature by requested checkpoint: T-60=minutes_to_force_close (AUROC=1.000); T-30=inventory_build_rate_10m (AUROC=1.000); T-20=ATR_pct (AUROC=1.000); T-10=minutes_to_force_close (AUROC=1.000); T-5=minutes_to_force_close (AUROC=1.000).
9. Inventory build: measured in notional and utilization rates; see feature-main-effects.csv.
10. One-sided fill cascade: measured, but its independent support is limited by sparse fills and two TRUE events.
11. Reversal failure: measured by reversal ratios/counts and paired-fill conversion.
12. Directional persistence: measured by returns, slopes, efficiency, same-direction ratio, and streaks.
13. Volatility expansion: measured by ATR, realized volatility, short/long ratio, and range expansion.
14. Grid headroom: measured in remaining adverse grid levels and boundary distance.
15. Time-to-force-close: measured for every minute and checkpoint.
16. TAIL_IRREVERSIBILITY_POINT: `NO_RELIABLE_IRREVERSIBILITY_POINT` because N_TRUE=2 cannot estimate recovery probability reliably.
17. Best tested simple rule (status `NOT_PROMISING`, not an accepted hypothesis): `EW2_FILL_HEADROOM: one_sided_fill_ratio_10m >= 0.80 AND grid_headroom_remaining <= 2`.
18. TRUE recall: 2/2; FN=0.
19. FALSE alerts: 10 -> 9.
20. Mean lead vs D2: 122.5 minutes.
21. Fraction of tail remaining at warning among alerted TRUE events (n=2): 91.40%. Both TRUE events were alerted by the best tested rule.
22. Cross-symbol stability: `INSUFFICIENT_CROSS_SYMBOL_TRUE_SUPPORT`; TRUE events occur only in MU and SNDK.
23. SOXL: 2 FALSE and 0 TRUE events; it is reported separately as leveraged ETF-linked and cannot validate a shared rule.
24. Local threshold status: `NO_PROMISING_RULE_FOR_LOCAL_ROBUSTNESS`.
25. Sample gate: `SMALL_EVENT_SAMPLE` because TRUE < 5 and total events < 30; it is insufficient for validation or reliable LOEO.
26. Recommended next stage: `NONE` unless a future protocol expands independent TRUE events; no automatic v3.6 action design is authorized.
27. New Forward OOS candidate: `NONE`.
28. Production config: unchanged; auto-entry remains OFF; economic leverage remains 1x.
29. Pytest: `881 passed, 3 warnings`; compileall and git diff --check both exited 0. See `tests.txt`.
30. Conclusion code: `REJECT_FALSE_POSITIVE_NOT_REDUCED`.

## Interpretation

Because there are only two independent TRUE market events, rankings, AUROC/AUPRC, exact permutations, and Fisher results are descriptive. Scenario x seed replicas remain execution diagnostics and never enter sample counts. No detector action or trading-path counterfactual is implemented.
