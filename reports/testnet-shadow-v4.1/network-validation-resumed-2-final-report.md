# QuietGrid v4.1 Network Validation Resumed 2 Final Report

- Generated at: `2026-08-28T13:47:20+08:00`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- HEAD: `e95f18d2d8ba4ef8e324527234a6d03babac4349`
- Hotfix: `a407f9d67926b2603f9b0db25ab828199fe21f66`
- Worktree: clean before probe
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`
- Freeze tag: `semiconductor-grid-forward-oos-v2.9-freeze`

## Health Check

- `GET /fapi/v1/time`: `200`
- Retry-After: none
- Requests: exactly 1

## Production Public Probe

Status: `PRODUCTION_PUBLIC_TRADFI_PARTIAL_RATE_LIMITED`

- Server time: `SUPPORTED`
- Public WebSocket: `SUPPORTED` (`@trade` + `@bookTicker`)
- ExchangeInfo: `SUPPORTED`
- SNDKUSDT: `TRADING` / `TRADIFI_PERPETUAL` / `PARTIAL_RATE_LIMITED`; `depth` returned 418
- MUUSDT: `TRADING` / `TRADIFI_PERPETUAL` / `SUPPORTED`
- SOXLUSDT: `TRADING` / `TRADIFI_PERPETUAL` / `PARTIAL_RATE_LIMITED`; `kline` returned 418
- SKHYNIXUSDT: `TRADING` / `TRADIFI_PERPETUAL` / `SUPPORTED` / `RESEARCH_ONLY`
- min_notional: all four symbols parsed as `5.0`
- Rate-limit stages: `SNDKUSDT:depth` Retry-After `13881s`; `SOXLUSDT:kline` Retry-After `12643s`

Probe gate: `NOT_PASS`. Per validation rules, the run stops here. No smoke, reconnect, restart, or soak was started.

## Safety

- Production private API calls: `0`
- Real orders: `0`
- Credentials used: none
- Programmatic node rotation or proxy: none
- Probe runs: 1; programmatic retries: 0

## Conclusion

- Overall: `PARTIAL_V41_NETWORK_VALIDATION`
- Long shadow observation: `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- Profitability: `NOT_EVALUATED_FOR_PROFITABILITY`
