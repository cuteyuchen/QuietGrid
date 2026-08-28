# QuietGrid v4.1 Probe Rate Limit Fail Fast Validation

- Result: `PASS_PROBE_RATE_LIMIT_FAIL_FAST`
- Generated at: `2026-08-28T13:58:00+08:00`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Previous HEAD: `7df0f5c99885950144f297a28cfdf4609070c768`
- Code fix commit: `55494ba78b061892125f2db9b2ca9bdd10aa2a40`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`
- Full regression: `868 passed`
- Compileall: `PASS`
- `git diff --check`: `PASS`

## Changes

- First HTTP 418/429 in Production Public Probe now stops all remaining REST requests immediately.
- Current symbol endpoint loop stops; next symbols are not started.
- WebSocket capability probe is skipped after REST rate limit and recorded as `SKIPPED_DUE_TO_RATE_LIMIT`.
- Rate-limit details persist to `reports/testnet-shadow-v4.1/production-public-cooldown.json`.
- Active cooldown returns `PROBE_COOLDOWN_ACTIVE` before any Binance request.
- SKHYNIXUSDT remains `RESEARCH_ONLY` and does not block the live shadow gate.

## Cooldown

- Detected at: `2026-08-28T05:45:22.202375+00:00`
- Status: `418`
- Retry-After: `13881` seconds
- Cooldown until: `2026-08-28T09:36:43.202375+00:00` UTC
- Local expiry: `2026-08-28T17:36:43+08:00`

## Conclusion

- Code fix: `PASS_PROBE_RATE_LIMIT_FAIL_FAST`
- Overall network validation: `PARTIAL_V41_NETWORK_VALIDATION`
- Long shadow observation: `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- Profitability: `NOT_EVALUATED_FOR_PROFITABILITY`
