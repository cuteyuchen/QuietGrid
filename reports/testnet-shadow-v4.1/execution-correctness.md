# v4.1 Execution Correctness

- STOP_MARKET: explicit STOP_PENDING -> STOP_TRIGGERED -> FILLED path
- reduce-only: exposure capped at current position; no flip
- force-flat: latched, episode-scoped, bounded retry
- maker price: resting limit price
- touch != fill: queue, latency, eligibility, and participation required
- reconcile: RECONCILED
