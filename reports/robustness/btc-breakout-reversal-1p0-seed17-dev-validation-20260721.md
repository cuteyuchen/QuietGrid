# QuietGrid 逐窗口损失机制诊断

生成时间：2026-07-20T17:20:56.795108+00:00

参数：`neutral_r1.250_s0.00180_x0.0200`  
终场前停止新增库存：1440 分钟。  
最终 OOS 保持封存且未执行。

## 开发集损失分类

| 状态 | 原因 | 数量 | 总盈亏 | 配对网格盈亏 | 退出库存盈亏 | 退出费用 |
|---|---|---:|---:|---:|---:|---:|
| TRADED | window_force_close | 23 | 16.54 | 19.57 | 0.00 | 0.00 |
| BLOCKED | BLOCKED_SCORE | 15 | 0.00 | 0.00 | 0.00 | 0.00 |
| TRADED | breakout_reversal_stop_loss | 8 | -3.89 | 12.43 | -21.01 | 0.50 |
| TRADED | stop_loss | 7 | 1.84 | 3.55 | 0.00 | 0.00 |
| TRADED | stop_loss_upper | 2 | 1.57 | 1.98 | 0.00 | 0.00 |
| TRADED | breakout_reversal_stop_loss_upper | 1 | -0.58 | 0.54 | -2.32 | 0.06 |

## 验证集损失分类

| 状态 | 原因 | 数量 | 总盈亏 | 配对网格盈亏 | 退出库存盈亏 | 退出费用 |
|---|---|---:|---:|---:|---:|---:|
| BLOCKED | 没有网格候选同时满足经济性与交易所下单约束。 | 9 | 0.00 | 0.00 | 0.00 | 0.00 |
| TRADED | window_force_close | 7 | 0.88 | 1.49 | 0.00 | 0.00 |
| TRADED | stop_loss_upper | 4 | 0.38 | 1.08 | 0.00 | 0.00 |
| BLOCKED | BLOCKED_SCORE | 3 | 0.00 | 0.00 | 0.00 | 0.00 |
| BLOCKED | 没有网格候选满足交易所最小下单量或最小名义金额。 | 2 | 0.00 | 0.00 | 0.00 | 0.00 |
| TRADED | stop_loss | 2 | -1.59 | -1.13 | 0.00 | 0.00 |
| TRADED | breakout_reversal_stop_loss_upper | 1 | -2.89 | 1.55 | -2.78 | 0.07 |

## 开发集最差窗口

| 标的 | 窗口 | 原因 | 盈亏 | 全程收益 | 最大上行 | 最大下行 | 入场方向 | 方向效率 |
|---|---|---|---:|---:|---:|---:|---:|---:|
| BTCUSDT | nyse_20250108T210000Z | breakout_reversal_stop_loss | -6.21 | -0.85% | 0.33% | -4.02% | -1 | 0.137 |
| BTCUSDT | nyse_20250620T200000Z | breakout_reversal_stop_loss | -3.96 | -1.31% | 0.85% | -4.64% | -1 | 0.087 |
| BTCUSDT | nyse_20240830T200000Z | breakout_reversal_stop_loss | -3.90 | -0.06% | 0.98% | -3.41% | 1 | 0.182 |
| BTCUSDT | nyse_20250307T210000Z | breakout_reversal_stop_loss | -2.11 | -5.16% | 0.12% | -7.81% | 1 | 0.144 |
| BTCUSDT | nyse_20241122T210000Z | stop_loss | -1.82 | -0.80% | 0.02% | -3.10% | 1 | 0.379 |
| BTCUSDT | nyse_20240913T200000Z | stop_loss | -1.30 | -3.20% | -0.03% | -4.01% | 1 | 0.316 |
| BTCUSDT | nyse_20250502T200000Z | stop_loss | -1.28 | -1.98% | 0.25% | -3.17% | 1 | 0.132 |
| BTCUSDT | nyse_20250328T200000Z | stop_loss | -1.27 | -2.74% | 0.14% | -3.73% | 1 | 0.531 |
| BTCUSDT | nyse_20241101T200000Z | window_force_close | -0.74 | -0.47% | 0.64% | -2.82% | -1 | 0.011 |
| BTCUSDT | nyse_20240927T200000Z | window_force_close | -0.68 | -2.02% | 0.53% | -2.49% | -1 | 0.538 |
| BTCUSDT | nyse_20241231T210000Z | window_force_close | -0.67 | 2.16% | 2.61% | -0.64% | 1 | 0.169 |
| BTCUSDT | nyse_20241108T210000Z | breakout_reversal_stop_loss_upper | -0.58 | 5.83% | 6.97% | -0.98% | 1 | 0.315 |
