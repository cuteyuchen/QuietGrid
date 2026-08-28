# QuietGrid v4.1 Proxy-Based Production Public Network Validation

Generated: 2026-08-29T01:27:00+08:00

## Result

`BLOCKED_V41_NETWORK_VALIDATION`

Stopped at Stage 3 (PAPER_BASELINE smoke) due to a deterministic runtime defect. No code was modified.

## Identity

- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Base SHA: `d1cdcd736b3f97121a0b003fc0a52163e2f86636`
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`
- Worktree: clean before this validation attempt

## Regression

- Pytest: `868 passed`
- Compileall: PASS
- `git diff --check`: PASS

## Proxy Egress Qualification

- Result: `PASS_PROXY_EGRESS_QUALIFICATION`
- Stability: `STABLE_PROXY_EGRESS`
- Proxy: Clash Verge / Mihomo, system proxy `127.0.0.1:7897`
- Proxy group: `select`, pinned node `印度孟买`
- Rule path for Binance: `MATCH -> 亏本机场 -> 印度孟买`
- TUN: `Meta Tunnel` up with default routes
- Egress IP: `141.148.210.128` (AS31898 Oracle, Mumbai) stable across 3 samples
- `EGRESS_SHARED_STATUS`: `UNKNOWN_OR_POSSIBLY_SHARED`
- `OPERATIONAL_EGRESS_RISK`: `PRESENT`
- Other Binance consumers: none
- Cooldown: expired, evidence preserved

## Production Public Probe

- Result: `PASS_PRODUCTION_PUBLIC_PROBE`
- Classification: `PRODUCTION_PUBLIC_TRADFI_SUPPORTED`
- Live shadow gate: `PASS`
- SNDKUSDT: `SUPPORTED`
- MUUSDT: `SUPPORTED`
- SOXLUSDT: `SUPPORTED`
- SKHYNIXUSDT: `RESEARCH_ONLY`
- Trade WebSocket: `SUPPORTED`
- BookTicker WebSocket: `SUPPORTED`
- 418/429 count: 0

## PAPER_BASELINE Smoke

- Result: `FAIL_PUBLIC_BASELINE_SMOKE`
- Failure: `BLOCKED_BY_RUNTIME_DEFECT`
- Error: `sqlite3.OperationalError: no such table: control_state`
- Trigger: fresh shadow DB, real `TradingController`, first market event
- Events before failure: 4 REST recovery + 1 Trade
- Orders/fills/cancels/stops/funding: 0
- Production private API calls: 0
- Signed requests: 0
- Real orders: 0

The same failure is deterministically reproducible with a single local Trade event and a fresh DB; it does not depend on Binance network state.

## Not Started

PAPER_CONSERVATIVE smoke, WebSocket recovery, process restart recovery, and bounded soak were not started.

## Boundaries

- `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- `NOT_EVALUATED_FOR_PROFITABILITY`
- No server deployment, no 24/7 runtime, no v4.2 work, no 31111 changes.
