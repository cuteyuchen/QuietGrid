# Semiconductor Grid v2.7.1 Backtest

## 1. 执行摘要

- 结论代码: `FAIL_NO_ROBUST_EDGE`
- 是否存在可验证优势: `否`
- 最佳 Profile: `US_LEVERAGED_ETF/N20`
- 是否依赖零 Maker: `False`
- EXECUTION_STRESS 净收益: `7.6165802774560944`
- Final OOS 净收益: `-0.16956252846693676`
- 是否允许进入测试网候选: `否`

## 2. 数据可信度

- research_branch: `codex/semiconductor-grid-backtest-run-v2.7.1`
- base_commit_sha: `55e4a1116c9e4d4814d6d0c4a8cc5a0a1c871e47`
- data_ok: `True`
- total_unique_windows: `51`
- runs: `942`
- blocked: `197`

## 3. 固定基准结果

- KR_STOCK/L100/EXECUTION_STRESS: pnl=3.225431 windows=2 pos_ratio=1.000 pf=3.225
- KR_STOCK/L100/MAKER_PROMO_OFF: pnl=3.937859 windows=2 pos_ratio=1.000 pf=3.938
- KR_STOCK/L100/PRIMARY_ZERO_MAKER: pnl=3.819342 windows=2 pos_ratio=1.000 pf=3.819
- KR_STOCK/L20/EXECUTION_STRESS: pnl=-4.026028 windows=3 pos_ratio=0.667 pf=0.631
- KR_STOCK/L20/MAKER_PROMO_OFF: pnl=0.578524 windows=3 pos_ratio=0.667 pf=1.055
- KR_STOCK/L20/PRIMARY_ZERO_MAKER: pnl=1.338795 windows=3 pos_ratio=0.667 pf=1.131
- KR_STOCK/N100/EXECUTION_STRESS: pnl=-2.450293 windows=4 pos_ratio=0.500 pf=0.306
- KR_STOCK/N100/MAKER_PROMO_OFF: pnl=-0.689374 windows=2 pos_ratio=0.500 pf=0.278
- KR_STOCK/N100/PRIMARY_ZERO_MAKER: pnl=0.850276 windows=4 pos_ratio=0.500 pf=1.301
- KR_STOCK/N20/EXECUTION_STRESS: pnl=-3.346815 windows=8 pos_ratio=0.500 pf=0.772
- KR_STOCK/N20/MAKER_PROMO_OFF: pnl=2.489006 windows=8 pos_ratio=0.500 pf=1.182
- KR_STOCK/N20/PRIMARY_ZERO_MAKER: pnl=5.457186 windows=8 pos_ratio=0.500 pf=1.434
- US_LEVERAGED_ETF/N100/EXECUTION_STRESS: pnl=-0.578015 windows=1 pos_ratio=0.000 pf=0.000
- US_LEVERAGED_ETF/N100/MAKER_PROMO_OFF: pnl=-0.523983 windows=1 pos_ratio=0.000 pf=0.000
- US_LEVERAGED_ETF/N100/PRIMARY_ZERO_MAKER: pnl=-0.231796 windows=1 pos_ratio=0.000 pf=0.000
- US_LEVERAGED_ETF/N20/EXECUTION_STRESS: pnl=7.616580 windows=9 pos_ratio=0.556 pf=2.083
- US_LEVERAGED_ETF/N20/MAKER_PROMO_OFF: pnl=7.930731 windows=9 pos_ratio=0.556 pf=2.163
- US_LEVERAGED_ETF/N20/PRIMARY_ZERO_MAKER: pnl=11.743609 windows=9 pos_ratio=0.556 pf=2.861
- US_STOCK/N100/EXECUTION_STRESS: pnl=-6.812324 windows=5 pos_ratio=0.200 pf=0.237
- US_STOCK/N100/MAKER_PROMO_OFF: pnl=-5.141775 windows=4 pos_ratio=0.250 pf=0.309
- US_STOCK/N100/PRIMARY_ZERO_MAKER: pnl=-6.409663 windows=5 pos_ratio=0.200 pf=0.085
- US_STOCK/N20/EXECUTION_STRESS: pnl=-29.372248 windows=13 pos_ratio=0.308 pf=0.348
- US_STOCK/N20/MAKER_PROMO_OFF: pnl=-10.442561 windows=13 pos_ratio=0.308 pf=0.710
- US_STOCK/N20/PRIMARY_ZERO_MAKER: pnl=-11.364088 windows=13 pos_ratio=0.385 pf=0.692

## 4. Profile 对比

- KR_STOCK/L100: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (insufficient_windows;window_concentration_too_high)
- KR_STOCK/L20: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (insufficient_windows;window_concentration_too_high;execution_stress_not_positive)
- KR_STOCK/N100: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (insufficient_windows;low_positive_ratio;window_concentration_too_high;execution_stress_not_positive)
- KR_STOCK/N20: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (low_positive_ratio;window_concentration_too_high;execution_stress_not_positive)
- US_LEVERAGED_ETF/N100: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (insufficient_windows;primary_not_positive;low_positive_ratio;low_profit_factor;window_concentration_too_high;execution_stress_not_positive)
- US_LEVERAGED_ETF/N20: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (window_concentration_too_high)
- US_STOCK/N100: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (insufficient_windows;primary_not_positive;low_positive_ratio;low_profit_factor;window_concentration_too_high;execution_stress_not_positive)
- US_STOCK/N20: `SEMICONDUCTOR_GRID_NOT_VALIDATED` (primary_not_positive;low_positive_ratio;low_profit_factor;window_concentration_too_high;execution_stress_not_positive)

## 5. 成本与库存诊断

- best primary pnl: `11.74360926114569`
- hard fail reasons: `best_window_concentration, validation_positive, final_oos_positive`

## 6. 执行压力结果

- PRIMARY_ZERO_MAKER: `11.74360926114569`
- EXECUTION_STRESS: `7.6165802774560944`
- MAKER_PROMO_OFF: `7.930730924740238`

## 7. Final OOS

- Development: `13.16298767600166`
- Validation: `-1.2498158863890336`
- Final OOS: `-0.16956252846693676`

## 8. 验收门槛

- minimum_unique_windows: threshold=8 actual=9 `PASS`
- positive_window_ratio: threshold=0.55 actual=0.5555555555555556 `PASS`
- profit_factor: threshold=1.05 actual=2.8608592761360985 `PASS`
- max_drawdown_pct_of_capital: threshold=0.05 actual=0.0129540342091238 `PASS`
- mean_inventory_drag_ratio: threshold=0.35 actual=0.0 `PASS`
- best_window_concentration: threshold=0.35 actual=0.40777771230686527 `FAIL`
- PRIMARY_ZERO_MAKER_net_pnl: threshold=0.0 actual=11.74360926114569 `PASS`
- EXECUTION_STRESS_net_pnl: threshold=0.0 actual=7.6165802774560944 `PASS`
- validation_positive: threshold=0.0 actual=-1.2498158863890336 `FAIL`
- final_oos_positive: threshold=0.0 actual=-0.16956252846693676 `FAIL`
- positive_seed_count: threshold=4 actual=6 `PASS`

## 9. 最终结论代码

`FAIL_NO_ROBUST_EDGE`

本轮没有稳健候选时，应停止参数搜索并保持自动开仓关闭。

## 10. 测试记录

- 相关套件: python -m pytest -q tests/test_grid_viability.py tests/test_scheduler.py tests/test_scheduler_markets.py tests/test_semiconductor_grid_strategy.py tests/test_semiconductor_grid_backtest_helpers.py tests/test_semiconductor_grid_rules.py => 20 passed
- 完整套件: python -m pytest -q => 753 passed, 3 failed (tests/test_controller.py 既有 take-profit 相关失败，与本轮 v2.7.1 研究改动无关)
- compileall: python -m compileall core strategy scripts tests => exit 0
- 本轮没有稳健候选，停止参数搜索，保持自动开仓关闭。
