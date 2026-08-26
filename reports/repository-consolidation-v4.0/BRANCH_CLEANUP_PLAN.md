# QuietGrid v4.0 Branch Cleanup Plan

PROPOSED ONLY — NOT EXECUTED

本轮只生成建议，不创建 archive tag、不删除 branch、不移动 tag。

## Proposed annotated tags

git tag -a research/v2.8-final 7a24b18c0f027853fcfd939e8d2cb375f2e99c59 -m 'archive v2.8 research'
git tag -a research/v2.9-freeze f5c2a6a45b28b348dcc50c3cbbda6d206b11e102 -m 'archive v2.9 freeze'
git tag -a research/v2.9.2-diagnostics abe6ebf4474ac0362707fa631d29e35a81cc81b4 -m 'archive v2.9.2 diagnostics'
git tag -a research/v3.2-final eb64ed821b09f285041639a1502456d24bb40e2f -m 'archive v3.2 research'
git tag -a research/v3.3-final 3c15782da79dab306154d02653ca59201370b76f -m 'archive v3.3 research'
git tag -a research/v3.4-final 32df63f7a843bdf0982d580f427026f416d70118 -m 'archive v3.4 research'
git tag -a research/v3.5-final 996151e9f910786494c03e7d5e5bfef5a525e964 -m 'archive v3.5 research'

## Proposed branch deletes

git branch -d codex/semiconductor-grid-backtest-run-v2.7.1
git branch -d codex/semiconductor-grid-combinatorial-backtest-v2.8
git branch -d codex/semiconductor-grid-forward-oos-v2.9

All commands above are proposed only. Existing freeze tag is not moved or replaced.
