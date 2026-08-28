# QuietGrid v4.1 Runtime Defect: MIN_NOTIONAL key mismatch

Status: `BLOCKED_BY_RUNTIME_DEFECT`

- Generated at: `2026-08-28T11:43:03+08:00`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Frozen baseline commit: `5f7c58c08717039a13e45a4483185a1d9ce6e9ef`
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Network target: Binance Futures Production Public
- Probe classification: `PRODUCTION_PUBLIC_TRADFI_PARTIAL`

## Observed behavior

The frozen production public probe reached Binance Futures Production Public:

- `GET /fapi/v1/time`: `SUPPORTED`
- Public WebSocket `@trade` and `@bookTicker`: `SUPPORTED`
- SNDKUSDT, MUUSDT, SOXLUSDT, SKHYNIXUSDT: exists, `TRADING`, `TRADIFI_PERPETUAL`
- `min_notional` check: `false` for all four symbols
- Some REST checks returned transient HTTP 418 during the probe burst
- Slow independent REST rechecks of the 418 endpoints returned HTTP 200

## Root cause

Raw Binance `exchangeInfo` contains:

```json
{"filterType": "MIN_NOTIONAL", "notional": "5"}
```

for each of SNDKUSDT, MUUSDT, SOXLUSDT, and SKHYNIXUSDT. The frozen `exchange/public_market.py` `get_symbol_rules()` reads only `minNotional`:

```python
"min_notional": float(notional.get("minNotional", 0) or 0),
```

Actual observed output from the frozen method:

```text
min_notional: 0.0
```

## Runtime impact

`strategy/controller.py` and `strategy/grid_engine.py` consume `rules.get("min_notional", 0.0)`. A value of `0.0` is treated as no min-notional constraint. Under Production Public data, the frozen runtime would therefore not enforce Binance's 5 USDT `MIN_NOTIONAL`, which changes shadow sizing and validation semantics relative to the attested 31111-NEUTRAL runtime.

## Minimal reproduction

1. `GET https://fapi.binance.com/fapi/v1/exchangeInfo`
2. Inspect `MIN_NOTIONAL` for any of SNDKUSDT, MUUSDT, SOXLUSDT, SKHYNIXUSDT.
3. Call `ProductionPublicMarketData().get_symbol_rules("SNDKUSDT")`.
4. Observed result: `min_notional == 0.0`.

## Decision

No code was modified. Per frozen-validation rules, the run is marked `BLOCKED_BY_RUNTIME_DEFECT`. Baseline smoke, conservative smoke, reconnect, restart, and bounded soak were not started.
