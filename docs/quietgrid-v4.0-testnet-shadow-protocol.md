# QuietGrid v4.0 Testnet / Shadow Protocol

## Scope

v4.0 validates execution engineering around the frozen `31111-NEUTRAL` candidate at economic leverage `1x`. It does not perform parameter search, candidate reselection, leverage search, TP search, or inventory-rule search.

Evidence channels remain separate: historical exposed research, v2.9 Forward OOS, Testnet execution, TradFi public-data shadow, and production. v4 never appends Testnet or Shadow results to the v2.9 Forward OOS ledger.

## Lanes

- `TESTNET_EXECUTION`: Binance Futures Testnet only, with explicit `BINANCE_TESTNET=true` and an allowlisted Testnet host.
- `TRADFI_SHADOW_BASELINE`: production public market data plus persistent paper execution.
- `TRADFI_SHADOW_CONSERVATIVE`: the same public data and strategy intents with stricter queue, latency, participation, trade-through, and stale-data assumptions.
- `PUBLIC_DATA_ONLY`: public market reads without order mutation.

The default lane is no order capability. Production signed REST, production account endpoints, production listen keys, real orders, real cancels, leverage writes, and margin writes are disabled in v4.

## Frozen Runtime

`strategy/frozen_31111_runtime.py` loads and validates the attested candidate-freeze and config-freeze artifacts. The candidate SHA, freeze commit, freeze time, exposure cutoff, symbol universe, calendars, SOXL capital multiplier, and economic leverage are read from those artifacts rather than copied into a new mutable strategy configuration.

## Capability Probe

`scripts/quietgrid_v40_capability_probe.py` performs only public Testnet `/fapi/v1/time` and `/fapi/v1/exchangeInfo` calls by default. Authenticated checks and real order smoke are opt-in and must never run from the public probe flag. Missing credentials are reported as `SKIPPED_NO_CREDENTIALS`; network failures are `ERROR_RETRYABLE`.

## Shadow Execution

`exchange/shadow.py` is independent from `MockExchangeClient`. It persists orders, fills, positions, and events in SQLite. A quote touch is not a fill. Baseline and conservative profiles use queue-ahead estimates, participation caps, placement/cancel latency, stale-data cutoffs, partial fills, post-only rejection, and deterministic adverse-first handling for ambiguous events.

Paper state can be reopened after a process restart. Reconciliation and force-flat operate only on the QuietGrid paper namespace. Client order IDs are idempotent and no v4 path mutates the v2.9 ledger.

## Reports

Tracked v4 evidence is written under `reports/repository-consolidation-v4.0/` and `reports/testnet-shadow-v4.0/`. Runtime databases and sensitive values belong under ignored `data/shadow/` or `data/runtime/`.

Profitability is not evaluated by this protocol: `NOT_EVALUATED_FOR_PROFITABILITY`.
