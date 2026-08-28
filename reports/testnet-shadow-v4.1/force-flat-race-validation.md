# Force-flat Race Validation

Force-flat is event-driven and episode-scoped:

`WAIT_CANCEL_TERMINAL -> WAIT_FLATTEN -> COMPLETE | FAIL_FORCE_FLAT`

Validation coverage:

- Cancel latency window is not skipped.
- A resting order filled during the cancel window increases the position before flattening.
- The force-flat state machine re-reads position after the cancel terminal.
- No-fill, full-fill, and fill-during-cancel race scenarios complete with zero position and zero active risk orders.
- `max_attempts` is persisted with the episode and enforced across events and restarts.
- A force-flat episode cannot be replayed; a new episode creates a new idempotent cycle.
- Active force-flat latches reject new non-reducing maker orders.

Result: PASS
