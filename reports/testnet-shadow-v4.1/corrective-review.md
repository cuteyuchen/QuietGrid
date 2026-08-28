# QuietGrid v4.1 Corrective Review

Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`

Base SHA: `1e1d45f816beb92d51df6bbcbbdc753eb12105fc`

Final code SHA: `8d4b7f3e0d50d5c4e9c705b211625514a57f7cda`

## Engineering blockers resolved

- `ControllerConfig` field ordering and risk wiring now accept a disabled C1 profit protection mode.
- A frozen artifact compiler is the single source for the effective controller config, with a deterministic effective strategy SHA.
- Market event identity now includes source, symbol, stream type, and event id, so same-ms, duplicate, cross-symbol, and cross-stream events cannot collide.
- Trade and book freshness are tracked separately; a stale book blocks new maker orders.
- Force-flat is an event-driven state machine with cancel latency, position re-read after cancel terminal, bounded retries, episode scoping, and restart persistence.
- Zero-position stop orders expire instead of filling.
- Runtime manifests are written to an injectable report directory so validation runs do not overwrite repository evidence.
- Production probe classification is `PRODUCTION_PUBLIC_PROBE_INCOMPLETE` when the probe cannot complete, never `UNSUPPORTED`.

## Frozen protocol

- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Freeze tag: `semiconductor-grid-forward-oos-v2.9-freeze`
- Forward OOS ledger: `2 / 8` unchanged
- `SNDKUSDT`, `MUUSDT`, `SOXLUSDT`: `SHADOW_ELIGIBLE`
- `SKHYNIXUSDT`: `RESEARCH_ONLY` per frozen `live_symbols`

## Validation

- `pytest -q`: 844 passed
- Production public probe: completed, HTTP 451 from Binance, classified `PRODUCTION_PUBLIC_PROBE_INCOMPLETE`
- Baseline/conservative public smoke: skipped because the public capability probe is incomplete
- Profitability: not evaluated
