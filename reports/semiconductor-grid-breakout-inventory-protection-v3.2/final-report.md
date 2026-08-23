# QuietGrid Semiconductor Grid v3.2
# Post-Entry Regime Shift & Breakout Inventory Protection Study

## Research identity
Base commit: `abe6ebf4474ac0362707fa631d29e35a81cc81b4`; mode: `NEW_POST_HOC_RESEARCH`; current 2/8 is `RESEARCH_VALIDATION_EXPOSED`, not new Forward OOS.

## 24 Answers
1. CONTROL parity: `PASS_CONTROL_PARITY`; current replay mismatches=0.
2. CONTROL paired grid edge across exposed rows: 746.505204 USDT; edge remains present where paired pnl is positive.
3. Main problem remains inventory tail: control inventory drag=960.246565 USDT.
4. Lowest false-breakout rate among detectors that emitted signals: `D3` at 80.00%. D1 emitted 0 signals and therefore its apparent 0% rate is abstention, not superior classification; D2=83.33%, D3=80.00%.
5. Best TRUE_BREAKOUT identification: `D2` with 36 TRUE_BREAKOUT signals versus D1=0 and D3=18; D2 also detects the current SNDK tail event.
6. Reduce-only: effective only in the D2 region; D2-R1 tail reduction=44.22%, grid-edge retention=94.57%. It still fails the false-breakout gate.
7. Partial flatten 25%: effective only with D2; D2-R2 tail reduction=57.87%, grid-edge retention=95.65%. It is not candidate-qualified.
8. Partial flatten 50%: strongest Phase 1 tail result at D2-R3; tail reduction=71.26%, grid-edge retention=93.92%, but false-breakout rate=83.33%, so it is rejected.
9. Full flatten was not run because Phase 1 is the registered gate for Phase 2.
10. Inventory age rules show no separately demonstrated incremental value in Phase 1; AGE_SOFT=360 and AGE_HARD=720 remain secondary states, and age alone never flattens.
11. Conditional profit lock has no evaluated incremental value because Phase 3 was correctly not run without a qualified Phase 1/2 region.
12. Highest grid-edge retention among protection profiles: D3-R2 at 98.25%; CONTROL remains 100% by definition.
13. Highest inventory-tail reduction profile: D2-R3.
14. Lowest max drawdown profile: D2-R3.
15. Lowest worst-window loss profile: D2-R3.
16. Stable protection region: `NO`; eligible=NONE.
17. This is not an accepted isolated optimum: the D2 family improves in one direction, but every D2 point fails the false-breakout gate, leaving no eligible neighboring pair.
18. Current SNDK replay CONTROL net=-4.034062; no recommended candidate exists. Research-best D2-R3 net=0.409785, but it is not freeze-qualified.
19. D2-R3 improvement primarily comes from inventory-tail reduction: exposed-history tail reduction=71.26% with grid-edge retention=93.92%; current replay tail reduction=80.00% with retention=100.00%.
20. Material cross-window false-breakout harm remains for D2-R3: missed grid pnl=118.300818 USDT and false-breakout exit cost=38.310643 USDT. No symbol is removed.
21. Under EXECUTION_STRESS, aggregate CONTROL net=-137.081751 and D2-R3 net=55.512431; the pnl improvement survives, but the detector still fails its false-breakout gate.
22. New Forward OOS candidate freeze: `NO`.
23. Recommended candidate: `NONE`; new candidate Forward OOS remains `0/8`.
24. Failure/decision reason: `REJECT_NO_STABLE_PROTECTION_REGION`; all signaling detector families exceed the registered 35% false-breakout ceiling, so no eligible stable protection region exists.

## Freeze and safety
CONTROL remains 31111-NEUTRAL. No v2.9 ledger, candidate freeze, production Controller, leverage, symbol universe, or automatic trading setting was changed.
The original 31111 Forward OOS remains independent and continues its own 2/8 to 8/8 sequence.

## Outputs
All requested CSV/JSON artifacts are in this directory. Phase 2/3 files explicitly record that their gates were not reached.
