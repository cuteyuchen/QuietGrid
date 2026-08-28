# QuietGrid v4.1 Corrective Final Report

- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Base SHA: `1e1d45f816beb92d51df6bbcbbdc753eb12105fc`
- Final code SHA: `8d4b7f3e0d50d5c4e9c705b211625514a57f7cda`
- Active remote branches: `master`, `codex/semiconductor-grid-forward-oos-monitor-v2.9.1`, `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Freeze integrity: `PASS_FREEZE_INTEGRITY`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Forward OOS: `2 / 8`
- Frozen runtime parity: `PASS`
- Effective strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- 31111 effective semantics: `A3 / B1 / C1 / D1 / E1` mapped from frozen candidate
- Real controller integration: `PASS`
- Market event identity: `PASS`
- Trade stream type: `@trade`, native `t` id
- Book stream: `@bookTicker`
- Freshness model: `PASS`, independent trade/book freshness
- STOP_MARKET: `PASS`
- Reduce-only: `PASS`
- Force-flat race: `PASS`
- Queue: `PASS`
- Maker pricing: `PASS`
- Session integration: `PASS`
- Production Public probe: `PRODUCTION_PUBLIC_PROBE_INCOMPLETE` (Binance HTTP 451)
- Per-symbol capability: `ERROR_FATAL` for all four, probe did not complete
- Baseline smoke: `SKIPPED_NOT_PASS`
- Conservative smoke: `SKIPPED_NOT_PASS`
- Bounded soak: `NOT_STARTED`
- Profitability: `NOT_EVALUATED_FOR_PROFITABILITY`
- Overall: `PARTIAL_V41_ENGINEERING_VALIDATION`

The engineering blockers are fixed and locally validated. Long shadow observation must wait until a complete Production Public probe is possible from this environment.
