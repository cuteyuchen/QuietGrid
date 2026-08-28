# QuietGrid v4.1 Network Validation Final Report

Generated: 2026-08-29T02:25:00+08:00

## Final Status

`BLOCKED_V41_NETWORK_VALIDATION`

`NOT_READY_FOR_LONG_SHADOW_OBSERVATION`

`NOT_EVALUATED_FOR_PROFITABILITY`

## Completed Gates

- `PASS_PRE_FLIGHT_REGRESSION` (874 passed after DB hotfix)
- `PASS_RUNTIME_DB_SCHEMA_INITIALIZATION`
- `PASS_FRESH_DB_REAL_CONTROLLER_FIRST_EVENT`
- `PASS_RUNTIME_DB_RESTART_IDEMPOTENCY`
- `PASS_BASELINE_CONSERVATIVE_DB_ISOLATION`
- `PASS_FREEZE_INTEGRITY`
- `PASS_FROZEN_31111_RUNTIME_PARITY`
- `PASS_PROXY_EGRESS_QUALIFICATION`
- `PASS_PRODUCTION_PUBLIC_PROBE`
- `PASS_NO_PRODUCTION_PRIVATE_API`
- `PASS_NO_REAL_ORDER_MUTATION`

## Blocking Defect

`FAIL_PUBLIC_BASELINE_SMOKE`

`BLOCKED_BY_RUNTIME_DEFECT_BOOKTICKER_STREAM`

The runtime silently discards all Production Public BOOK_TICKER messages because
`"bookTicker".lower()` is compared against `"bookTicker"`. The 10-minute smoke
therefore had no real book freshness path despite the network delivering book
messages.

## Not Run

- PAPER_CONSERVATIVE smoke
- WebSocket disconnect / reconnect
- Process restart recovery
- Bounded soak

## Safety

- Production Private API calls: 0
- Signed requests: 0
- Real orders: 0
- Candidate SHA unchanged
- Effective Strategy SHA unchanged
- Forward OOS: `2 / 8`
