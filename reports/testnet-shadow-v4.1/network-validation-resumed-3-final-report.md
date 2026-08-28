# QuietGrid v4.1 Network Validation Resumed 3 Final Report

- Generated at: `2026-08-28T17:42:49+08:00`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- HEAD: `838b59d5ed15d3e6b067dd916bb29f210b0d2bbc`
- Probe fail-fast fix: `55494ba78b061892125f2db9b2ca9bdd10aa2a40`
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`

## Production Public Probe

Status: `PRODUCTION_PUBLIC_PROBE_INCOMPLETE_RATE_LIMITED`

- Server time: `SUPPORTED`
- First rate limit: `SNDKUSDT:ticker`
- HTTP status: `418`
- Retry-After: `14732` seconds
- Cooldown until: `2026-08-28T13:47:57.334444+00:00` UTC / `2026-08-28T21:47:57+08:00`
- Rate limit headers: `x-mbx-used-weight-1m=0`, `retry-after=14732`
- SNDKUSDT: `PARTIAL_RATE_LIMITED` / not confirmed
- MUUSDT: `NOT_RUN_DUE_TO_RATE_LIMIT`
- SOXLUSDT: `NOT_RUN_DUE_TO_RATE_LIMIT`
- SKHYNIXUSDT: `NOT_RUN_DUE_TO_RATE_LIMIT` / `RESEARCH_ONLY`
- WebSocket: `SKIPPED_DUE_TO_RATE_LIMIT`

Fail-fast behavior worked as designed: the probe stopped after the first 418 and did not run the remaining symbols or WebSocket probe.

Probe gate: `NOT_PASS`. No smoke, reconnect, restart, or soak was started.

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
