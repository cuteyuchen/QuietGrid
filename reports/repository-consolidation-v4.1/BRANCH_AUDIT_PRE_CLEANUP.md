# QuietGrid v4.1 Repository Consolidation

## Audit metadata

- Audit date: 2026-08-27
- Remote: `origin` -> `https://github.com/cuteyuchen/QuietGrid.git`
- Audit command: `git fetch --all --tags --prune`
- Known v4.0 baseline: `89026233c309006f8171999ade766af6e5705f09`
- Old master: `55e4a1116c9e4d4814d6d0c4a8cc5a0a1c871e47`
- Retained Forward OOS monitor: `codex/semiconductor-grid-forward-oos-monitor-v2.9.1`
- Existing freeze tag: `semiconductor-grid-forward-oos-v2.9-freeze`

This report records the state before cleanup. Branch deletion is allowed only after the classification and preservation condition in the table is satisfied. No force push, reset, or freeze-tag movement is authorized.

## Remote branches

| Branch | Tip SHA | Tip subject | Tip date | Tip is ancestor of v4.0 | Tip is ancestor of master | Unique commits outside retained lines | Existing tag coverage | Classification |
|---|---|---|---|---:|---:|---:|---|---|
| `master` | `55e4a1116c9e4d4814d6d0c4a8cc5a0a1c871e47` | feat: add semiconductor closed-market grid v2.7 | 2026-07-25 | yes | yes | 0 | none at tip | `KEEP_ACTIVE` |
| `codex/semiconductor-grid-forward-oos-monitor-v2.9.1` | `50d681485503415ff339a8c24a3b90fda6049bb7` | research: refresh and append forward OOS data | 2026-08-23 | yes | no | 0 | none at tip; freeze tag in ancestry | `KEEP_ACTIVE` |
| `codex/semiconductor-grid-testnet-shadow-v4.0` | `89026233c309006f8171999ade766af6e5705f09` | docs: bind v4 reports to final runtime head | 2026-08-26 | yes | no | 0 | milestone tag to be created | `DELETE_REACHABLE_HISTORY` |
| `codex/semiconductor-grid-forward-oos-v2.9` | `f5c2a6a45b28b348dcc50c3cbbda6d206b11e102` | research: regenerate v2.9 forward OOS freeze evidence | 2026-08-09 | yes | no | 0 | `semiconductor-grid-forward-oos-v2.9-freeze` | `ARCHIVE_TAG_THEN_DELETE` |
| `codex/semiconductor-grid-oos-diagnostics-v2.9.2` | `abe6ebf4474ac0362707fa631d29e35a81cc81b4` | research: diagnose first forward OOS inventory losses v2.9.2 | 2026-08-23 | no | no | 1 | none | `ARCHIVE_TAG_THEN_DELETE` |
| `codex/semiconductor-grid-breakout-inventory-protection-v3.2` | `eb64ed821b09f285041639a1502456d24bb40e2f` | research: evaluate breakout inventory protection v3.2 | 2026-08-23 | no | no | 2 | none | `ARCHIVE_TAG_THEN_DELETE` |
| `codex/semiconductor-grid-breakout-confirmation-v3.3` | `3c15782da79dab306154d02653ca59201370b76f` | research: improve breakout confirmation precision v3.3 | 2026-08-23 | no | no | 3 | none | `ARCHIVE_TAG_THEN_DELETE` |
| `codex/semiconductor-grid-backtest-v2.7` | `b40c8d73d3ca0bec8bbb6f7f4fb0cb36c3eed3b2` | test: cover Binance rule snapshot normalization | 2026-07-25 | no | no | 12 | none | `ARCHIVE_TAG_THEN_DELETE` |
| `codex/profit-protection-backtest-v2.3` | `31d828122f17978cec79aace5ac0a38f57c42f0e` | research: record stock perpetual H1.1 invalid sample gate | 2026-07-25 | no | no | 9 | none | `ARCHIVE_TAG_THEN_DELETE` |
| `codex/semiconductor-grid-backtest-run-v2.7.1` | `1d1fb073f921785867d689c4d89b540644531d22` | docs: add v2.8 combinatorial grid backtest protocol | 2026-07-30 | yes | no | 0 | freeze tag in ancestry | `DELETE_REACHABLE_HISTORY` |
| `codex/semiconductor-grid-combinatorial-backtest-v2.8` | `7a24b18c0f027853fcfd939e8d2cb375f2e99c59` | 回测结果 | 2026-07-31 | yes | no | 0 | freeze tag in ancestry | `DELETE_REACHABLE_HISTORY` |
| `v2.1-runtime-autostart` | `09140b8aafb1f72091a8be92666c22fad4f9b054` | feat(api): 增强数据健康检查与交易控制逻辑 | 2026-07-21 | yes | yes | 0 | none at tip; master ancestry | `DELETE_REACHABLE_HISTORY` |
| `v2.2-neutral-trend-defense` | `e41a1bc704c0f76a9e013795a506cff629ea3494` | docs: add neutral-grid trend defense implementation notes | 2026-07-21 | yes | yes | 0 | none at tip; master ancestry | `DELETE_REACHABLE_HISTORY` |

`unique commits outside retained lines` is `git rev-list --count <tip> --not <master> <monitor> <v4.0>`. A zero value means the branch tip's history is already reachable from a retained engineering/evidence line. For nonzero values, the branch tip must be archived before deletion.

## Recovered objects

| Object | Availability | Existing tag coverage | Required action |
|---|---|---|---|
| `32df63f7a843bdf0982d580f427026f416d70118` (v3.4) | available | none | create and push annotated tag `archive/semiconductor-grid-v3.4-recovered` |
| `996151e9f910786494c03e7d5e5bfef5a525e964` (v3.5) | available | none | create and push annotated tag `archive/semiconductor-grid-v3.5-recovered` |

The recovered v3.4/v3.5 objects are historical research evidence, not an accepted strategy and not active development.

## Freeze integrity before cleanup

- `semiconductor-grid-forward-oos-v2.9-freeze` is an annotated tag object.
- Its peeled commit target is `f5c2a6a45b28b348dcc50c3cbbda6d206b11e102`.
- The v2.9 freeze branch points to that same commit and may be deleted only after this tag remains remotely verified.
- Current Forward OOS assessment remains `2 / 8`, `NOT_EVALUATED`, `INSUFFICIENT_FORWARD_OOS`.

## master fast-forward precondition

- `OLD_MASTER`: `55e4a1116c9e4d4814d6d0c4a8cc5a0a1c871e47`
- `NEW_MASTER`: `89026233c309006f8171999ade766af6e5705f09`
- merge-base: `55e4a1116c9e4d4814d6d0c4a8cc5a0a1c871e47`
- ahead/behind (`OLD_MASTER...NEW_MASTER`): `0 18`
- `OLD_MASTER` is an ancestor of `NEW_MASTER`: yes
- planned operation: normal fast-forward only

## Worktrees observed

```text
E:/project/QuietGrid       8902623 [codex/semiconductor-grid-testnet-shadow-v4.0]
E:/project/QuietGrid-v3    4bd2ea4 [codex/semiconductor-grid-leverage-robustness-v3.0]
E:/project/QuietGrid-v31   4b181cd [codex/semiconductor-grid-integer-leverage-v3.1]
```

The v3.0 and v3.1 branches are local worktree branches and are not remote active branches. They must not be deleted while their worktrees are in use.

## Cleanup decision

No remote branch was classified `BLOCKED_UNKNOWN`, and no branch was identified as irreplaceable active formal work. Proceed with annotated archive tags and ordinary remote branch deletion one branch at a time, then re-fetch and verify the final topology before creating v4.1.
