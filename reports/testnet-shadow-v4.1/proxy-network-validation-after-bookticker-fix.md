# QuietGrid v4.1 Proxy Network Validation After BookTicker Fix

Generated: 2026-08-29T08:44:00+08:00

## Result

`PASS_V41_NETWORK_VALIDATION`

## Egress

- `PASS_PROXY_EGRESS_QUALIFICATION`
- `STABLE_PROXY_EGRESS`
- Egress IP: `141.148.210.128` (AS31898 Oracle, Mumbai)
- Samples: 3 / 3 identical
- Proxy: Clash Verge / Mihomo pinned node, TUN active

## Production Public Probe

- `PASS_PRODUCTION_PUBLIC_PROBE`
- `PRODUCTION_PUBLIC_TRADFI_SUPPORTED`
- `live_shadow_gate = PASS`
- SNDKUSDT: `SUPPORTED`
- MUUSDT: `SUPPORTED`
- SOXLUSDT: `SUPPORTED`
- SKHYNIXUSDT: `RESEARCH_ONLY`
- Trade WebSocket: `SUPPORTED`
- BookTicker WebSocket: `SUPPORTED`
- 418 / 429: 0

## PAPER_BASELINE Smoke

- Duration: 600.007 seconds
- TRADE: 2818
- BOOK_TICKER: 27786
- Controller ticks: 50
- Orders / fills: 0
- Reconcile: `RECONCILED`

## PAPER_CONSERVATIVE Smoke

- Duration: 605.014 seconds
- TRADE: 3369
- BOOK_TICKER: 18090
- Controller ticks: 57
- Orders / fills: 0
- Reconcile: `RECONCILED`

## WebSocket Recovery

`PASS_PUBLIC_STREAM_RECOVERY`

- Forced transport abort while real Production WS was active
- Reconnect count: 1
- REST recovery executed
- During the no-data window, book was stale and risk-increasing maker order was
  rejected with `BOOK_DATA_STALE`
- After recovery, first new WS events: BOOK_TICKER and TRADE, independently
- Reconcile: `RECONCILED`

## Process Restart

`PASS_SHADOW_PROCESS_RESTART_RECOVERY`

- Kept the Conservative SQLite, journal, and state
- Pre-seeded paper position (MUUSDT qty 1) and one open paper order
- After restart: position and open order unchanged
- Core + shadow schema present
- Reconcile: `RECONCILED`

## Bounded Soak

`PASS_BOUNDED_SHADOW_SOAK`

- Duration: 1800.015 seconds
- TRADE: 6327
- BOOK_TICKER: 70004
- Controller ticks: 147
- Estimated reconnects: 7
- Reconcile: `RECONCILED`
- Both streams continuously grew across the full window

## Safety

- Production Private API calls: 0
- Signed requests: 0
- Real orders: 0
- Candidate SHA unchanged
- Effective Strategy SHA unchanged
- Forward OOS: `2 / 8`

## Boundaries

- `READY_FOR_LONG_SHADOW_OBSERVATION`
- `NOT_EVALUATED_FOR_PROFITABILITY`
