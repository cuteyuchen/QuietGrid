# QuietGrid v4.1 BookTicker Stream Parsing Hotfix

Generated: 2026-08-29T08:42:00+08:00

## Result

`PASS_BOOKTICKER_STREAM_PARSER`

`PASS_TRADE_BOOK_COMBINED_STREAM`

## Identity

- Pre-fix HEAD: `0f93ce6d46c2e4f7e4bd0897695b517e3be48faf`
- BookTicker Hotfix SHA: `f4a36d04ad33384ef601ce13826277c881faceb7`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`

## Root Cause

`BinancePublicTradeStream.__aiter__()` compared the lowercased Binance event
name against the original camel-case string:

```python
str(data.get("e", "")).lower() == "bookTicker"
```

`"bookTicker".lower()` is `"bookticker"`, so the comparison could never match
and every Production Public BOOK_TICKER message was silently ignored.

## Parser Before / After

Before:

```python
if str(data.get("e", "")).lower() == "trade":
    ...
elif str(data.get("e", "")).lower() == "bookTicker":
    ...
```

After:

```python
event_type = str(data.get("e", "")).strip().lower()
if event_type == "trade":
    ...
elif event_type == "bookticker":
    ...
```

Field semantics are unchanged: `s`, `u`, `b`, `B`, `a`, `A`, `T`/`E` still map
to `BOOK_TICKER`, symbol, event id, bid, bid_qty, ask, ask_qty, and timestamp.

## Tests

- `test_v41_binance_stream_parses_real_bookticker_event`
- `test_v41_binance_stream_yields_trade_and_bookticker`
- `test_v41_binance_stream_ignores_unknown_event`
- `test_v41_book_and_trade_freshness_update_independently`

## Offline Regression

- Pytest: `878 passed` (baseline `874` plus 4 new tests)
- Compileall: PASS
- `git diff --check`: PASS

## Integrity

- Candidate SHA unchanged
- Effective Strategy SHA unchanged
- Forward OOS unchanged at `2 / 8`
