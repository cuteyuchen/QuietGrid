# QuietGrid 半导体休市网格 v2.7.2 修复复跑与 Forward OOS 协议

> 状态：`PRE-REGISTERED_RETEST_PROTOCOL`
>
> 依赖文档：`docs/semiconductor-grid-v2.7.2-repair-spec.md`
>
> 本协议用于在完成方法修复后，重新验证半导体休市窗口网格策略。它不是参数优化计划，也不是自动实盘部署计划。

---

## 1. 研究问题

本轮只回答以下问题：

> 在对应基础证券市场关闭的周末或节假日窗口内，使用开仓前可见的 Regime 与 Grid Viability 条件筛选后，SNDK、MU、SOXL、SKHYNIX 的中性或预注册做多网格，能否在真实时间边界、真实库存尾部、Funding、执行概率和退出成本约束下，产生稳定且可重复的正收益？

本轮同时比较：

```text
R0_STATIC_REPAIRED
R1_CONTROLLER_FAITHFUL
```

其中：

- R0：修复窗口、库存和 Regime 后的单次静态网格基线；
- R1：复现生产会话状态、滚动重建、冷静期和再次观察的移动网格版本。

R0 与 R1 必须使用同一数据、同一规则、同一随机种子和同一成本场景。

---

## 2. 预注册限制

### 2.1 禁止参数优化

v2.7.2 首次正式复跑必须冻结 v2.7.1 的：

```text
Profile
min/max grid count
min step
range multiplier
capital multiplier
Regime threshold
Viability threshold
LONG signal threshold
inventory threshold
stop threshold
cooldown threshold
regrid interval
```

本轮不得：

- 网格搜索；
- 格距搜索；
- Gate 阈值搜索；
- 标的选择搜索；
- 根据 v2.7.1 或 v2.7.2 的最终收益修改规则；
- 仅保留表现最好的月份；
- 删除亏损窗口；
- 用未来路径选择 LONG / NEUTRAL；
- 转向 Funding、Basis、趋势、跨资产价差或机器学习策略。

### 2.2 禁止自动交易

必须保持：

```text
startup_auto_entry = false
testnet_force_window = false
testnet_fast_observation = false
```

回测通过也不自动修改生产开关。

---

## 3. 标的、市场与策略矩阵

## 3.1 标的

```text
SNDKUSDT
MUUSDT
SOXLUSDT
SKHYNIXUSDT
```

## 3.2 市场日历

| 标的 | 市场组 | 日历 | 参考开盘 |
|---|---|---|---|
| SNDKUSDT | US_STOCK | NYSE | America/New_York 04:00 |
| MUUSDT | US_STOCK | NYSE | America/New_York 04:00 |
| SOXLUSDT | US_LEVERAGED_ETF | NYSE | America/New_York 04:00 |
| SKHYNIXUSDT | KR_STOCK | XKRX | XKRX regular open |

## 3.3 Profile

### N20

```text
direction_mode = NEUTRAL
grid_num = 3–20
```

### N100

```text
direction_mode = NEUTRAL
grid_num = 20–100
```

### L20

```text
direction_mode = LONG
grid_num = 3–20
requires_long_signal = true
```

### L100

```text
direction_mode = LONG
grid_num = 20–100
requires_long_signal = true
```

合法组合：

```text
SNDKUSDT   × N20/N100
MUUSDT     × N20/N100
SOXLUSDT   × N20/N100
SKHYNIXUSDT × N20/N100/L20/L100
```

## 3.4 执行场景

### PRIMARY_ZERO_MAKER

```text
maker_fee_rate = 0
正常 Taker 费
正常 Funding
maker_fill_probability = 0.65
max_fills_per_bar = 2
stop_slippage_bps = 10
```

### EXECUTION_STRESS

```text
maker_fee_rate = 0
Taker 费扩大
maker_fill_probability = 0.45
max_fills_per_bar = 1
stop_slippage_bps = 25
```

### MAKER_PROMO_OFF

```text
maker_fee_rate = 0.0002
正常 Taker 费
正常 Funding
maker_fill_probability = 0.65
max_fills_per_bar = 2
stop_slippage_bps = 15
```

## 3.5 随机种子

```text
3
10
17
31
59
97
```

---

## 4. 数据冻结与时间截止

## 4.1 数据要求

每个标的必须具有：

```text
1m K 线
quote volume
trade count
真实 Funding sidecar
冻结 exchangeInfo 交易规则
```

必须检查：

- 时间单调；
- 无重复时间；
- 无冲突 OHLC；
- 无未解释的 1m 缺口；
- 无未来时间；
- Funding 时间有序；
- tick size、step size、min qty、min notional 有效；
- 合约为 `TRADIFI_PERPETUAL` 且状态为 `TRADING`。

## 4.2 数据截止

运行时记录：

```text
data_cutoff_utc
```

窗口只有在 `force_close_at <= data_cutoff_utc` 且对应最后一根完整 Bar 已存在时才算完整。

任何跨越数据截止时刻的窗口均标记：

```text
INCOMPLETE_WINDOW
```

不得进入收益统计。

## 4.3 哈希

必须为以下输入计算 SHA256：

```text
每个 K 线文件
每个 Funding 文件
exchange-rules.json
config/config.yaml
回测脚本
Backtest engine
Regime engine
Scheduler
策略注册模块
```

---

## 5. 样本暴露与分区

## 5.1 已暴露样本

截至 v2.7.2 修复协议提交前已经在 v2.7.1 报告中出现的全部窗口，统一标记：

```text
RESEARCH_VALIDATION_EXPOSED
```

这些窗口用于：

- 修复回归测试；
- 比较 v2.7.1 与 v2.7.2；
- 比较 R0 和 R1；
- 形成研究候选排序。

不得再次称为未查看 Final OOS。

## 5.2 新 Forward OOS

Forward OOS 从本协议提交后的第一个完整休市窗口开始。

Forward OOS 规则：

1. 参数、配置、代码哈希在窗口开始前冻结；
2. 只有完整结束后才追加结果；
3. 不允许修改历史结果；
4. 不允许因某个窗口亏损而改变下一个窗口规则；
5. 每个新窗口追加一行不可变 ledger；
6. 至少 4 个完整窗口后才给描述性结论；
7. 至少 8 个完整窗口后才允许正式候选判断；
8. 少于 8 个窗口时，结论必须包含 `INSUFFICIENT_FORWARD_OOS`。

## 5.3 历史修复复跑的分层

为保持诊断价值，已暴露历史可按时间顺序拆为：

```text
EXPOSED_EARLY
EXPOSED_LATE
```

或保留旧的 Development / Validation / Final_OOS 标签，但报告标题必须加：

```text
EXPOSED
```

不得把旧标签理解为新的统计独立性。

---

## 6. 窗口生成与交易边界

每个合法窗口：

```text
previous_market_close
→ observation period
→ admitted trading period
→ force_close_at
→ next_reference_open
```

### 正确公式

```text
force_close_at
= next_reference_open - 120 minutes
```

```text
observation_end
= previous_market_close + 180 minutes
```

资格要求：

```text
force_close_at - observation_end >= 120 minutes
```

交易段：

```text
[observation_end, force_close_at)
```

`minimum_trade_minutes` 不能再次从尾部扣除。

### 必须保存的窗口字段

```text
symbol
market_group
calendar
window_kind
previous_market_close
observation_start
observation_end
trade_start
last_trade_bar
force_close_at
next_reference_open
complete
blocked_reason
```

### Window Kind

只允许：

```text
WEEKEND
HOLIDAY
```

普通工作日隔夜不得进入正式样本。

---

## 7. R0_STATIC_REPAIRED 运行规则

R0 目的：隔离方法修复对 v2.7.1 结果的影响。

### 7.1 入场

每个完整窗口：

1. 使用前 180 根闭合 Bar；
2. 运行真实 Regime Engine；
3. 运行 Grid Viability Gate；
4. LONG Profile 运行预注册 LONG signal；
5. 全部通过后建立一次网格。

### 7.2 运行

- 网格参数在 Session 开始时冻结；
- 不滚动重建；
- 不在止损后再次入场；
- 到止损或 `force_close_at` 结束；
- 必须保留平仓前库存快照。

### 7.3 用途

R0 只回答：

> 在窗口、库存和 Regime 方法正确后，v2.7.1 的静态网格结果是否仍成立？

---

## 8. R1_CONTROLLER_FAITHFUL 运行规则

R1 目的：复现仓库生产策略的实际会话语义。

## 8.1 状态机

至少实现：

```text
OBSERVING
BLOCKED
RUNNING
DEFENSIVE
COOLDOWN
REOBSERVING
FORCE_CLOSING
CLOSED
```

## 8.2 初次开仓

只有以下全部通过才开仓：

```text
Scheduler window allowed
minimum remaining trade time
Regime admitted
Grid Viability admitted
LONG signal admitted（仅 L Profile）
exchange rules valid
capital and inventory limits valid
```

## 8.3 滚动重建

使用生产配置固定周期：

```text
rolling_regrid_seconds = 7200
```

每次评估只能使用当时已闭合数据。

重建报告必须记录：

```text
regrid_timestamp
old_center
new_center
old_step
new_step
old_grid_num
new_grid_num
reason
canceled_order_count
inventory_before
inventory_after
regrid_cost
```

禁止无成本瞬移网格。

## 8.4 防守与冷静期

当区间击穿、库存风险或 Regime 恶化时：

- 停止增加风险；
- 撤销不合适挂单；
- 按生产规则被动或主动减仓；
- 进入 COOLDOWN；
- 满足冷静条件后重新观察；
- 重新入场仍必须通过 Regime 与 Viability。

## 8.5 窗口风险预算

同一休市窗口内多个 Session 必须共享：

```text
max_window_loss
max_stop_count
max_consecutive_session_losses
capital_limit
```

不得每次重启都重置全部风险预算。

## 8.6 强制离场

到 `force_close_at`：

1. 停止新增订单；
2. 撤销全部未成交订单；
3. 记录 pre-exit inventory snapshot；
4. 使用退出成本模型清仓；
5. 记录库存实现损益；
6. Session 与窗口均关闭。

---

## 9. 成交与成本模型

每次成交必须遵守：

```text
tick size
step size
min qty
min notional
max fills per bar
maker fill probability
同 Bar 成交顺序约束
库存上限
```

成本必须至少包括：

```text
maker fees
taker fees
funding paid
funding received
seed execution cost
stop exit cost
force exit cost
regrid cost
slippage
```

禁止：

- 所有触价订单必成交；
- 同一根 Bar 同时使用未来高低点选择最优顺序；
- 把止损和窗口退出当 Maker；
- 忽略最终库存；
- 忽略重建撤单和再挂单影响；
- 把 Funding 绝对值一律当成本而忽略持仓方向。

---

## 10. 必须输出的指标

## 10.1 收益

```text
paired_grid_pnl
inventory_realized_pnl
funding_paid
funding_received
maker_fees
taker_fees
seed_cost
stop_exit_cost
force_exit_cost
regrid_cost
net_pnl
```

## 10.2 风险

```text
max_drawdown
max_drawdown_pct
worst_window_pnl
worst_5pct_mean
CVaR_95
max_session_loss
max_window_loss
stop_count
```

## 10.3 库存

```text
max_inventory_utilization
mean_inventory_utilization
pre_exit_position_qty
pre_exit_inventory_notional
pre_exit_unrealized_pnl
peak_negative_unrealized_pnl
inventory_drag
inventory_drag_ratio
peak_inventory_drag_ratio
max_unpaired_lots
max_unpaired_lot_age
```

## 10.4 网格质量

```text
grid_num
step_pct
crossings_per_hour
reversal_ratio
pair_completion_count
attempted_fill_count
accepted_fill_count
rejected_fill_count
net_capacity_per_hour
```

## 10.5 Regime 与 Gate

```text
regime_score
regime_state
regime_component_scores
hard_limit_reasons
gate_pass
gate_block_reasons
passed_but_lost
blocked_but_would_have_profited（仅诊断，不用于调参）
```

## 10.6 会话

```text
session_count
regrid_count
cooldown_count
reentry_count
session_duration
window_active_ratio
force_close_count
```

## 10.7 集中度

```text
best_market_window_concentration
top_3_market_window_concentration
best_symbol_contribution
month_contribution
positive_seed_count
seed_std_pnl
```

---

## 11. 聚合规则

### 11.1 随机种子

六个种子属于同一市场窗口的执行复制。

先聚合：

```text
symbol × window × profile × scenario × engine_mode
```

输出种子均值、最小值、最大值、标准差与正种子数。

### 11.2 市场窗口

同一 calendar window 下多个标的的组合收益相加，但不得把同一窗口重复计为多个独立时间样本。

### 11.3 分组

至少按以下维度报告：

```text
engine_mode: R0/R1
symbol
market_group
profile
scenario
exposure_split
calendar month
window kind
seed
```

---

## 12. 验收门槛

## 12.1 实现门槛

任何一项失败，直接结论：

```text
FAIL_IMPLEMENTATION
```

实现门槛包括：

- 无未完成窗口进入正式统计；
- 交易终点为 `force_close_at`；
- pre-exit 库存指标有效；
- Regime 不固定为 100；
- 同输入同种子可复现；
- 全部测试 0 failed；
- 无 look-ahead；
- 旧 OOS 已正确标记为 exposed。

## 12.2 历史已暴露研究候选门槛

对 `RESEARCH_VALIDATION_EXPOSED`，候选至少满足：

```text
unique_complete_windows >= 8
positive_window_ratio >= 0.55
profit_factor >= 1.05
max_drawdown_pct <= 0.05
mean_inventory_drag_ratio <= 0.35
best_market_window_concentration <= 0.35
PRIMARY_ZERO_MAKER net_pnl > 0
EXECUTION_STRESS net_pnl > 0
positive_seed_count >= 4
```

Maker 优惠取消为负时，必须标记：

```text
MAKER_DEPENDENT
```

## 12.3 Forward OOS 门槛

只有 Forward OOS 至少 8 个完整窗口时，才允许正式通过：

```text
Forward OOS net_pnl > 0
Forward OOS profit_factor >= 1.05
Forward OOS positive_window_ratio >= 0.50
Forward OOS best_window_concentration <= 0.50
EXECUTION_STRESS Forward OOS net_pnl > 0
无单一标的贡献超过全部正收益的 70%
```

Forward OOS 少于 8 个窗口时只能输出：

```text
RESEARCH_CANDIDATE_AWAITING_FORWARD_OOS
```

不能输出 `PASS_TESTNET_CANDIDATE`。

---

## 13. 标的级决策规则

本轮必须分别决定，不得只使用大组平均掩盖差异。

### SNDK

重点检查：

- 少数大赚、多数小亏是否仍存在；
- Regime 与 Viability 能否在开仓前识别盈利窗口；
- 窗口尾部新增两小时是增益还是风险；
- pre-exit 库存是否吞噬配对利润。

### MU

重点检查：

- 修复后是否仍为显著负收益；
- 若仍负，不允许因 SNDK 盈利而保留在同一生产池；
- 不进行参数救援。

### SOXL

重点检查：

- N20 的正收益是否在完整窗口和正确边界下保留；
- Validation 负收益是否稳定存在；
- R1 移动网格是否降低单窗口集中度；
- 杠杆 ETF 的库存尾部是否被低估。

### SKHYNIX

重点检查：

- L100/L20 样本是否仍不足；
- LONG signal 是否真实通过而非固定 Regime；
- KRX 边界与 Funding 是否正确；
- 不足 8 个完整窗口时只做描述性结论。

---

## 14. 运行阶段

## Phase 0：修复验证

执行：

```bash
python -m compileall core strategy scripts tests
pytest -q
```

要求：

```text
0 failed
```

然后运行：

- 窗口边界单元测试；
- 未完成窗口测试；
- pre-exit 库存测试；
- Regime 一致性测试；
- R0/R1 可复现测试；
- look-ahead 测试。

Phase 0 失败时停止。

## Phase 1：R0_STATIC_REPAIRED

运行全部合法：

```text
Symbol × Profile × Scenario × Seed
```

不进行任何参数搜索。

输出 v2.7.1 与 v2.7.2 R0 差异归因：

```text
incomplete-window exclusion
corrected window end
inventory accounting
regime admission
other
```

## Phase 2：R1_CONTROLLER_FAITHFUL

使用相同冻结输入运行生产一致状态机。

比较：

```text
R1 - R0 net pnl
R1 - R0 drawdown
R1 - R0 inventory drag
R1 - R0 concentration
R1 regrid cost
R1 session count
```

## Phase 3：历史已暴露结论

给出每个标的和 Profile 的研究状态：

```text
REJECT
RESEARCH_CANDIDATE
INSUFFICIENT_DATA
IMPLEMENTATION_BLOCKED
```

不得在此阶段输出全新 OOS 通过。

## Phase 4：Forward OOS Ledger

修复代码和规则冻结后，只追加后续完整窗口。

Forward OOS 不进行批量历史重跑替代。

---

## 15. 必须生成的文件

输出目录：

```text
reports/semiconductor-grid-backtest-v2.7.2/
```

至少包含：

```text
repair-manifest.json
dependency-manifest.json
input-hash-manifest.json
exchange-rules.json
data-audit.json
data-audit.md
completed-window-manifest.csv
incomplete-window-manifest.csv
window-boundary-audit.md
window-overlap-audit.md
regime-breakdown.csv
grid-viability-breakdown.csv
session-breakdown.csv
regrid-breakdown.csv
pre-exit-inventory-breakdown.csv
exit-attribution.csv
window-results-r0.csv
window-results-r1.csv
seed-distribution.csv
symbol-breakdown.csv
profile-scenario-summary.csv
static-vs-controller-summary.csv
exposed-validation-summary.csv
forward-oos-ledger.csv
acceptance-gates.json
results.json
final-report.md
compileall.stdout.log
compileall.stderr.log
pytest.stdout.log
pytest.stderr.log
backtest-r0.stdout.log
backtest-r0.stderr.log
backtest-r1.stdout.log
backtest-r1.stderr.log
```

---

## 16. 最终报告结构

`final-report.md` 必须按以下顺序：

### 1. 实现状态

直接回答：

```text
P0 修复是否全部完成？
完整测试是否 0 failed？
是否存在未完成窗口污染？
库存指标是否有效？
Regime 是否真实接入？
```

### 2. 数据与样本暴露

说明：

```text
data_cutoff_utc
完整窗口数
未完成窗口数
已暴露历史边界
Forward OOS 起点
```

### 3. v2.7.1 与 R0 差异

逐项归因，不得只报总收益变化。

### 4. R0 固定网格结果

按标的、Profile、场景分别展示。

### 5. R1 Controller-faithful 结果

展示滚动重建、冷静期、再入场和成本。

### 6. 库存与退出归因

重点回答：

```text
配对利润有多少被库存吞噬？
窗口退出前平均库存多少？
最差库存尾部发生在哪些窗口？
```

### 7. 稳健性

比较：

```text
PRIMARY_ZERO_MAKER
EXECUTION_STRESS
MAKER_PROMO_OFF
六随机种子
```

### 8. 标的级结论

分别给 SNDK、MU、SOXL、SKHYNIX 状态。

### 9. Forward OOS

历史已暴露数据不得放进这一节冒充 Forward OOS。

### 10. 验收门槛

逐项列出：

```text
门槛
实际值
PASS/FAIL/NOT_ENOUGH_DATA
```

### 11. 最终结论代码

只能使用：

```text
PASS_TESTNET_CANDIDATE
PASS_RESEARCH_ONLY_MAKER_DEPENDENT
RESEARCH_CANDIDATE_AWAITING_FORWARD_OOS
FAIL_NO_ROBUST_EDGE
FAIL_EXECUTION_STRESS
FAIL_INVENTORY_TAIL
FAIL_INSUFFICIENT_DATA
FAIL_DATA_QUALITY
FAIL_IMPLEMENTATION
```

---

## 17. 停止条件

出现以下任一情况，停止研究扩展：

- 修复后 SNDK、SOXL、SKHYNIX 所有合法候选均为负；
- R1 在执行压力下全部为负；
- pre-exit 库存尾部持续吞噬绝大多数配对利润；
- 收益继续高度集中于单一窗口；
- Gate 通过窗口不能优于 Gate 阻断窗口；
- 结果依赖未完成窗口；
- 结果依赖未来数据；
- 完整测试无法达到 0 failed；
- MU 仍显著负且没有独立、预注册的经济解释。

停止时输出：

```text
本轮未形成稳健候选，保持自动开仓关闭，不进行参数搜索。
```

---

## 18. 合并与部署限制

### 可以提交

- 修复代码；
- 测试；
- R0/R1 报告；
- 审计清单；
- Forward OOS ledger 框架。

### 不得自动合并或启用

- `startup_auto_entry = true`；
- 提高生产杠杆；
- 修改实盘白名单；
- 将 SKHYNIX 加入 NYSE 调度；
- 自动申请或扩大 API 权限；
- 在 Forward OOS 不足时启动测试网候选。

即使历史已暴露结果通过，也必须先完成人工代码审查和 Forward OOS 积累。

---

## 19. 完成定义

本轮完成必须同时满足：

1. P0 修复全部通过；
2. 完整测试 0 failed；
3. R0 与 R1 均完成固定矩阵；
4. 未完成窗口完全隔离；
5. 窗口尾部与策略定义一致；
6. 库存拖累和退出归因真实可复核；
7. Regime 与生产一致；
8. 历史已暴露数据没有冒充新 OOS；
9. Forward OOS ledger 已建立；
10. 自动开仓保持关闭。
