# QuietGrid 半导体休市网格 v2.7.2 修复规范

> 状态：`REQUIRED_BEFORE_RETEST`
>
> 适用分支：`codex/semiconductor-grid-backtest-run-v2.7.1`
>
> 目标版本：`semiconductor-grid-v2.7.2`
>
> 本文只定义方法、实现与审计修复，不允许借修复之名调整策略参数或扩大策略范围。

---

## 1. 修复目的

v2.7.1 已完成四标的、四 Profile、三执行场景和六随机种子的固定矩阵回测，但最终报告不能直接作为策略经济性终判。

原因不是数据完全不可用，而是回测实现存在会影响正式结论的关键偏差：

1. 未结束的休市窗口被当作完整 Final OOS，并在数据末尾强制平仓；
2. 窗口尾部同时扣除了 `force_close_minutes` 和 `minimum_trade_minutes`，导致交易比策略定义提前约两小时结束；
3. 库存拖累和强制退出库存指标在平仓后读取，因此系统性显示为零；
4. 回测把 `regime_score` 固定为 100，没有复现生产 Regime Engine；
5. 单窗口只建立一次固定网格，没有复现移动中枢、滚动重建、冷静期和再次入场；
6. v2.7.1 已经读取并报告过历史 Validation / Final OOS，因此这些区间不能继续称为全新封存样本；
7. 完整测试仍有 3 个 Controller 止盈语义测试失败，仓库不能标记为全绿。

因此，v2.7.1 的正式研究状态重新定义为：

```text
FAIL_IMPLEMENTATION_PENDING_RETEST
```

交易安全结论保持不变：

```text
startup_auto_entry = false
禁止自动实盘
禁止自动测试网开仓
禁止提高杠杆
```

---

## 2. 不允许变化的策略基线

修复期间必须冻结以下策略输入：

### 2.1 标的

```text
SNDKUSDT
MUUSDT
SOXLUSDT
SKHYNIXUSDT
```

### 2.2 日历

| 标的 | 市场组 | 日历 |
|---|---|---|
| SNDKUSDT | US_STOCK | NYSE |
| MUUSDT | US_STOCK | NYSE |
| SOXLUSDT | US_LEVERAGED_ETF | NYSE |
| SKHYNIXUSDT | KR_STOCK | XKRX |

### 2.3 Profile

```text
N20
N100
L20
L100
```

约束：

- SNDK、MU、SOXL 只允许 N20、N100；
- SKHYNIX 允许 N20、N100、L20、L100；
- LONG 信号只能使用当时已闭合数据；
- 不得根据最终价格或窗口盈亏反向标注方向。

### 2.4 执行场景

```text
PRIMARY_ZERO_MAKER
EXECUTION_STRESS
MAKER_PROMO_OFF
```

### 2.5 固定随机种子

```text
3, 10, 17, 31, 59, 97
```

### 2.6 资金与杠杆

```text
economic_leverage = 1
capital_per_symbol = 500 USDT
SOXL capital_multiplier = 0.5
```

修复提交中不得修改上述输入。

---

## 3. P0 修复项

P0 项未完成时，禁止生成新的策略结论。

## 3.1 P0-A：排除未完成窗口

### 当前问题

窗口生成器会将数据结尾处尚未达到正常策略退出边界的窗口加入样本，并在最后一根数据上执行强制平仓。

这会把“数据下载截止”错误解释为“策略退出事件”。

### 正确规则

一个正式窗口必须同时满足：

```text
window.previous_market_close 存在
window.force_close_at 存在
window.next_reference_open 存在
最后一根闭合 K 线时间 >= force_close_at 前最后一个完整 1m Bar
交易段长度 >= minimum_trade_rows
观察段长度 >= observation_rows
```

对 1m 数据，完整窗口推荐判断：

```python
last_close_time >= force_close_at - 1 millisecond
```

或使用严格等价的分钟边界判断。

### 未完成窗口处理

未完成窗口必须：

- 标记为 `INCOMPLETE_WINDOW`；
- 写入 `blocked-windows.csv`；
- 不进入 Development、Validation、Final OOS；
- 不参与 Gate 通过率；
- 不参与收益、集中度、随机种子和月份统计；
- 不执行数据末尾强制平仓。

### 必须新增测试

1. 当前周末仅有半段数据时，窗口被阻断；
2. 数据刚好覆盖到 `force_close_at` 时，窗口有效；
3. 数据超过 `force_close_at` 时，只截取到退出边界；
4. 未完成窗口不得改变 Final OOS 净收益；
5. 未完成窗口不得计入 `total_unique_windows`。

---

## 3.2 P0-B：修正窗口交易结束时间

### 当前问题

v2.7.1 使用：

```python
trade_end = next_reference_open - (
    force_close_minutes + minimum_trade_minutes
)
```

这把两个不同概念叠加扣除：

- `force_close_minutes`：真正的交易退出边界；
- `minimum_trade_minutes`：观察结束后是否还有足够交易时间的准入条件。

结果是每个窗口比策略定义提前约 120 分钟停止。

### 正确规则

```python
force_close_at = next_reference_open - timedelta(minutes=force_close_minutes)
trade_end = force_close_at
```

`minimum_trade_minutes` 只用于窗口资格：

```python
observation_end = previous_market_close + observation_duration
remaining_trade_minutes = force_close_at - observation_end
eligible = remaining_trade_minutes >= minimum_trade_minutes
```

### 边界定义

对 1m K 线：

- 允许观察段从 `previous_market_close` 开始；
- 交易 Bar 必须满足 `bar.open_time >= observation_end`；
- 最后一根交易 Bar 必须满足 `bar.close_time < force_close_at` 或项目统一采用的等价闭区间规则；
- 到达 `force_close_at` 后，不再允许新增挂单；
- 在 `force_close_at` 执行撤单和库存退出。

### 必须新增测试

1. NYSE 普通周末正确运行至参考开盘前 120 分钟；
2. NYSE 节假日长周末使用相同规则；
3. XKRX 普通周末正确运行至开盘前 120 分钟；
4. `minimum_trade_minutes` 不改变窗口末端；
5. 观察结束后不足 120 分钟时窗口阻断；
6. 夏令时切换周边的 UTC 边界正确。

---

## 3.3 P0-C：修复库存拖累与强制退出指标

### 当前问题

v2.7.1 使用 `force_close_at_end=True`，然后从已完成平仓的 `BacktestResult` 读取：

```text
unrealized_pnl
net_position_qty
```

平仓后这两个值自然接近零，导致：

```text
inventory_drag_ratio = 0
force_close_inventory_notional = 0
```

即使运行过程中库存利用率很高、未配对头寸持续数百分钟，报告仍错误显示库存没有拖累。

### 回测结果模型必须新增字段

建议在 `BacktestResult` 中增加：

```text
pre_exit_position_qty
pre_exit_inventory_notional
pre_exit_unrealized_pnl
pre_exit_mark_price
pre_exit_timestamp
peak_negative_unrealized_pnl
peak_inventory_drag_ratio
inventory_realized_at_exit
force_exit_fees
force_exit_slippage_cost
force_exit_reason
```

### 指标定义

```text
pre_exit_inventory_notional
= abs(pre_exit_position_qty) × pre_exit_mark_price
```

```text
inventory_drag
= max(0, -pre_exit_unrealized_pnl)
```

```text
inventory_drag_ratio
= inventory_drag / max(gross_grid_pnl, epsilon)
```

```text
peak_inventory_drag_ratio
= max_t(max(0, -unrealized_pnl_t) / max(realized_grid_pnl_t, epsilon))
```

为避免早期毛利润接近零造成比率爆炸，必须同时报告绝对值，并可在比率统计中要求：

```text
realized_grid_pnl_t >= minimum_drag_denominator
```

但不得简单把异常大比率截断为零。

### 退出归因

总收益必须拆为：

```text
paired_grid_pnl
inventory_realized_pnl
funding_pnl
maker_fees
taker_fees
seed_cost
stop_exit_cost
force_exit_cost
net_pnl
```

### 必须新增测试

1. 结束前有多头库存时，`pre_exit_inventory_notional > 0`；
2. 结束前有浮亏时，`inventory_drag > 0`；
3. 平仓后净仓位为零，但 pre-exit 指标仍保留；
4. 强制退出费用只计一次；
5. 止损退出与窗口退出分别归因；
6. 盈利库存不能被错误记为负拖累；
7. 绝对拖累和拖累比率与手工算例一致。

---

## 3.4 P0-D：接入真实 Regime Engine

### 当前问题

v2.7.1 在候选生成时直接传入：

```python
regime_score = 100.0
```

这绕过了生产策略的波动、趋势、反转、流动性和成本综合评分。

### 正确实现

回测必须使用与生产一致的 Regime 计算函数，输入仅限观察期已闭合数据。

至少输出：

```text
regime_score
regime_state
volatility_component
trend_component
liquidity_component
mean_reversion_component
cost_component
hard_limit_reasons
soft_breach_count
```

### 禁止事项

- 不得用未来交易段重新计算入场分数；
- 不得因窗口最终盈利而修改准入；
- 不得仅为提高通过率而把分数固定为 100；
- 不得另写与生产不一致的简化 Regime 版本，除非报告中作为独立对照并明确命名。

### 必须新增测试

1. 明显单边观察期被 Regime 阻断；
2. 点差或深度硬限制失败时阻断；
3. 低方向性且流动性合格时可通过；
4. 同一观察数据在生产与回测得到相同评分；
5. 观察期之后的数据变化不影响既定入场分数。

---

## 4. P1 修复项

## 4.1 P1-A：增加生产一致的移动网格会话模拟器

### 当前问题

v2.7.1 是：

```text
观察 180 根
生成一次参数
整个窗口只运行一次固定网格
```

而策略配置包含：

```text
rolling_regrid_enabled = true
rolling_regrid_seconds = 7200
center_half_life_minutes = 30
cooldown
re-observe
inventory manager
```

### 目标

新增一个 Controller-faithful session simulator，至少实现：

```text
OBSERVING
ADMITTED
RUNNING
DEFENSIVE
COOLDOWN
REOBSERVING
FORCE_CLOSING
CLOSED
```

### 固定行为

- 初次观察 180 根已闭合 1m Bar；
- 通过 Scheduler、Regime 和 Viability 后才开网格；
- 每 120 分钟评估一次滚动重建；
- 重建只使用当时已闭合数据；
- 区间击穿或风控触发后进入 DEFENSIVE / COOLDOWN；
- 冷静条件满足后重新观察；
- 一个休市窗口允许多个 Session，但必须共享窗口级风险预算；
- 到 `force_close_at` 必须撤单并清仓。

### 比较基线

v2.7.2 必须同时保留：

```text
R0_STATIC_REPAIRED
R1_CONTROLLER_FAITHFUL
```

R0 用于与 v2.7.1 可比；R1 用于验证真实策略行为。

不得只报告表现更好的版本。

---

## 4.2 P1-B：时间划分与样本暴露重分类

v2.7.1 已读取并公开过历史 Development、Validation 和 Final OOS。

因此 v2.7.2 必须将截至本修复协议提交前已存在的数据统一标记为：

```text
RESEARCH_VALIDATION_EXPOSED
```

这些数据可以用于：

- 验证修复是否改变结果；
- 调试窗口与库存指标；
- 比较 R0 与 R1；
- 形成研究候选。

这些数据不能再用于宣称全新 Final OOS。

真正的 Forward OOS 定义为：

```text
本修复协议提交后，第一个完整结束的休市窗口起
```

Forward OOS 必须：

- 在运行前冻结规则和参数；
- 只在窗口完整结束后追加结果；
- 不得回写调整历史规则；
- 至少积累 4 个完整窗口才输出描述性判断；
- 至少积累 8 个完整窗口才允许正式稳定性判断。

---

## 4.3 P1-C：聚合与集中度审计

随机种子只是同一价格窗口下的执行不确定性复制，不是独立市场样本。

正确层级：

```text
seed run
→ symbol-window 平均或分布
→ calendar-window portfolio
→ split / symbol / profile / scenario 聚合
```

必须报告：

```text
seed_mean_pnl
seed_min_pnl
seed_max_pnl
seed_std_pnl
positive_seed_count
unique_market_windows
best_market_window_concentration
top_3_market_window_concentration
```

禁止把 6 个随机种子当成 6 个独立周末增加样本量。

当同一市场窗口包含多个标的时：

- 组合集中度按 calendar window 计算；
- 标的集中度单独计算；
- 不得因为标的拆分而重复计算同一周末。

---

## 4.4 P1-D：Funding 审计语义

当前 Funding coverage ratio 可能超过 1，因为预期事件数使用连续时长除以 8 小时估算。

修复要求：

- 覆盖率用于完整性判断时上限显示为 1.0；
- 额外事件单独报告为 `extra_event_count`；
- 检查 Funding 时间是否严格递增；
- 检查相邻事件间隔分布；
- 事件落在交易窗口内才计入该 Session；
- Funding 正负方向必须按实际持仓方向应用；
- 报告中区分 `funding_paid` 与 `funding_received`。

---

## 4.5 P1-E：修复完整测试失败

当前完整测试中 3 个失败集中在 Controller 止盈语义。

修复前必须先明确配置语义：

```text
take_profit_usdt 是立即平仓阈值
或
take_profit_usdt 是利润保护激活阈值
```

仓库只允许保留一种正式语义，并同步：

```text
README
config/config.yaml
Controller
RiskManager
ProfitProtection
单元测试
回测退出模型
```

不得通过删除测试或放宽断言解决失败。

完成标准：

```text
pytest -q
0 failed
```

---

## 5. 报告与审计输出修复

v2.7.2 至少新增或修正以下文件：

```text
reports/semiconductor-grid-backtest-v2.7.2/
  repair-manifest.json
  completed-window-manifest.csv
  incomplete-window-manifest.csv
  window-boundary-audit.md
  regime-breakdown.csv
  session-breakdown.csv
  regrid-breakdown.csv
  pre-exit-inventory-breakdown.csv
  exit-attribution.csv
  seed-distribution.csv
  static-vs-controller-summary.csv
  exposed-validation-summary.csv
  forward-oos-ledger.csv
  acceptance-gates.json
  results.json
  final-report.md
```

### `repair-manifest.json`

必须记录：

```text
base_commit_sha
repair_commit_sha
strategy_config_sha
backtest_script_sha
window_builder_sha
backtest_engine_sha
regime_engine_sha
run_started_at_utc
run_finished_at_utc
data_cutoff_utc
```

### `window-boundary-audit.md`

每个市场至少展示一个完整算例：

```text
previous_market_close
observation_end
force_close_at
next_reference_open
last_included_trade_bar
remaining_trade_minutes
window_complete
```

### `pre-exit-inventory-breakdown.csv`

至少包含：

```text
symbol
window_key
profile
scenario
seed
pre_exit_position_qty
pre_exit_inventory_notional
pre_exit_unrealized_pnl
peak_negative_unrealized_pnl
inventory_drag
inventory_drag_ratio
peak_inventory_drag_ratio
inventory_realized_at_exit
```

---

## 6. 代码修改范围

预计允许修改：

```text
scripts/semiconductor_grid_backtest.py
strategy/backtest.py
strategy/grid_viability.py
strategy/semiconductor_grid.py
core/scheduler.py（仅边界一致性修复）
core/models.py（仅结果字段）
回测专用 session simulator
tests/
docs/
```

未经单独人工批准，禁止修改：

```text
生产杠杆
生产资金上限
生产标的白名单
生产自动开仓开关
实盘 API 权限
实盘下单路径
正式止损阈值
Profile 参数
Viability 阈值
```

---

## 7. 修复验收标准

只有全部满足，才能开始 v2.7.2 正式复跑：

### 窗口

- [ ] 未完成窗口全部阻断；
- [ ] 交易结束点等于 `force_close_at`；
- [ ] `minimum_trade_minutes` 只作为准入条件；
- [ ] NYSE 与 XKRX 边界测试通过；
- [ ] 夏令时和长周末测试通过。

### 库存

- [ ] 强制退出前库存快照被保留；
- [ ] 库存拖累不再系统性为零；
- [ ] 退出损益归因可手工复核；
- [ ] 强制退出成本只计一次。

### 策略一致性

- [ ] Regime 不再固定为 100；
- [ ] 回测与生产 Regime 同输入同结果；
- [ ] R0 静态与 R1 Controller-faithful 分开报告；
- [ ] 不使用未来数据决定入场、方向或重建。

### 数据与样本

- [ ] 旧 OOS 重分类为 `RESEARCH_VALIDATION_EXPOSED`；
- [ ] Forward OOS 从修复协议提交后的完整窗口开始；
- [ ] 随机种子不被当成独立市场样本；
- [ ] Funding 审计语义正确。

### 测试

- [ ] `python -m compileall core strategy scripts tests` 通过；
- [ ] 新增修复测试通过；
- [ ] `pytest -q` 为 0 failed；
- [ ] 结果在相同输入和种子下可复现。

---

## 8. 修复后的结论边界

v2.7.2 修复复跑后，只允许输出以下之一：

```text
PASS_TESTNET_CANDIDATE
PASS_RESEARCH_ONLY_MAKER_DEPENDENT
FAIL_NO_ROBUST_EDGE
FAIL_EXECUTION_STRESS
FAIL_INVENTORY_TAIL
FAIL_INSUFFICIENT_DATA
FAIL_DATA_QUALITY
FAIL_IMPLEMENTATION
```

如果任何 P0 项仍失败，结论必须是：

```text
FAIL_IMPLEMENTATION
```

不得用策略收益结果覆盖实现失败。

---

## 9. Definition of Done

v2.7.2 修复工作完成的定义：

1. 窗口边界与原策略完全一致；
2. 未结束窗口不会进入任何正式样本；
3. 库存尾部风险能在平仓前被真实测量；
4. Regime、Viability、Scheduler 和执行模型均只使用当时可见数据；
5. 静态网格与生产一致会话模拟分别给出结果；
6. 已暴露历史与未来 Forward OOS 严格分离；
7. 完整测试全绿；
8. 参数保持冻结；
9. 自动开仓保持关闭；
10. 新报告能够被第三方仅凭清单、哈希和 CSV 复核。
