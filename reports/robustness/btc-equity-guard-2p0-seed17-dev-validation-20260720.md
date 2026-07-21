# QuietGrid 逐窗口损失机制诊断

生成时间：2026-07-20T14:24:44.645350+00:00

参数：`neutral_r1.250_s0.00180_x0.0200`  
终场前停止新增库存：1440 分钟。  
最终 OOS 保持封存且未执行。

## 开发集损失分类

| 状态 | 原因 | 数量 | 总盈亏 | 配对网格盈亏 | 退出库存盈亏 | 退出费用 |
|---|---|---:|---:|---:|---:|---:|
| TRADED | equity_drawdown_guard | 23 | -22.30 | 20.76 | -39.96 | 1.18 |
| TRADED | window_force_close | 15 | 22.46 | 24.23 | 0.00 | 0.00 |
| BLOCKED | BLOCKED_SCORE | 13 | 0.00 | 0.00 | 0.00 | 0.00 |
| TRADED | stop_loss | 4 | 6.90 | 8.85 | -1.28 | 0.03 |
| TRADED | stop_loss_upper | 1 | 1.64 | 1.85 | 0.00 | 0.00 |

## 验证集损失分类

| 状态 | 原因 | 数量 | 总盈亏 | 配对网格盈亏 | 退出库存盈亏 | 退出费用 |
|---|---|---:|---:|---:|---:|---:|
| BLOCKED | 没有网格候选同时满足经济性与交易所下单约束。 | 8 | 0.00 | 0.00 | 0.00 | 0.00 |
| TRADED | equity_drawdown_guard | 6 | -4.69 | 5.21 | -9.07 | 0.30 |
| TRADED | window_force_close | 5 | 2.23 | 2.49 | 0.00 | 0.00 |
| BLOCKED | BLOCKED_SCORE | 3 | 0.00 | 0.00 | 0.00 | 0.00 |
| BLOCKED | 没有网格候选满足交易所最小下单量或最小名义金额。 | 3 | 0.00 | 0.00 | 0.00 | 0.00 |
| TRADED | stop_loss_upper | 2 | 1.67 | 1.87 | 0.00 | 0.00 |
| TRADED | stop_loss | 1 | 0.14 | 0.36 | 0.00 | 0.00 |

## 开发集最差窗口

| 标的 | 窗口 | 原因 | 盈亏 | 全程收益 | 最大上行 | 最大下行 | 入场方向 | 方向效率 |
|---|---|---|---:|---:|---:|---:|---:|---:|
| BTCUSDT | nyse_20240913T200000Z | equity_drawdown_guard | -2.27 | -3.20% | -0.03% | -4.01% | 1 | 0.316 |
| BTCUSDT | nyse_20240906T200000Z | equity_drawdown_guard | -2.07 | 1.93% | 3.18% | -0.05% | -1 | 0.575 |
| BTCUSDT | nyse_20250703T170000Z | equity_drawdown_guard | -1.95 | -0.65% | 0.20% | -2.29% | -1 | 0.060 |
| BTCUSDT | nyse_20241122T210000Z | equity_drawdown_guard | -1.93 | -0.80% | 0.02% | -3.10% | 1 | 0.379 |
| BTCUSDT | nyse_20250307T210000Z | equity_drawdown_guard | -1.91 | -5.16% | 0.12% | -7.81% | 1 | 0.144 |
| BTCUSDT | nyse_20250108T210000Z | equity_drawdown_guard | -1.90 | -0.85% | 0.33% | -4.02% | -1 | 0.137 |
| BTCUSDT | nyse_20241220T210000Z | equity_drawdown_guard | -1.56 | -3.05% | 1.72% | -4.06% | 1 | 0.521 |
| BTCUSDT | nyse_20241101T200000Z | equity_drawdown_guard | -1.52 | -0.47% | 0.64% | -2.82% | -1 | 0.011 |
| BTCUSDT | nyse_20250214T210000Z | equity_drawdown_guard | -1.43 | -2.18% | 0.49% | -2.31% | 1 | 0.239 |
| BTCUSDT | nyse_20250228T210000Z | equity_drawdown_guard | -1.42 | 8.67% | 12.53% | -0.59% | -1 | 0.122 |
| BTCUSDT | nyse_20241231T210000Z | equity_drawdown_guard | -1.30 | 2.16% | 2.61% | -0.64% | 1 | 0.169 |
| BTCUSDT | nyse_20250502T200000Z | stop_loss | -1.26 | -1.98% | 0.25% | -3.17% | 1 | 0.132 |
