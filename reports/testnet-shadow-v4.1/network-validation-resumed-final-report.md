# QuietGrid v4.1 Network Validation Resumed Final Report

- Generated at: `2026-08-28T12:07:40+08:00`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Pre-fix HEAD: `c918dbe1a674c3620a6f1712721b137d430ebd7b`
- Hotfix SHA: `a407f9d67926b2603f9b0db25ab828199fe21f66`
- Worktree after hotfix: clean
- Compileall: `PASS`
- Tests: `861 passed`
- `git diff --check`: `PASS`
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Strategy SHA drift: `false`
- Forward OOS: `2 / 8`
- Freeze tag: `semiconductor-grid-forward-oos-v2.9-freeze`

## Hotfix

- `MIN_NOTIONAL.notional` parses to `min_notional=5.0`
- `NOTIONAL.minNotional` parses to `min_notional=7.5`
- Legacy `MIN_NOTIONAL.minNotional` fallback supported
- Present but invalid `MIN_NOTIONAL`/`NOTIONAL` raises `PublicMarketRuleParseError` instead of returning `0.0`
- Missing notional filter returns `min_notional=0.0`, `min_notional_available=false`
- Rule metadata: `min_notional_filter_type`, `min_notional_source_field`, `min_notional_available`
- `exchangeInfo` cached per `ProductionPublicMarketData` instance
- Probe pacing: `0.4s`, one exchangeInfo plan, no programmatic node rotation

## Production Public Probe

Status: `PRODUCTION_PUBLIC_PROBE_INCOMPLETE_RATE_LIMITED`

- HTTP status: `418`
- Retry-After: `3894` seconds
- Rate-limit stage: `server_time`
- SNDKUSDT: `PARTIAL_RATE_LIMITED` / not confirmed / not unsupported
- MUUSDT: `PARTIAL_RATE_LIMITED` / not confirmed / not unsupported
- SOXLUSDT: `PARTIAL_RATE_LIMITED` / not confirmed / not unsupported
- SKHYNIXUSDT: `PARTIAL_RATE_LIMITED` / `RESEARCH_ONLY` / not confirmed

Per validation rules, the run stops here. No smoke, reconnect, restart, or soak was started.

## Safety

- Production private API calls: `0`
- Real orders: `0`
- Credentials used: none
- Programmatic node rotation: none

## Conclusion

- Overall: `PARTIAL_V41_NETWORK_VALIDATION`
- Long shadow observation: `NOT_READY_FOR_LONG_SHADOW_OBSERVATION`
- Profitability: `NOT_EVALUATED_FOR_PROFITABILITY`
