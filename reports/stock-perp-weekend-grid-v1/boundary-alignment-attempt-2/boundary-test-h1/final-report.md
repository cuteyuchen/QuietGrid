# H1：股票永续周末/节假日低波动假设

本报告严格在任何网格参数搜索、B0–B5、Validation 或 Short OOS 之前生成。

- Tier A-Core：`8`
- Development W/O/R：`{'W': 103, 'O': 20, 'R': 570}`
- Development 日历窗口 W/O/R：`{'W': 17, 'O': 7, 'R': 144}`
- W realized volatility 中位数：`0.02164982170394055`
- O realized volatility 中位数：`0.012646791911822532`
- R realized volatility 中位数：`0.05551298260342722`

| H1 门槛 | 结果 |
| --- | --- |
| 至少 3 个 Tier A-Core | **PASS** |
| W 波动率不高于 O/R 的 90% | **FAIL** |
| W directional efficiency 不高于 O/R | **FAIL** |
| W fee-adjusted cycle capacity 不低于 O/R | **FAIL** |
| W fee-adjusted capacity 中位数严格为正 | **PASS** |
| 至少 60% 完整月份同方向优势 | **FAIL** |
| 优势不只来自一个标的 | **FAIL** |
| 最佳窗口贡献不超过 35% | **FAIL** |
| 日历窗口 block bootstrap 支持非随机优势 | **FAIL** |
| 排除上市前 14 天后方向不反转 | **FAIL** |
| W 成交质量不显著差于 O/R | **FAIL** |

失败门槛：`volatility_not_higher_than_90pct, directional_efficiency_not_higher, cycle_capacity_not_lower, sixty_percent_month_advantage, advantage_not_one_symbol, best_window_contribution_le_35pct, bootstrap_supports_non_noise, mature_stage_direction, execution_quality`

## 结论：`STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED`

H1 失败后已停止参数优化和策略回测；没有读取 Validation/Short OOS，也没有修改生产默认配置。


`baseline-comparison.csv`, B0–B5、Validation 和 Short OOS 均未运行（H1 gate failed or not opened）。
