# QuietGrid v4.1 Runtime DB Initialization Hotfix

Generated: 2026-08-29T02:20:00+08:00

## Result

`PASS_RUNTIME_DB_SCHEMA_INITIALIZATION`

## Identity

- Pre-fix HEAD: `08382cd70d0c98ce2d463cc8222e6efaf09932db`
- Hotfix SHA: `d402c51626d2ef9bc1eab9b4bccea63fa51b2c73`
- Branch: `codex/semiconductor-grid-continuous-shadow-runtime-v4.1`
- Candidate: `31111-NEUTRAL`
- Candidate SHA: `c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Effective Strategy SHA: `776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3`
- Forward OOS: `2 / 8`

## Root Cause

`ContinuousShadowRuntime.create()` did not run `db.database.init_db()` on the
runtime SQLite before constructing the real `TradingController`. A fresh DB
therefore contained only `shadow_*` tables created by `ShadowBroker`, and the
first controller tick failed with:

```text
sqlite3.OperationalError: no such table: control_state
```

## Fix Scope

Changed files:

- `quietgrid_v41/runtime.py`
- `tests/test_v41_shadow_runtime.py`

`init_db(runtime_db)` and `_ensure_runtime_db_schema(runtime_db)` are now called
inside `ContinuousShadowRuntime.create()` before `ShadowBroker` and controller
construction. Strategy semantics, compiler behavior, ShadowBroker execution,
and the frozen candidate were not changed.

## Offline Regression

- Pytest: `874 passed` (baseline `868` plus 6 new v4.1 DB initialization tests)
- Compileall: PASS
- `git diff --check`: PASS
- Fresh DB single-event real-controller repro: PASS, `controller_ticks=1`,
  `reconcile=RECONCILED`
- Existing shadow-only DB upgrade: PASS, non-destructive
- Restart idempotency: PASS, tables unchanged, state preserved
- Baseline / Conservative DB isolation: PASS
- Official CLI fresh DB path: PASS

## Acceptance

- `PASS_RUNTIME_DB_SCHEMA_INITIALIZATION`
- `PASS_FRESH_DB_REAL_CONTROLLER_FIRST_EVENT`
- `PASS_RUNTIME_DB_RESTART_IDEMPOTENCY`
- `PASS_BASELINE_CONSERVATIVE_DB_ISOLATION`
- `PASS_FREEZE_INTEGRITY`
- `PASS_FROZEN_31111_RUNTIME_PARITY`
