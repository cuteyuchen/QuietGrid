# QuietGrid v4.1 Network Validation Final Report

- Generated at: `2026-08-28T11:31:03+08:00`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- HEAD: `5f7c58c08717039a13e45a4483185a1d9ce6e9ef`
- Worktree: clean before probe
- Tests: `844 passed`
- Compileall: project source `PASS`; bare `compileall .` also scans `.venv` and reports unrelated syntax errors inside `ccxt` static dependencies
- `git diff --check`: `PASS`
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Freeze tag: `semiconductor-grid-forward-oos-v2.9-freeze`
- Semantics: `A3 / B1 / C1 / D1 / E1`, `NEUTRAL`, `1x`, `C1 disabled`
- Live allowlist intent: `SNDKUSDT`, `MUUSDT`, `SOXLUSDT`
- Research only: `SKHYNIXUSDT`

## Production Public Probe

Status: `PRODUCTION_PUBLIC_PROBE_INCOMPLETE`

- SNDKUSDT: `ERROR_FATAL` / no capability evidence
- MUUSDT: `ERROR_FATAL` / no capability evidence
- SOXLUSDT: `ERROR_FATAL` / no capability evidence
- SKHYNIXUSDT: `ERROR_FATAL` / no capability evidence

Fatal reason: `HTTP 451` from `https://fapi.binance.com/fapi/v1/exchangeInfo`. Per validation rules, this is recorded as incomplete and not as symbol unsupported. No proxy, VPN, tunnel, host/DNS override, third-party relay, credential, or private API call was used.

## Later Stages

- Paper Baseline public smoke: `SKIPPED` / `NOT_PASS`
- Paper Conservative public smoke: `SKIPPED` / `NOT_PASS`
- WebSocket disconnect/reconnect: `NOT_RUN`
- Process restart recovery: `NOT_RUN`
- Bounded shadow soak: `NOT_RUN`

## Safety

- Production private API: `DISABLED`
- Signed requests: `DISABLED`
- Real order mutation: `false`
- Paper order mutation: `false`
- Production credentials used: none

## Conclusion

- Overall: `BLOCKED_V41_NETWORK_VALIDATION`
- Long shadow observation: `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- Profitability: `NOT_EVALUATED_FOR_PROFITABILITY`
