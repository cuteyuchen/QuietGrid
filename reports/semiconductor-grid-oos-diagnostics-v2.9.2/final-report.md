# QuietGrid v2.9.2 Forward OOS Loss Attribution

## Formal status

`INSUFFICIENT_FORWARD_OOS` (2/8); acceptance gates remain `NOT_EVALUATED`.

## Diagnostic status

`EARLY_WARNING_INVENTORY_TAIL`

## 23 Answers
1. 4 个理论窗口中，NYSE/XKRX 2026-08-14 完整合格；NYSE/XKRX 2026-08-21 因数据覆盖不完整拒绝。
2. 窗口 1 net=-4.034062189851；窗口 2 net=0.000000000000。
3. 主要亏损来自 NYSE 窗口；XKRX 窗口零交易。
4. SNDK paired_grid_pnl=1.969562546715，为正。
5. 20 个 paired cycles 全部为正，positive_cycle_ratio=1.000000。
6. inventory_realized_pnl=-5.855964424613 USDT。
7. inventory_drag_usdt=5.700604726513，ratio=2.894350695295。
8. 首次超过 paired 利润：2026-08-17T00:04:00+00:00。
9. 最大方向 SHORT：max_short=0.130000，max_long=0.040000。
10. max inventory utilization=0.4381572。
11. 最老 lot=1460 个分钟 bar。
12. POST_ENTRY_REGIME_SHIFT=False。
13. Stop 分类=TRUE_BREAKOUT, UNRESOLVED；可观测路径为 TRUE_BREAKOUT。
14. +30/+60/+120 分钟未回到 grid range/entry；+240 分钟无完整观测，window_end 仍 TRUE_BREAKOUT。
15. block same-side 20/30% 均值 net=-4.528887；40/50% 基本等于 baseline。
16. reduce-only 20/30% 均值 net=-2.966652，有明显诊断改善；40% 不变。
17. stop boundary 均值 net 0.5x/1x/1.5x/2x=-0.914677/-4.034062/-7.153447/-8.300866；更宽 stop 风险更高。
18. PRIMARY/STRESS/MAKER_OFF：PRIMARY_ZERO_MAKER=-4.034062 (ΔPRIMARY=+0.000000); EXECUTION_STRESS=-4.805862 (ΔPRIMARY=-0.771800); MAKER_PROMO_OFF=-4.236835 (ΔPRIMARY=-0.202773)。
19. 六个固定 seed 全部为负，市场路径/库存尾部更重要。
20. 主要问题是 GRID_EDGE_PRESENT_BUT_INVENTORY_TAIL_DOMINATES。
21. 没有理由现在修改 31111；SNDK removal is NOT authorized from 2/8 Forward OOS.
22. 应继续原样累计到 8/8。
23. Early Warning=EARLY_WARNING_INVENTORY_TAIL；正式结论=INSUFFICIENT_FORWARD_OOS。

按要求公式 reconciliation error 最大=0.388399245250 USDT；等于 stop slippage，engine-native reconciliation error=0。
CF_NO_INVENTORY 理论净收益均值=1.666543 USDT；所有 counterfactual 均为 DIAGNOSTIC_COUNTERFACTUAL。
31111 参数、生产配置、candidate SHA、exposure cutoff、正式 ledger 和自动交易状态均未修改。