# Market Event Integrity

Event identity now includes source, symbol, stream type, and native event id:

`source:symbol:stream_type:raw_event_id`

Validation coverage:

- Binance trade websocket uses native `t` as the trade id.
- Same event id and same stream on the same symbol is a duplicate.
- Same event id on a different symbol is a distinct event.
- Same event id on a different stream type is a distinct event.
- Events without a native id use a deterministic fallback hash and are marked `EVENT_ID_FALLBACK_HASH`.
- Equal exchange timestamps remain distinct when sequence or stream identity differs.
- REST bootstrap events are separate from websocket events.

Result: PASS
