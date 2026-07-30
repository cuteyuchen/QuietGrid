# Window Boundary Audit

公式：`observation_end = previous_market_close + 180 minutes`；`force_close_at = next_reference_open - 120 minutes`；`minimum_trade_minutes` 仅用于准入。

## KR_STOCK

- previous_market_close: `2026-06-05T06:30:00+00:00`
- observation_end: `2026-06-05T09:30:00+00:00`
- force_close_at: `2026-06-07T22:00:00+00:00`
- next_reference_open: `2026-06-08T00:00:00+00:00`
- last_included_trade_bar: `2026-06-07T21:59:00+00:00`
- remaining_trade_minutes: `3630`
- window_complete: `True`

## US_LEVERAGED_ETF

- previous_market_close: `2026-05-15T20:00:00+00:00`
- observation_end: `2026-05-15T23:00:00+00:00`
- force_close_at: `2026-05-18T06:00:00+00:00`
- next_reference_open: `2026-05-18T08:00:00+00:00`
- last_included_trade_bar: `2026-05-18T05:59:00+00:00`
- remaining_trade_minutes: `3300`
- window_complete: `True`

## US_STOCK

- previous_market_close: `2026-04-10T20:00:00+00:00`
- observation_end: `2026-04-10T23:00:00+00:00`
- force_close_at: `2026-04-13T06:00:00+00:00`
- next_reference_open: `2026-04-13T08:00:00+00:00`
- last_included_trade_bar: `2026-04-13T05:59:00+00:00`
- remaining_trade_minutes: `3300`
- window_complete: `True`
