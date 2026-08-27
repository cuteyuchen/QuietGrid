# QuietGrid v4.1 Repository Consolidation

## Post-cleanup verification

- Verification date: 2026-08-27
- `origin/master`: `89026233c309006f8171999ade766af6e5705f09`
- v4.0 baseline reachable from `master`: yes
- `semiconductor-grid-testnet-shadow-v4.0`: annotated tag, target `89026233c309006f8171999ade766af6e5705f09`
- `semiconductor-grid-forward-oos-v2.9-freeze`: annotated tag, target `f5c2a6a45b28b348dcc50c3cbbda6d206b11e102`
- Forward OOS evidence status remains `2 / 8`, `NOT_EVALUATED`, `INSUFFICIENT_FORWARD_OOS`

## Remote branches after cleanup

```text
origin/codex/semiconductor-grid-forward-oos-monitor-v2.9.1
origin/master
```

The monitor branch remains at `50d681485503415ff339a8c24a3b90fda6049bb7` and remains the isolated Forward OOS evidence collection line. No v4.1 runtime work has been merged into it.

## Deleted branches and preservation

| Deleted branch | Tip SHA | Classification | Preservation |
|---|---|---|---|
| `codex/profit-protection-backtest-v2.3` | `31d828122f17978cec79aace5ac0a38f57c42f0e` | `ARCHIVE_TAG_THEN_DELETE` | `archive/codex-profit-protection-backtest-v2.3` |
| `codex/semiconductor-grid-backtest-v2.7` | `b40c8d73d3ca0bec8bbb6f7f4fb0cb36c3eed3b2` | `ARCHIVE_TAG_THEN_DELETE` | `archive/codex-semiconductor-grid-backtest-v2.7` |
| `codex/semiconductor-grid-breakout-confirmation-v3.3` | `3c15782da79dab306154d02653ca59201370b76f` | `ARCHIVE_TAG_THEN_DELETE` | `archive/rejected-semiconductor-grid-v3.3` |
| `codex/semiconductor-grid-breakout-inventory-protection-v3.2` | `eb64ed821b09f285041639a1502456d24bb40e2f` | `ARCHIVE_TAG_THEN_DELETE` | `archive/rejected-semiconductor-grid-v3.2` |
| `codex/semiconductor-grid-oos-diagnostics-v2.9.2` | `abe6ebf4474ac0362707fa631d29e35a81cc81b4` | `ARCHIVE_TAG_THEN_DELETE` | `archive/codex-semiconductor-grid-oos-diagnostics-v2.9.2` |
| `codex/semiconductor-grid-forward-oos-v2.9` | `f5c2a6a45b28b348dcc50c3cbbda6d206b11e102` | `ARCHIVE_TAG_THEN_DELETE` | existing `semiconductor-grid-forward-oos-v2.9-freeze` |
| `codex/semiconductor-grid-testnet-shadow-v4.0` | `89026233c309006f8171999ade766af6e5705f09` | `DELETE_REACHABLE_HISTORY` | `master` plus `semiconductor-grid-testnet-shadow-v4.0` |
| `codex/semiconductor-grid-backtest-run-v2.7.1` | `1d1fb073f921785867d689c4d89b540644531d22` | `DELETE_REACHABLE_HISTORY` | v4.0/master ancestry |
| `codex/semiconductor-grid-combinatorial-backtest-v2.8` | `7a24b18c0f027853fcfd939e8d2cb375f2e99c59` | `DELETE_REACHABLE_HISTORY` | v4.0/master ancestry |
| `v2.1-runtime-autostart` | `09140b8aafb1f72091a8be92666c22fad4f9b054` | `DELETE_REACHABLE_HISTORY` | master ancestry |
| `v2.2-neutral-trend-defense` | `e41a1bc704c0f76a9e013795a506cff629ea3494` | `DELETE_REACHABLE_HISTORY` | master ancestry |

Every remote deletion was followed by a fetch/prune and remote branch verification. No force push was used. The formal freeze tag was neither deleted nor moved.

## Recovered object anchors

- `archive/semiconductor-grid-v3.4-recovered` -> `32df63f7a843bdf0982d580f427026f416d70118`
- `archive/semiconductor-grid-v3.5-recovered` -> `996151e9f910786494c03e7d5e5bfef5a525e964`

Both are annotated tags and were remotely verified. Their messages mark them as recovered historical research evidence, not an accepted strategy and not active development.

## Active topology conclusion

`PASS_BRANCH_CONSOLIDATION`

The only remote active lines after cleanup are `master` and the Forward OOS monitor. The v4.1 development branch is created separately from the exact v4.0 baseline after this gate.
