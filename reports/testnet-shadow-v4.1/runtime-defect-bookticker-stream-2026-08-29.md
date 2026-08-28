# Runtime Defect: BinancePublicTradeStream drops BOOK_TICKER

Generated: 2026-08-29T02:23:00+08:00

## Classification

`BLOCKED_BY_RUNTIME_DEFECT`

## Evidence

During the real 10-minute PAPER_BASELINE smoke after the DB initialization
hotfix:

- Journal event type counts: `TRADE=13800`, `BOOK_TICKER=0`
- All 13800 TRADE events carried native Binance trade id `t`
- Duplicate event IDs: 0
- Out-of-order exchange timestamps: 0

The runtime used the same combined Production Public WebSocket URL as the
successful probe. A direct 15-second raw WebSocket sample on the same URL
received:

```text
bookTicker 7168
trade      2061
```

A direct run of `BinancePublicTradeStream` over the same URL for 5 seconds
received only:

```text
Counter({'TRADE': 132})
```

## Root Cause

In `quietgrid_v41/runtime.py`, the event type check is:

```python
elif str(data.get("e", "")).lower() == "bookTicker":
```

The value is lowercased before comparison, so the comparison can never match
`"bookTicker"` (capital T). BOOK_TICKER messages are silently ignored.

## Impact

- Book freshness cannot be maintained by the runtime
- Risk-increasing maker orders cannot be fenced on stale book state
- Baseline smoke cannot be accepted as PASS

## Stop Rule Applied

No further network stages were run. No runtime code was modified after
detection.
