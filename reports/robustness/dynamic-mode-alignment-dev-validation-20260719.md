# QuietGrid 动态方向开发/验证诊断

生成时间：2026-07-19T11:16:12.922844+00:00

本报告只使用决策时已经闭合的历史 K 线；窗口后续涨跌不参与方向选择。  
终场前停止新增库存：1440 分钟。  
最大库存名义金额：200 USDT。  
最终 OOS 保持封存且未执行。

通过候选：0 / 48

## 固定方向基线

| 模式 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF |
|---|---:|---:|---:|---:|
| LONG | -66.21 | 0.070 | -25.90 | 0.090 |
| SHORT | -58.12 | 0.077 | -25.63 | 0.020 |
| NEUTRAL | -1.35 | 0.985 | 13.80 | 1.429 |

## 动态规则

| 规则 | 结果 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF | 开发模式分布 | 验证模式分布 |
|---|---|---:|---:|---:|---:|---|---|
| lb4320_dt1.20_nt0.35_p0.67_contrarian | 未通过 | 5.71 | 1.236 | -5.13 | 0.719 | LONG:3, NEUTRAL:24, PAUSE:76, SHORT:9 | LONG:2, NEUTRAL:17, PAUSE:33, SHORT:4 |
| lb4320_dt0.80_nt0.35_p0.67_contrarian | 未通过 | 4.99 | 1.200 | -5.66 | 0.699 | LONG:3, NEUTRAL:24, PAUSE:75, SHORT:10 | LONG:3, NEUTRAL:17, PAUSE:32, SHORT:4 |
| lb4320_dt1.60_nt0.35_p0.67_momentum | 未通过 | 9.59 | 1.467 | -5.46 | 0.671 | LONG:6, NEUTRAL:24, PAUSE:82 | LONG:3, NEUTRAL:17, PAUSE:36 |
| lb4320_dt1.60_nt0.35_p0.67_contrarian | 未通过 | 7.01 | 1.320 | -5.62 | 0.664 | NEUTRAL:24, PAUSE:82, SHORT:6 | NEUTRAL:17, PAUSE:36, SHORT:3 |
| lb4320_dt1.20_nt0.50_p0.67_contrarian | 未通过 | -0.77 | 0.981 | -6.75 | 0.675 | LONG:3, NEUTRAL:43, PAUSE:57, SHORT:9 | LONG:2, NEUTRAL:20, PAUSE:30, SHORT:4 |
| lb4320_dt1.60_nt0.35_p0.50_contrarian | 未通过 | 5.68 | 1.245 | -6.60 | 0.628 | NEUTRAL:24, PAUSE:79, SHORT:9 | NEUTRAL:17, PAUSE:31, SHORT:8 |
| lb4320_dt0.80_nt0.50_p0.67_contrarian | 未通过 | -1.49 | 0.964 | -7.28 | 0.658 | LONG:3, NEUTRAL:43, PAUSE:56, SHORT:10 | LONG:3, NEUTRAL:20, PAUSE:29, SHORT:4 |
| lb4320_dt1.60_nt0.50_p0.67_momentum | 未通过 | 3.12 | 1.084 | -7.08 | 0.629 | LONG:6, NEUTRAL:43, PAUSE:63 | LONG:3, NEUTRAL:20, PAUSE:33 |
| lb4320_dt1.60_nt0.50_p0.67_contrarian | 未通过 | 0.53 | 1.014 | -7.24 | 0.624 | NEUTRAL:43, PAUSE:63, SHORT:6 | NEUTRAL:20, PAUSE:33, SHORT:3 |
| lb4320_dt1.20_nt0.35_p0.67_momentum | 未通过 | 5.69 | 1.233 | -7.53 | 0.597 | LONG:9, NEUTRAL:24, PAUSE:76, SHORT:3 | LONG:4, NEUTRAL:17, PAUSE:33, SHORT:2 |
| lb4320_dt1.20_nt0.35_p0.50_contrarian | 未通过 | -2.97 | 0.910 | -8.63 | 0.604 | LONG:9, NEUTRAL:24, PAUSE:62, SHORT:17 | LONG:5, NEUTRAL:17, PAUSE:25, SHORT:9 |
| lb4320_dt1.60_nt0.50_p0.50_contrarian | 未通过 | -0.79 | 0.980 | -8.22 | 0.593 | NEUTRAL:43, PAUSE:60, SHORT:9 | NEUTRAL:20, PAUSE:28, SHORT:8 |
| lb4320_dt0.80_nt0.35_p0.67_momentum | 未通过 | 5.23 | 1.210 | -8.30 | 0.573 | LONG:10, NEUTRAL:24, PAUSE:75, SHORT:3 | LONG:4, NEUTRAL:17, PAUSE:32, SHORT:3 |
| lb4320_dt1.60_nt0.35_p0.50_momentum | 未通过 | 8.11 | 1.380 | -9.08 | 0.551 | LONG:9, NEUTRAL:24, PAUSE:79 | LONG:8, NEUTRAL:17, PAUSE:31 |
| lb4320_dt1.60_nt0.50_p0.50_momentum | 未通过 | 1.63 | 1.043 | -10.71 | 0.528 | LONG:9, NEUTRAL:43, PAUSE:60 | LONG:8, NEUTRAL:20, PAUSE:28 |
| lb4320_dt1.20_nt0.50_p0.67_momentum | 未通过 | -0.78 | 0.981 | -9.15 | 0.567 | LONG:9, NEUTRAL:43, PAUSE:57, SHORT:3 | LONG:4, NEUTRAL:20, PAUSE:30, SHORT:2 |

只有开发集和验证集同时通过收益、PF、回撤、覆盖率和集中度门槛，才允许进入参数邻域与 Walk-Forward 复验。
