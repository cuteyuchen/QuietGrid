# QuietGrid v4.1 Proxy Network Validation After DB Fix

Generated: 2026-08-29T02:22:00+08:00

## Result

`BLOCKED_V41_NETWORK_VALIDATION`

Stopped after PAPER_BASELINE smoke due to a newly observed runtime defect:
`BLOCKED_BY_RUNTIME_DEFECT_BOOKTICKER_STREAM`.

## Egress

- `PASS_PROXY_EGRESS_QUALIFICATION`
- `STABLE_PROXY_EGRESS`
- Egress IP: `141.148.210.128` (AS31898 Oracle, Mumbai)
- Samples: 3 / 3 identical
- Proxy: Clash Verge / Mihomo pinned node, TUN active
- Binance requests before probe: 0

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

- Duration: 600.012 seconds
- Events processed: 13800
- Controller ticks: 36
- Orders / fills / cancels / stops / funding: 0
- Reconcile: `RECONCILED`
- Production private API calls: 0
- Real orders: 0

## New Runtime Defect

The smoke consumed 13800 real production TRADE events but zero BOOK_TICKER
events. A direct 15-second real Production WebSocket sample on the same stream
URL received 7168 bookTicker messages, so the messages were present on the
network. A direct `BinancePublicTradeStream` run received only TRADE events.

This is a runtime stream parsing defect, not a network or strategy issue.

## Not Started

- PAPER_CONSERVATIVE smoke
- WebSocket disconnect / reconnect
- Process restart recovery
- Bounded soak

## Boundaries

- `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- `NOT_EVALUATED_FOR_PROFITABILITY`
- No code modified after the DB hotfix
