# QuietGrid v4.1 Network Validation Final Report

- Generated at: `2026-08-28T11:43:03+08:00`
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

Status: `PRODUCTION_PUBLIC_TRADFI_PARTIAL`

- SNDKUSDT: `PARTIAL` / `TRADING` / `TRADIFI_PERPETUAL`
- MUUSDT: `PARTIAL` / `TRADING` / `TRADIFI_PERPETUAL`
- SOXLUSDT: `PARTIAL` / `TRADING` / `TRADIFI_PERPETUAL`
- SKHYNIXUSDT: `PARTIAL` / `TRADING` / `TRADIFI_PERPETUAL` / `RESEARCH_ONLY`

Server time: `SUPPORTED`. Public WebSocket: `SUPPORTED`, both `@trade` and `@bookTicker` observed. No proxy, VPN, tunnel, host/DNS override, third-party relay, credential, or private API call was used.

## Blocking Defect

Status: `BLOCKED_BY_RUNTIME_DEFECT`

Binance Production `exchangeInfo` returns the `MIN_NOTIONAL` filter as `{"filterType":"MIN_NOTIONAL","notional":"5"}` for all four symbols. Frozen `ProductionPublicMarketData.get_symbol_rules()` reads `minNotional` instead of `notional`, so the frozen runtime observes `min_notional=0.0`. Controller and grid engine code treats `0` as no min-notional constraint, so the Production Public runtime would not apply Binance's 5 USDT minimum notional.

Minimal reproduction and evidence: `reports/testnet-shadow-v4.1/runtime-defect-min-notional-key.md`.

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
- Blocking condition: `BLOCKED_BY_RUNTIME_DEFECT`
- Long shadow observation: `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- Profitability: `NOT_EVALUATED_FOR_PROFITABILITY`
