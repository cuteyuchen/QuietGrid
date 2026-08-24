# QuietGrid Semiconductor Grid v3.4
# Staged De-Risking & Early Inventory Defense Study

Base commit: `3c15782da79dab306154d02653ca59201370b76f`; mode: `POST_HOC_RESEARCH`; all reused history and current replay are `RESEARCH_VALIDATION_EXPOSED`.

## Final Answers
1. CONTROL parity: `PASS_CONTROL_PARITY`.
2. v3.3 parity: `PASS_V33_PARITY` for CONTROL, D2-R3, and S0 = D2-C1-R3-20m.
3. D2 run-level confusion: TP=36, FP=180, FN=0, TN=0.
4. D2 unique-event confusion: TP=2, FP=10, FN=0, TN=0.
5. Unique market events: TRUE=2, FALSE=10, unresolved=0. `SMALL_EVENT_SAMPLE_WARNING` applies because there are only 2 unique TRUE events.
6. Tested staged profiles: `S0, S1, S2, S3`; Stage 1=D2 and Stage 2=`FROZEN_CONFIRMATION_REFERENCE C1-20m`.
7. S0 pre-confirmation tail fraction=10.598855%.
8. Best staged profile by unique net defense value: `S1`; value=6.210505, efficiency=621.050549.
9. Tail saved before confirmation: S1=0.000000, S2=0.102540, S3=0.256350 USDT.
10. Grid edge retention: S1=98.35%, S2=98.35%, S3=98.35%.
11. Inventory tail reduction: S1=13.43%, S2=13.63%, S3=13.91%.
12. TRUE loss avoided / FALSE defense cost for `S1`: 6.210505 / 0.000000 USDT.
13. CONTROL max drawdown=7.430614; best staged max drawdown=7.430614.
14. CONTROL worst window=-5.843021; best staged worst window=-5.843021.
15. Current SNDK OOS replay PRIMARY_ZERO_MAKER six-seed mean: CONTROL=-4.034062, D2-R3=0.409785, S0=0.380041, S1=0.380041, S2=0.385990, S3=0.394913.
16. EXECUTION_STRESS: CONTROL=-137.081751; `S1`=-101.346310.
17. MAKER_PROMO_OFF: CONTROL=-94.388844; `S1`=-57.641933.
18. Time-split breakdown is recorded for `EXPOSED_EARLY`, `EXPOSED_LATE`, and `CURRENT_OOS_REPLAY`; no symbol was removed from the portfolio.
19. Stable staged-defense region: `NO_STABLE_STAGED_DEFENSE_REGION`; Phase 2=COMPLETED_LIMITED_ROBUSTNESS; isolated action optimum=NO; isolated timing optimum=NO.
20. Recommended Forward OOS candidate: `NONE`; new candidate SHA=`NONE`; any future candidate starts `0/8`.
21. PnL accounting uses a delta-versus-S0 convention for TRUE breakouts, credits S0's embedded full confirmation execution once, and separately charges early and remaining confirmation execution. The audit has zero reconciliation residual.
22. FALSE-breakout recovery releases the soft block at causal C1 rejection; early inventory is never automatically re-levered.
23. Production settings, original 31111 candidate SHA, official v2.9 ledger, startup_auto_entry, capital, leverage, and symbol allowlist remain unchanged.

## Conclusion
`REJECT_INVENTORY_TAIL_NOT_IMPROVED`
