# Semiconductor Grid v2.7.2 Backtest

## 1. 实现状态

- P0 修复：窗口完整性、正确尾部、退出前库存、真实 Regime 已接入。
- 完整测试：以 `pytest.stdout.log` 为准；正式矩阵仅在 0 failed 后运行。
- 未完成窗口污染：`否`（总完整窗口 47）。
- 库存指标：使用强平前快照，不再从平仓后净仓位推断。
- Regime：生产 `RegimeEngine`，观察期闭合 Bar；历史深度使用明确标记的 `OBSERVATION_QUOTE_VOLUME_PER_BAR_PROXY`。

## 2. 数据与样本暴露

- data_cutoff_utc: `2026-07-25T08:34:59.999000+00:00`
- 完整窗口数（symbol-window records）: `47`
- 未完成窗口数（已排除）: `4`
- 已暴露历史边界: `2026-07-20T06:00:00+00:00`
- 已暴露历史标签: `RESEARCH_VALIDATION_EXPOSED`
- Forward OOS 起点: `2026-07-26T04:32:00+00:00`
- Forward OOS 状态: `INSUFFICIENT_FORWARD_OOS`

## 3. v2.7.1 与 R0 差异

- incomplete-window exclusion：跨越数据截止的窗口不运行、不聚合。
- corrected window end：交易持续到 `force_close_at`，不再额外扣除最短交易时长。
- inventory accounting：保存 pre-exit 快照并拆分库存退出损益。
- regime admission：不再使用固定 100 分。
- other：Funding 收付分离，旧 OOS 全部重分类为 exposed。
- MUUSDT/N100/EXECUTION_STRESS: v2.7.1=-7.573049, R0=-3.026739, delta=4.546310。
- MUUSDT/N100/PRIMARY_ZERO_MAKER: v2.7.1=-6.107766, R0=-2.467610, delta=3.640156。
- MUUSDT/N20/EXECUTION_STRESS: v2.7.1=-36.957598, R0=-13.711532, delta=23.246066。
- MUUSDT/N20/MAKER_PROMO_OFF: v2.7.1=-29.491828, R0=-12.809250, delta=16.682578。
- MUUSDT/N20/PRIMARY_ZERO_MAKER: v2.7.1=-30.173348, R0=-10.986059, delta=19.187289。
- SKHYNIXUSDT/L20/EXECUTION_STRESS: v2.7.1=-4.026028, R0=4.392227, delta=8.418255。
- SKHYNIXUSDT/L20/MAKER_PROMO_OFF: v2.7.1=0.578524, R0=8.587051, delta=8.008527。
- SKHYNIXUSDT/L20/PRIMARY_ZERO_MAKER: v2.7.1=1.338795, R0=8.916830, delta=7.578035。
- SKHYNIXUSDT/N20/EXECUTION_STRESS: v2.7.1=-3.346815, R0=2.636991, delta=5.983806。
- SKHYNIXUSDT/N20/MAKER_PROMO_OFF: v2.7.1=2.489006, R0=5.760664, delta=3.271658。
- SKHYNIXUSDT/N20/PRIMARY_ZERO_MAKER: v2.7.1=5.457186, R0=6.147616, delta=0.690430。
- SNDKUSDT/N100/EXECUTION_STRESS: v2.7.1=0.760725, R0=-5.300692, delta=-6.061417。
- SNDKUSDT/N100/PRIMARY_ZERO_MAKER: v2.7.1=-0.301898, R0=-4.671781, delta=-4.369883。
- SNDKUSDT/N20/EXECUTION_STRESS: v2.7.1=7.585351, R0=-7.569791, delta=-15.155142。
- SNDKUSDT/N20/MAKER_PROMO_OFF: v2.7.1=19.049267, R0=-5.524418, delta=-24.573685。
- SNDKUSDT/N20/PRIMARY_ZERO_MAKER: v2.7.1=18.809260, R0=-3.952335, delta=-22.761595。
- SOXLUSDT/N100/EXECUTION_STRESS: v2.7.1=-0.578015, R0=-1.079803, delta=-0.501788。
- SOXLUSDT/N100/PRIMARY_ZERO_MAKER: v2.7.1=-0.231796, R0=-0.474743, delta=-0.242946。
- SOXLUSDT/N20/EXECUTION_STRESS: v2.7.1=7.616580, R0=0.603236, delta=-7.013345。
- SOXLUSDT/N20/MAKER_PROMO_OFF: v2.7.1=7.930731, R0=1.078366, delta=-6.852365。
- SOXLUSDT/N20/PRIMARY_ZERO_MAKER: v2.7.1=11.743609, R0=1.543920, delta=-10.199689。

## 4. R0 固定网格结果

- KR_STOCK/L20/EXECUTION_STRESS: pnl=4.392227, windows=1, pf=4.392
- KR_STOCK/L20/MAKER_PROMO_OFF: pnl=8.587051, windows=1, pf=8.587
- KR_STOCK/L20/PRIMARY_ZERO_MAKER: pnl=8.916830, windows=1, pf=8.917
- KR_STOCK/N20/EXECUTION_STRESS: pnl=2.636991, windows=1, pf=2.637
- KR_STOCK/N20/MAKER_PROMO_OFF: pnl=5.760664, windows=1, pf=5.761
- KR_STOCK/N20/PRIMARY_ZERO_MAKER: pnl=6.147616, windows=1, pf=6.148
- US_LEVERAGED_ETF/N100/EXECUTION_STRESS: pnl=-1.079803, windows=1, pf=0.000
- US_LEVERAGED_ETF/N100/PRIMARY_ZERO_MAKER: pnl=-0.474743, windows=1, pf=0.000
- US_LEVERAGED_ETF/N20/EXECUTION_STRESS: pnl=0.603236, windows=2, pf=2.110
- US_LEVERAGED_ETF/N20/MAKER_PROMO_OFF: pnl=1.078366, windows=2, pf=3.106
- US_LEVERAGED_ETF/N20/PRIMARY_ZERO_MAKER: pnl=1.543920, windows=2, pf=5.618
- US_STOCK/N100/EXECUTION_STRESS: pnl=-8.327431, windows=3, pf=0.000
- US_STOCK/N100/PRIMARY_ZERO_MAKER: pnl=-7.139390, windows=3, pf=0.000
- US_STOCK/N20/EXECUTION_STRESS: pnl=-21.281323, windows=6, pf=0.043
- US_STOCK/N20/MAKER_PROMO_OFF: pnl=-18.333669, windows=6, pf=0.041
- US_STOCK/N20/PRIMARY_ZERO_MAKER: pnl=-14.938394, windows=6, pf=0.134

## 5. R1 Controller-faithful 结果

- KR_STOCK/L20/EXECUTION_STRESS: pnl=4.392227, windows=1, pf=4.392
- KR_STOCK/L20/MAKER_PROMO_OFF: pnl=8.587051, windows=1, pf=8.587
- KR_STOCK/L20/PRIMARY_ZERO_MAKER: pnl=8.916830, windows=1, pf=8.917
- KR_STOCK/N20/EXECUTION_STRESS: pnl=2.636991, windows=1, pf=2.637
- KR_STOCK/N20/MAKER_PROMO_OFF: pnl=5.760664, windows=1, pf=5.761
- KR_STOCK/N20/PRIMARY_ZERO_MAKER: pnl=6.147616, windows=1, pf=6.148
- US_LEVERAGED_ETF/N100/EXECUTION_STRESS: pnl=-1.079803, windows=1, pf=0.000
- US_LEVERAGED_ETF/N100/PRIMARY_ZERO_MAKER: pnl=-0.474743, windows=1, pf=0.000
- US_LEVERAGED_ETF/N20/EXECUTION_STRESS: pnl=-0.002177, windows=2, pf=0.998
- US_LEVERAGED_ETF/N20/MAKER_PROMO_OFF: pnl=0.707618, windows=2, pf=1.801
- US_LEVERAGED_ETF/N20/PRIMARY_ZERO_MAKER: pnl=1.470066, windows=2, pf=4.602
- US_STOCK/N100/EXECUTION_STRESS: pnl=-8.327431, windows=3, pf=0.000
- US_STOCK/N100/PRIMARY_ZERO_MAKER: pnl=-7.139390, windows=3, pf=0.000
- US_STOCK/N20/EXECUTION_STRESS: pnl=-19.712695, windows=6, pf=0.047
- US_STOCK/N20/MAKER_PROMO_OFF: pnl=-16.565299, windows=6, pf=0.045
- US_STOCK/N20/PRIMARY_ZERO_MAKER: pnl=-12.831086, windows=6, pf=0.152
- Controller 诊断范围：`R1_CONTROLLER_FAITHFUL/PRIMARY_ZERO_MAKER`，sessions=117，cooldowns=105，reentries=15。
- 滚动重建状态：`{"SKIPPED": 117}`；regrid_cost=0.000000 USDT。
- 会话停止原因：`{"stop_loss": 54, "stop_loss_upper": 57, "window_force_close": 6}`；执行成本=13.290161 USDT。

## 6. 库存与退出归因

- 以下为 R1/PRIMARY 逐运行等权诊断合计（候选与种子并列，不解释为可同时部署的组合收益）。
- 配对网格利润=374.840965 USDT；库存已实现损益=-401.402919 USDT；被库存吞噬=401.402919 USDT（1.071× 配对利润）。
- 退出前平均库存名义价值=170.422932 USDT；非零退出前库存运行=96/102。
- 最差库存尾部：SOXLUSDT/N20/seed=17，window=`US_LEVERAGED_ETF:2026-06-18T20:00:00+00:00:2026-06-22T08:00:00+00:00`，退出前未实现损益=-7.608732 USDT，名义价值=126.782500 USDT。
- 拖累比率最小分母：`0.01` USDT；绝对拖累同时保留，未将异常比率截断为零。
- 逐运行明细见 `pre-exit-inventory-breakdown.csv` 与 `exit-attribution.csv`。

## 7. 稳健性

- 三执行场景和六随机种子均按预注册矩阵运行；种子先在同一市场窗口内聚合。
- 最佳受检候选 KR_STOCK/L20/EXECUTION_STRESS: pnl=4.392227 USDT。
- 最佳受检候选 KR_STOCK/L20/MAKER_PROMO_OFF: pnl=8.587051 USDT。
- 最佳受检候选 KR_STOCK/L20/PRIMARY_ZERO_MAKER: pnl=8.916830 USDT。
- PRIMARY seed=3: pnl=9.744445 USDT，windows=1。
- PRIMARY seed=10: pnl=9.744445 USDT，windows=1。
- PRIMARY seed=17: pnl=9.744445 USDT，windows=1。
- PRIMARY seed=31: pnl=9.744445 USDT，windows=1。
- PRIMARY seed=59: pnl=7.261599 USDT，windows=1。
- PRIMARY seed=97: pnl=7.261599 USDT，windows=1。

## 8. 标的级结论

- MUUSDT/N100: `INSUFFICIENT_DATA`，net_pnl=-2.467610。
- MUUSDT/N20: `INSUFFICIENT_DATA`，net_pnl=-8.878752。
- SKHYNIXUSDT/L20: `INSUFFICIENT_DATA`，net_pnl=8.916830。
- SKHYNIXUSDT/N20: `INSUFFICIENT_DATA`，net_pnl=6.147616。
- SNDKUSDT/N100: `INSUFFICIENT_DATA`，net_pnl=-4.671781。
- SNDKUSDT/N20: `INSUFFICIENT_DATA`，net_pnl=-3.952335。
- SOXLUSDT/N100: `INSUFFICIENT_DATA`，net_pnl=-0.474743。
- SOXLUSDT/N20: `INSUFFICIENT_DATA`，net_pnl=1.470066。

## 9. Forward OOS

- 状态：`INSUFFICIENT_FORWARD_OOS`
- 历史已暴露窗口未计入 Forward OOS。

## 10. 验收门槛

- minimum_unique_windows: threshold=8 actual=1 `FAIL`
- positive_window_ratio: threshold=0.55 actual=1.0 `PASS`
- profit_factor: threshold=1.05 actual=8.916829838218312 `PASS`
- max_drawdown_pct: threshold=0.05 actual=0.0 `PASS`
- mean_inventory_drag_ratio: threshold=0.35 actual=0.0 `PASS`
- best_market_window_concentration: threshold=0.35 actual=1.0 `FAIL`
- PRIMARY_ZERO_MAKER_net_pnl: threshold=0.0 actual=8.916829838218312 `PASS`
- EXECUTION_STRESS_net_pnl: threshold=0.0 actual=4.392227234552383 `PASS`
- positive_seed_count: threshold=4 actual=6 `PASS`

## 11. 最终结论代码

`FAIL_NO_ROBUST_EDGE`

本轮未形成稳健候选，保持自动开仓关闭，不进行参数搜索。

## 安全开关

- startup_auto_entry = false
- testnet_force_window = false
- testnet_fast_observation = false
- data_quality_ok = True
