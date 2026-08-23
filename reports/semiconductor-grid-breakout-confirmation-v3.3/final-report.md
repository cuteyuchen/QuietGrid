# QuietGrid Semiconductor Grid v3.3
# Two-Stage Breakout Confirmation & Detector Precision Study

Base commit: `eb64ed821b09f285041639a1502456d24bb40e2f`; mode: `POST_HOC_RESEARCH`; all reused 2/8 data is `RESEARCH_VALIDATION_EXPOSED`.

## Final Answers
1. v3.2 parity: `PASS_V32_PARITY`; CONTROL and D2-R3 values reproduce the frozen v3.2 artifacts.
2. D2 baseline confusion: TP=36, FP=180, FN=0, TN=0; precision=16.67%, recall=100.00%.
3. The 83.33% D2 false-breakout rate is 180 FALSE_BREAKOUT signals out of 216 D2 signals.
4. TRUE_BREAKOUT sample count is 36.
5. Best confirmation precision: `D2-C1-R3-20m` at 100.00%.
6. Best recall: `D2-C1-R3-20m` at 100.00%.
7. Lowest false-breakout rate: `D2-C1-R3-20m` at 0.00%; zero-signal profiles are not treated as successful.
8. Best F1: `D2-C1-R3-20m` at 1.0000.
9. Largest true-breakout loss avoided: `D2-C1-R3-20m` at 109.745385 USDT.
10. Lowest false-breakout opportunity cost among profiles that actually confirmed events: `D2-C1-R3-20m` at 0.000000 USDT.
11. Best protection efficiency: `D2-C1-R3-20m` at 10974.5385.
12. Shortest non-zero confirmation delay: `D2-C1-R3-10m`; median=10.0m, P90=10.0m.
13. C1-20m reaches 100% precision and recall on this exposed sample, but its inventory-tail reduction is only 13.43% (below the 50% gate); C2/C3-10m retain only 50% recall. No profile passes all gates.
14. Best confirmation-profile grid-edge retention is 98.35%, above the 80% floor.
15. Best v3.3 confirmation-profile inventory-tail reduction is 15.85%; frozen D2-R3 remains the separate 71.26% response reference.
16. CONTROL max drawdown=7.430614; best confirmation profile `D2-C1-R3-20m` max drawdown=7.430614, so the strict DD gate is not met.
17. CONTROL worst window=-5.843021; `D2-C1-R3-20m` worst window=-5.843021, so the strict worst-window gate is not met.
18. Current SNDK replay CONTROL=-4.034062; frozen D2-R3=0.409785; best confirmed-R3=0.380041 (`D2-C1-R3-20m`).
19. Current SNDK TRUE_BREAKOUT recognition: `YES`; `D2-C1-R3-20m` confirms all 18 scenario/seed replay rows and executes R3 once per row.
20. C1-20m avoids all 180 registered D2 false-breakout actions while retaining all 36 TRUE_BREAKOUT events, but it fails the tail-reduction, DD, worst-window, and stability gates.
21. PRIMARY aggregate: CONTROL=-67.685523; `D2-C1-R3-20m`=-30.422490. It improves but remains research-only.
22. EXECUTION_STRESS aggregate: CONTROL=-137.081751; `D2-C1-R3-20m`=-101.346310. It improves but remains negative.
23. MAKER_PROMO_OFF aggregate: CONTROL=-94.388844; `D2-C1-R3-20m`=-57.641933. Production settings remain unchanged.
24. Cross-symbol support is incomplete: `D2-C1-R3-20m` net by symbol is SNDKUSDT=-102.338253, MUUSDT=-193.697236, SOXLUSDT=19.353129, SKHYNIXUSDT=87.271627; TRUE confirmations occur only in MU and SNDK events.
25. Cross-time support is incomplete: EXPOSED_EARLY net=-20.288906 with 0 confirmations; EXPOSED_LATE net=-169.062839 with 18; CURRENT_OOS_REPLAY net=-0.058988 with 18.
26. Stable confirmation region: `NO`.
27. C1-20m is an isolated classifier optimum: C1-10m retains 66.67% false breakouts and C1-30m recall falls to 50%. It is not candidate-qualified because its inventory-tail reduction is only 13.43%.
28. New candidate freeze: `NO`; `recommended_forward_oos_candidate = NONE`.
29. Candidate ID/SHA: `NONE` / `NONE`.
30. Any future candidate would start at `0/8`; current v3.3 has no Forward OOS count.

## Safety
CONTROL remains frozen 31111-NEUTRAL. R3 is frozen at 50% adverse inventory partial flatten plus reduce-only. Profit lock is disabled. No v2.9 ledger, candidate freeze, production controller, leverage, capital, or automatic trading setting was changed.

## Conclusion
`REJECT_NO_STABLE_CONFIRMATION_REGION`
