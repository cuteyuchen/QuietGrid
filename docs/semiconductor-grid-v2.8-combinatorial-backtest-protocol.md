# QuietGrid 半导体休市网格 v2.8 多维组合参数与交互效应回测协议

> 状态：`PRE_REGISTERED_MULTIVARIATE_RESEARCH_PROTOCOL`
>
> 适用仓库：`cuteyuchen/QuietGrid`
>
> 目标分支：`codex/semiconductor-grid-backtest-run-v2.7.1`
>
> 本协议替代“仅使用单一冻结参数即可否定整个策略家族”的解释。v2.7.2 只能证明当前参数化未通过，不能证明所有合理的波动区间、网格结构、利润保护、库存控制和止损机制均无效。

---

## 1. 研究结论边界

v2.7.2 的正确结论是：

```text
REJECT_CURRENT_PARAMETERIZATION
HISTORICAL_EDGE_NOT_VALIDATED
INSUFFICIENT_FORWARD_OOS
```

不得将其扩大为：

```text
REJECT_ALL_GRID_PARAMETERIZATIONS
```

v2.8 的核心任务不是继续验证单一配置，而是研究：

> 波动区间、网格结构、利润保护、库存控制和止损机制之间是否存在稳定的组合区域与可重复交互效应。

本轮必须采用组合参数设计，例如：

```text
11111
12111
13111
21111
22333
31445
```

禁止只使用：

```text
11111
21111
31111
41111
```

这种仅改变一个参数、其余参数长期固定的机械式回测作为最终依据。

---

## 2. 研究标的与方向模式

固定研究标的：

```text
SNDKUSDT
MUUSDT
SOXLUSDT
SKHYNIXUSDT
```

市场日历：

| 标的 | 参考市场 | 日历 | 允许方向 |
|---|---|---|---|
| SNDKUSDT | 美国股票市场 | NYSE | NEUTRAL |
| MUUSDT | 美国股票市场 | NYSE | NEUTRAL |
| SOXLUSDT | 美国股票市场 | NYSE | NEUTRAL |
| SKHYNIXUSDT | 韩国股票市场 | XKRX | NEUTRAL、LONG |

方向模式不是五位组合编码的一部分，组合 ID 使用后缀区分：

```text
22333-N
22333-L
```

其中：

```text
-N = NEUTRAL
-L = LONG
```

LONG 只允许用于 SKHYNIXUSDT，并且只能使用当时已经闭合的数据生成信号。

---

## 3. 五维组合编码

每个配置使用五位编码：

```text
ABCDE
```

含义：

```text
A = 波动准入与价格区间
B = 网格结构
C = 止盈与利润保护
D = 库存控制
E = 止损机制
```

止损后路径分析不是交易参数，不占编码位。所有发生止损的组合统一执行路径分析，未来路径不得回写改变当次交易结果。

---

## 4. A：波动准入与价格区间

所有倍率都以当前 v2.7.2 配置和对应标的自适应区间为基准。不得通过固定绝对价格宽度比较不同标的。

### A1：BASELINE_STRICT

```text
range_multiplier = 1.00 × baseline
max_volatility_expansion = 1.00 × baseline threshold
regime_score_threshold = baseline
```

用途：保留 v2.7.2 对照组。

### A2：BALANCED_WIDE

```text
range_multiplier = 1.50 × baseline
max_volatility_expansion = 1.25 × baseline threshold
regime_score_threshold = baseline - 5 points
```

仍必须满足：

```text
reversal_ratio >= baseline requirement
方向效率未触发 hard block
成交活跃度通过 Grid Viability Gate
```

### A3：ACTIVE_WIDE

```text
range_multiplier = 2.00 × baseline
max_volatility_expansion = 1.50 × baseline threshold
regime_score_threshold = baseline - 10 points
```

额外要求：

```text
reversal_ratio >= max(baseline, 0.35)
crossings_per_hour >= 1.5
zero_activity_ratio <= baseline limit
```

允许更高波动，但不允许高方向性单边行情被简单放行。

### A4：VOLATILITY_ADAPTIVE

区间倍率根据观察期波动分位数动态决定：

```text
vol_percentile <= 40%  -> 1.25 × baseline
40% < percentile <=70% -> 1.75 × baseline
70% < percentile <=90% -> 2.25 × baseline
percentile > 90%        -> BLOCK，除非方向效率与反转条件同时通过增强门槛
```

动态区间计算必须只使用窗口开始前可见历史。

### A 维度安全约束

1. 区间扩大时不得同步扩大最大资本风险。
2. 单格名义金额、最大库存和最大窗口损失必须独立受控。
3. 不得因为区间更宽而默认提高杠杆。
4. 高波动准入必须与反转、方向效率和活动度联合判断。

---

## 5. B：网格结构

格距必须大于执行成本地板：

```text
minimum_step
>= maker_round_trip_cost
 + expected_taker_exit_allocation
 + funding_allocation
 + slippage_buffer
 + minimum_target_edge
```

### B1：SPARSE

```text
target_grid_count = 5–10
minimum_step = max(1.50 × baseline normal min step, cost floor)
capital_per_level = 较高
```

用途：减少噪声成交和库存累积。

### B2：MEDIUM

```text
target_grid_count = 10–20
minimum_step = max(baseline normal min step, cost floor)
```

用途：接近当前 N20 基准。

### B3：DENSE

```text
target_grid_count = 20–50
minimum_step = max(0.08%, cost floor)
```

仅在 Maker 实际或研究场景为零、流动性与穿越门槛通过时运行。

### B4：ULTRA_DENSE

```text
target_grid_count = 50–100
minimum_step = max(0.05%, cost floor)
```

必须额外满足：

```text
crossings_per_hour >= 2.0
spread_to_step_ratio <= 0.35
zero_activity_ratio <= 0.10
trade_count_per_hour >= enhanced threshold
```

### B 维度安全约束

1. 网格数增加时，每层资本必须下降，组合最大风险不变。
2. 不得因为 Maker 为零而忽略 Taker、Funding、滑点和最终库存成本。
3. 50–100 格只属于研究配置，不得自动写入生产默认值。

---

## 6. C：止盈与利润保护

所有利润阈值使用净利润：

```text
net_pnl
= paired_grid_pnl
+ inventory_unrealized_pnl
+ inventory_realized_pnl
+ funding_received
- funding_paid
- maker_fees
- taker_fees
- estimated_exit_cost
```

### C1：NO_ACTIVE_PROFIT_EXIT

```text
无主动止盈
仅止损或窗口结束退出
```

用途：保留基准。

### C2：FIXED_NET_TAKE_PROFIT

```text
activation_and_close = 1.0% × capital_per_symbol
```

必须附加诊断阈值：

```text
0.5%
1.0%
1.5%
2.0%
```

这些子阈值用于收益曲线诊断。正式组合 C2 使用 1.0%，不得在同一阶段根据结果临时挑选。

### C3：PEAK_DRAWDOWN_PROTECTION

```text
activation = 1.0% × capital_per_symbol
peak drawdown 20% -> suppress inventory increase
peak drawdown 35% -> reduce inventory
peak drawdown 50% -> close
minimum_locked_profit = 25% of peak net profit
```

必须复现生产侧利润保护语义，而不是只在报告中描述。

### C4：INVENTORY_AWARE_PROTECTION

启动条件：

```text
net_pnl >= 0.5% × capital_per_symbol
paired_grid_pnl > 0
```

退出或减仓条件：

```text
inventory_drag_ratio >= 0.25 -> suppress same-direction risk
inventory_drag_ratio >= 0.40 -> reduce inventory
inventory_drag_ratio >= 0.60 -> close
```

同时保留峰值回撤 40% 的硬保护。

---

## 7. D：库存控制

库存拖累定义：

```text
inventory_drag
= max(0, -pre_exit_unrealized_pnl)
```

```text
inventory_drag_ratio
= inventory_drag / max(paired_grid_pnl, 0.01)
```

### D1：BASELINE_INVENTORY

使用当前 v2.7.2 库存规则。

### D2：BLOCK_SAME_SIDE_EARLY

```text
inventory_utilization >= 35%
或 inventory_drag_ratio >= 25%
-> 停止增加同方向库存
```

仍允许配对和平仓方向成交。

### D3：REDUCE_ONLY_ESCALATION

```text
utilization >= 35% -> block same side
utilization >= 50% -> reduce-only
inventory_drag_ratio >= 40% -> active reduction
```

### D4：AGGRESSIVE_TAIL_CONTROL

```text
utilization >= 25% -> block same side
utilization >= 40% -> reduce-only
inventory_drag_ratio >= 40% -> active reduction
inventory_drag_ratio >= 60% -> close
```

### D 维度评估要求

必须同时报告：

```text
paired_grid_pnl
inventory_realized_pnl
pre_exit_unrealized_pnl
peak_negative_unrealized_pnl
max_inventory_utilization
mean_inventory_utilization
max_unpaired_lots
max_unpaired_lot_age
```

不能只用最终净收益评价库存规则。

---

## 8. E：止损机制

所有止损均保留窗口级最大损失硬上限。扩大价格止损不等于取消风险预算。

### E1：BASELINE_STOP

使用当前 v2.7.2 上下区间止损。

### E2：HALF_ATR_BUFFER

```text
stop boundary = grid boundary + 0.5 ATR outward buffer
```

### E3：ONE_ATR_BUFFER

```text
stop boundary = grid boundary + 1.0 ATR outward buffer
```

### E4：TWO_ATR_BUFFER

```text
stop boundary = grid boundary + 2.0 ATR outward buffer
```

必须受窗口最大损失和库存上限约束。

### E5：TIME_CONFIRMED_STOP

```text
价格处于区间外
且超过 1.0 ATR buffer
且连续 30 根闭合 1m Bar 未回到区间
-> 触发止损
```

若库存或窗口风险预算先触发，则立即退出，不等待 30 分钟。

---

## 9. 必须包含的锚点组合

第一阶段至少包含下列组合，用于解释主效应和交互：

```text
11111  基准
12111  只改变网格结构
13111
14111
21111  只改变区间与波动准入
31111
41111
11211  固定止盈
11311  峰值保护
11411  库存感知利润保护
11121  早期阻止同向库存
11131  reduce-only 库存升级
11141  激进尾部库存控制
11112  0.5 ATR 止损
11113  1 ATR 止损
11114  2 ATR 止损
11115  时间确认止损
22333  1.5倍区间 + 中密度 + 峰值保护 + reduce-only + 1 ATR
22434  1.5倍区间 + 中密度 + 库存保护 + reduce-only + 2 ATR
31445  2倍区间 + 稀疏网格 + 库存保护 + 激进库存 + 时间确认
32333  2倍区间 + 中密度 + 峰值保护 + reduce-only + 1 ATR
42335  动态区间 + 中密度 + 峰值保护 + reduce-only + 时间确认
43445  动态区间 + 密集网格 + 库存保护 + 激进库存 + 时间确认
```

上述锚点用于解释，不代表预先认定其有效。

---

## 10. 组合设计方法

完整笛卡尔组合为：

```text
4 × 4 × 4 × 4 × 5 = 1280 个组合
```

直接全部运行会产生巨大的多重比较和过拟合风险。因此采用分阶段组合设计。

### Phase 0：实现校验

必须先完成：

1. 修复 R1 多 Session 最大回撤聚合，必须包含 Session 内部回撤。
2. 将 C2/C3/C4 利润保护真实接入回测引擎。
3. 将 A/B/D/E 参数作为显式不可变配置注入。
4. 所有运行保存 `combination_id`、完整参数快照和哈希。
5. 未完成窗口继续排除。
6. 完整 `pytest` 必须为 0 failed。
7. 自动开仓保持关闭。

任一 P0 项失败，不得进入正式矩阵。

### Phase 1：平衡组合筛选

从 1280 个候选中生成固定的 pairwise covering array：

```text
目标组合数：96
固定随机种子：20260801
覆盖强度：所有任意两个参数家族的等级组合至少出现 2 次
```

要求：

1. 包含全部锚点组合。
2. 每个 A/B/C/D/E 等级出现次数尽量均衡。
3. 每个两两交互都必须有覆盖。
4. 组合清单在运行前保存并计算 SHA256。
5. 不得在看到收益后替换组合。

Phase 1 所有组合先运行：

```text
R0_STATIC_REPAIRED
```

执行矩阵：

```text
combination
× legal symbol-direction profile
× 3 execution scenarios
× 6 seeds
× all complete exposed windows
```

### Phase 2：R1 Controller 验证

只有满足以下固定条件的 Phase 1 组合进入 R1：

```text
PRIMARY total_pnl > 0
PRIMARY median_window_pnl > 0
EXECUTION_STRESS total_pnl >= 0
profit_factor >= 1.05
positive_window_ratio >= 0.50
inventory_drag_ratio <= 0.75
best_window_concentration <= 0.50
```

另外，全部锚点组合无论表现如何都进入 R1，作为结构对照。

Phase 2 使用：

```text
R1_CONTROLLER_FAITHFUL
```

不得仅挑总收益排名靠前的组合。

### Phase 3：局部完整组合

按 R1 结果寻找最多两个稳定区域。稳定区域不是单一最优点，而是相邻参数组合簇。

每个区域最多选择两个相邻等级，例如：

```text
A2/A3
B1/B2
C3/C4
D2/D3
E2/E3
```

形成：

```text
2 × 2 × 2 × 2 × 2 = 32 个局部全组合
```

最多两个区域，共不超过 64 个组合。

选择区域必须依据预注册稳定性标准，不得人工凭收益截图决定。

### Phase 4：时间顺序验证

已暴露历史按时间顺序拆分：

```text
EXPOSED_EARLY  = 前 60%
EXPOSED_LATE   = 后 40%
```

Phase 1 与局部结构发现主要使用 EXPOSED_EARLY。

候选必须在 EXPOSED_LATE 独立满足：

```text
net_pnl > 0
median_window_pnl >= 0
profit_factor >= 1.05
inventory_drag_ratio <= 0.75
EXECUTION_STRESS net_pnl >= 0
```

不得在查看 EXPOSED_LATE 后返回修改参数再重新称其为验证。

### Phase 5：冻结 Forward OOS

最终最多保留：

```text
2 个 NEUTRAL 候选
1 个 SKHYNIX LONG 候选
```

冻结：

```text
代码 SHA
配置 SHA
组合 ID
交易规则 SHA
数据截止时间
执行场景
```

Forward OOS 至少 8 个完整窗口后才允许正式候选判断。

---

## 11. 相邻参数稳定性

不得选择孤立最优点。

对每个组合定义 Hamming 邻居：只有一位不同，且该位为相邻等级。

示例：

```text
22333 的邻居包括：
12333
32333
21333
23333
22233
22433
22323
22343
22332
22334
```

候选至少满足：

```text
>= 50% 可用邻居净收益为正
>= 40% 可用邻居执行压力结果非负
邻居净收益中位数为正
候选收益不得超过邻居中位数 5 倍
```

若只有一个参数点盈利、相邻组合普遍亏损，则标记：

```text
ISOLATED_OPTIMUM_REJECTED
```

---

## 12. 交互效应分析

必须计算并报告至少以下交互：

```text
A × B  波动区间与网格密度
A × E  区间宽度与止损缓冲
B × D  网格密度与库存控制
C × D  利润保护与库存控制
C × E  利润退出与止损机制
A × C  波动状态与利润保护
```

分析输出至少包括：

```text
main_effects.csv
pairwise_interactions.csv
interaction_heatmaps.csv
combination_neighborhoods.csv
```

不得仅输出“最佳组合”。

---

## 13. 止损后路径分析

每次因 E1–E5 或窗口风险预算退出后，继续读取未来路径用于诊断，但不得改变已实现收益。

记录：

```text
return_after_30m
return_after_60m
return_after_120m
return_after_240m
return_at_window_end
max_favorable_excursion_after_stop
max_adverse_excursion_after_stop
time_to_reenter_original_grid
reentered_within_30m
reentered_within_60m
reentered_within_120m
reentered_within_240m
```

分类：

### FALSE_BREAK_LIKELY

```text
120 分钟内重新进入原区间
且止损后最大继续偏离未超过额外 1 ATR
```

### TRUE_BREAK_LIKELY

```text
止损后继续向外超过 1 ATR
且 240 分钟内未重新进入原区间
```

### AMBIGUOUS

不满足上述任一条件。

必须按组合和 E 等级统计：

```text
false_break_ratio
true_break_ratio
median_reentry_time
post_stop_MAE
post_stop_MFE
```

该分析用于解释止损是否过紧，不允许在同一次运行中使用未来路径回撤止损决定。

---

## 14. 执行场景

保留三个固定场景：

### PRIMARY_ZERO_MAKER

```text
maker_fee = 0
正常 taker fee
真实 funding
正常成交概率与滑点
```

### EXECUTION_STRESS

```text
maker_fill_probability 下调
taker fee 提高
每 Bar 最大成交数下降
止损与强平滑点扩大
```

### MAKER_PROMO_OFF

```text
maker_fee = 0.02%
其他条件保持正常
```

组合必须在相同场景、相同种子和相同窗口中比较。

---

## 15. 随机种子

固定：

```text
3
10
17
31
59
97
```

额外要求：

```text
相同组合 + 相同窗口 + 相同场景 + 相同种子
= 完全可复现结果
```

不得为表现不佳的组合单独更换种子。

---

## 16. 评价顺序

候选选择采用过滤后排序，不得只按总收益排名。

### 16.1 硬过滤

必须满足：

```text
数据质量通过
没有未来数据
测试 0 failed
至少 4 个有效历史窗口用于探索
PRIMARY net_pnl > 0
PRIMARY median_window_pnl >= 0
EXECUTION_STRESS net_pnl >= 0
profit_factor >= 1.05
positive_window_ratio >= 0.50
inventory_drag_ratio <= 0.75
best_window_concentration <= 0.50
```

### 16.2 稳定性过滤

必须满足：

```text
相邻组合稳定性通过
至少 4/6 种子为正
EXPOSED_LATE 不反转为明显负值
结果不依赖单一标的或单一窗口
```

### 16.3 排序指标

通过过滤后，按以下顺序排序：

1. EXPOSED_LATE 中位窗口净收益；
2. EXECUTION_STRESS 净收益；
3. 最差 5% 窗口均值；
4. 库存拖累比例；
5. 相邻组合正收益比例；
6. 收益集中度。

总收益只作为次要指标。

---

## 17. 必须输出的指标

### 收益

```text
paired_grid_pnl
inventory_realized_pnl
net_pnl
median_window_pnl
mean_window_pnl
profit_factor
positive_window_ratio
```

### 风险

```text
max_drawdown
max_drawdown_pct
CVaR_95
worst_window_pnl
worst_5pct_mean
max_window_loss
```

### 库存

```text
inventory_drag
inventory_drag_ratio
pre_exit_inventory_notional
peak_negative_unrealized_pnl
max_inventory_utilization
mean_inventory_utilization
max_unpaired_lots
max_unpaired_lot_age
```

### 退出

```text
take_profit_count
profit_protection_suppress_count
profit_protection_reduce_count
profit_protection_close_count
stop_loss_count
window_force_close_count
inventory_forced_exit_count
```

### 网格质量

```text
grid_count
step_pct
crossings_per_hour
pair_completion_count
accepted_fill_count
rejected_fill_count
net_capacity_per_hour
```

### 稳定性

```text
seed_positive_count
best_window_concentration
top_3_window_concentration
neighbor_positive_ratio
neighbor_stress_nonnegative_ratio
symbol_contribution
month_contribution
```

---

## 18. 输出目录

```text
reports/semiconductor-grid-backtest-v2.8/
```

至少生成：

```text
run-manifest.json
repair-manifest.json
dependency-manifest.json
input-hash-manifest.json
combination-catalog.csv
combination-catalog.json
covering-array-audit.md
anchor-combinations.csv
phase1-r0-results.csv
phase2-r1-results.csv
phase3-local-factorial-results.csv
phase4-time-validation-results.csv
combination-summary.csv
combination-neighborhoods.csv
main-effects.csv
pairwise-interactions.csv
interaction-heatmaps.csv
profit-protection-breakdown.csv
inventory-control-breakdown.csv
stop-mechanism-breakdown.csv
post-stop-path-analysis.csv
symbol-breakdown.csv
scenario-breakdown.csv
seed-breakdown.csv
window-breakdown.csv
forward-oos-ledger.csv
acceptance-gates.json
final-report.md
pytest.stdout.log
pytest.stderr.log
backtest.stdout.log
backtest.stderr.log
```

JSON 清单必须实际提交，不得只生成在本地后遗漏。

---

## 19. 测试要求

新增测试至少覆盖：

```text
组合 ID 解析和序列化
所有因子等级映射
非法组合拒绝
固定 covering array 可复现
pairwise 覆盖审计
利润保护 C2/C3/C4
库存规则 D2/D3/D4
止损 E2/E3/E4/E5
时间确认止损不使用未来 Bar
止损后路径分析不回写交易结果
R1 多 Session 最大回撤
相邻组合计算
时间顺序拆分
Forward OOS 不可变 ledger
```

执行：

```bash
python -m compileall core strategy scripts tests
pytest -q
```

完整测试必须：

```text
0 failed
```

---

## 20. 生产安全边界

本轮不得修改：

```text
startup_auto_entry = false
testnet_force_window = false
testnet_fast_observation = false
leverage = 1x
生产资金上限
生产 Controller 自动准入
交易所下单逻辑
```

回测得到盈利组合也不自动部署。

---

## 21. 最终结论代码

只能从以下代码中选择：

```text
PASS_STABLE_PARAMETER_REGION_RESEARCH_ONLY
PASS_MAKER_DEPENDENT_REGION_RESEARCH_ONLY
REJECT_NO_STABLE_PARAMETER_REGION
REJECT_INVENTORY_TAIL_ACROSS_REGIONS
REJECT_EXECUTION_STRESS_ACROSS_REGIONS
REJECT_ISOLATED_OPTIMA_ONLY
FAIL_INSUFFICIENT_COMBINATION_COVERAGE
FAIL_INSUFFICIENT_DATA
FAIL_IMPLEMENTATION
```

若发现单一高收益点但相邻参数失败，必须使用：

```text
REJECT_ISOLATED_OPTIMA_ONLY
```

不得使用“最佳组合盈利”替代稳定区域判断。

---

## 22. 最终报告必须回答

1. 是否存在稳定盈利参数区域，而不是单一最优点？
2. 哪些 A×B、A×E、B×D、C×D 交互最重要？
3. 宽区间是否减少假突破，还是扩大真实趋势尾部？
4. 利润保护是否减少止损和窗口末库存损失？
5. 库存控制是否牺牲过多配对利润？
6. 密集网格在零 Maker 下是否仍被库存拖累吞噬？
7. SOXL、SNDK、MU、SKHYNIX 是否需要不同组合？
8. 哪些结果在 EXECUTION_STRESS 下仍然有效？
9. 结果是否依赖 Maker 免费？
10. Forward OOS 是否达到正式判断门槛？

---

## 23. 提交规则

实现与回测必须在独立研究分支完成。

建议新分支：

```text
codex/semiconductor-grid-combinatorial-backtest-v2.8
```

不得直接合并到 `master`。

完成后必须提交：

```text
代码
测试
组合清单
全部机器可读报告
最终报告
运行日志
哈希清单
```

提交信息建议：

```text
research: run multivariate semiconductor grid backtest v2.8
```

人工审查确认方法、测试和报告完整后，再决定是否建立 PR。

---

## 24. 本协议的核心原则

本轮不再用单一冻结参数草率否定整个策略，也不通过无边界暴力搜索制造历史赢家。

正确研究对象是：

```text
波动区间
× 网格结构
× 利润保护
× 库存控制
× 止损机制
```

正确判断标准是：

```text
稳定参数区域
+ 相邻组合一致性
+ 时间顺序验证
+ 执行压力存活
+ 库存尾部可控
+ Forward OOS
```

而不是：

```text
某一个组合的历史总收益最高
```
