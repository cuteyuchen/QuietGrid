# QuietGrid 动态方向开发/验证诊断

生成时间：2026-07-19T11:09:20.013914+00:00

本报告只使用决策时已经闭合的历史 K 线；窗口后续涨跌不参与方向选择。  
终场前停止新增库存：1440 分钟。  
最大库存名义金额：200 USDT。  
最终 OOS 保持封存且未执行。

通过候选：0 / 24

| 规则 | 结果 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF | 开发模式分布 | 验证模式分布 |
|---|---|---:|---:|---:|---:|---|---|
| lb4320_dt1.60_nt0.35_p0.67 | 未通过 | 9.59 | 1.467 | -5.46 | 0.671 | LONG:6, NEUTRAL:24, PAUSE:82 | LONG:3, NEUTRAL:17, PAUSE:36 |
| lb4320_dt1.60_nt0.50_p0.67 | 未通过 | 3.12 | 1.084 | -7.08 | 0.629 | LONG:6, NEUTRAL:43, PAUSE:63 | LONG:3, NEUTRAL:20, PAUSE:33 |
| lb4320_dt1.20_nt0.35_p0.67 | 未通过 | 5.69 | 1.233 | -7.53 | 0.597 | LONG:9, NEUTRAL:24, PAUSE:76, SHORT:3 | LONG:4, NEUTRAL:17, PAUSE:33, SHORT:2 |
| lb4320_dt0.80_nt0.35_p0.67 | 未通过 | 5.23 | 1.210 | -8.30 | 0.573 | LONG:10, NEUTRAL:24, PAUSE:75, SHORT:3 | LONG:4, NEUTRAL:17, PAUSE:32, SHORT:3 |
| lb4320_dt1.60_nt0.35_p0.50 | 未通过 | 8.11 | 1.380 | -9.08 | 0.551 | LONG:9, NEUTRAL:24, PAUSE:79 | LONG:8, NEUTRAL:17, PAUSE:31 |
| lb4320_dt1.60_nt0.50_p0.50 | 未通过 | 1.63 | 1.043 | -10.71 | 0.528 | LONG:9, NEUTRAL:43, PAUSE:60 | LONG:8, NEUTRAL:20, PAUSE:28 |
| lb4320_dt1.20_nt0.50_p0.67 | 未通过 | -0.78 | 0.981 | -9.15 | 0.567 | LONG:9, NEUTRAL:43, PAUSE:57, SHORT:3 | LONG:4, NEUTRAL:20, PAUSE:30, SHORT:2 |
| lb4320_dt0.80_nt0.50_p0.67 | 未通过 | -1.25 | 0.970 | -9.92 | 0.547 | LONG:10, NEUTRAL:43, PAUSE:56, SHORT:3 | LONG:4, NEUTRAL:20, PAUSE:29, SHORT:3 |
| lb4320_dt1.20_nt0.35_p0.50 | 未通过 | -3.41 | 0.896 | -12.73 | 0.467 | LONG:17, NEUTRAL:24, PAUSE:62, SHORT:9 | LONG:9, NEUTRAL:17, PAUSE:25, SHORT:5 |
| lb4320_dt1.20_nt0.50_p0.50 | 未通过 | -9.89 | 0.801 | -14.35 | 0.455 | LONG:17, NEUTRAL:43, PAUSE:43, SHORT:9 | LONG:9, NEUTRAL:20, PAUSE:22, SHORT:5 |
| lb1440_dt1.60_nt0.35_p0.50 | 未通过 | -15.08 | 0.605 | 0.70 | 1.060 | LONG:8, NEUTRAL:37, PAUSE:66, SHORT:1 | LONG:3, NEUTRAL:14, PAUSE:39 |
| lb1440_dt1.60_nt0.35_p0.67 | 未通过 | -15.08 | 0.605 | 0.70 | 1.060 | LONG:8, NEUTRAL:37, PAUSE:66, SHORT:1 | LONG:3, NEUTRAL:14, PAUSE:39 |
| lb4320_dt0.80_nt0.35_p0.50 | 未通过 | -12.38 | 0.723 | -18.63 | 0.374 | LONG:25, NEUTRAL:24, PAUSE:43, SHORT:20 | LONG:14, NEUTRAL:17, PAUSE:14, SHORT:11 |
| lb1440_dt1.20_nt0.35_p0.67 | 未通过 | -19.17 | 0.572 | -4.92 | 0.717 | LONG:15, NEUTRAL:37, PAUSE:54, SHORT:6 | LONG:5, NEUTRAL:14, PAUSE:32, SHORT:5 |
| lb1440_dt1.20_nt0.35_p0.50 | 未通过 | -20.50 | 0.556 | -5.78 | 0.683 | LONG:17, NEUTRAL:37, PAUSE:52, SHORT:6 | LONG:5, NEUTRAL:14, PAUSE:31, SHORT:6 |
| lb4320_dt0.80_nt0.50_p0.50 | 未通过 | -18.86 | 0.687 | -20.25 | 0.372 | LONG:25, NEUTRAL:43, PAUSE:24, SHORT:20 | LONG:14, NEUTRAL:20, PAUSE:11, SHORT:11 |

只有开发集和验证集同时通过收益、PF、回撤、覆盖率和集中度门槛，才允许进入参数邻域与 Walk-Forward 复验。
