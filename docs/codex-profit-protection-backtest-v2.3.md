# QuietGrid v2.3 净利润峰值保护与 Codex 回测目标

> 分支：`master`  
> 策略方向：继续使用 `NEUTRAL`，不启用动态 LONG/SHORT。  
> 本文既是实现说明，也是 Codex 下一轮回测、诊断和受约束调参的执行规范。

## 1. 修改背景

最近两年 BTCUSDT 与 ETHUSDT 的回测显示：

- 网格成交在震荡阶段能产生正的毛利润；
- 单边或波动扩张阶段会积累逆势库存；
- 原 `take_profit_usdt` 只检查 `session.realized_pnl`；
- 已实现网格利润可能为正，但库存浮亏、资金费和退出成本已经使真实可锁定利润很低；
- 因此“已实现利润达到阈值后立即止盈”会使用错误口径；
- 另一方面，完全不保护利润，会让已经盈利的 Session 最终因库存止损转亏。

本次改动把止盈改为：

```text
净利润达到启动线
→ 记录净利润峰值
→ 峰值利润回撤 25%：停止新增库存
→ 峰值利润回撤 35%：进入 REDUCE
→ 峰值利润回撤 50%：锁盈平仓
→ 可锁定利润跌到最低保留线：锁盈平仓
```

净利润口径：

```text
current_net_pnl
=
session.realized_pnl
+ inventory.unrealized_pnl
- estimated_exit_cost
```

其中：

```text
estimated_exit_cost
=
gross_inventory_notional × estimated_exit_cost_rate
```

## 2. 代码位置

新增：

```text
strategy/profit_protection.py
```

修改：

```text
strategy/risk.py
tests/test_risk.py
```

### 2.1 `ProfitProtectionTracker`

按 `session_id` 保存：

- 当前净利润；
- 历史净利润峰值；
- 从峰值回撤的比例；
- 预计退出成本；
- 最低应锁定利润；
- 当前动作阶段。

### 2.2 安全原则

没有 `InventorySnapshot` 时，不允许仅凭已实现利润触发止盈，也不会更新净利润峰值。

原因：价格流事件可能比完整库存对账更频繁；若在没有库存快照时把已实现利润当作净利润峰值，会再次产生“已赚 10、库存浮亏 8，却按 10 计算止盈”的错误。

### 2.3 动作映射

```text
ProfitProtectionAction.SUPPRESS
→ RiskAction.DEFEND

ProfitProtectionAction.REDUCE
→ RiskAction.REDUCE

ProfitProtectionAction.CLOSE
→ RiskAction.CLOSE
```

当前 Controller 对 `REDUCE` 的执行语义主要是撤销增加库存的订单，并不保证立即主动成交减仓。因此本轮回测必须分别统计：

- REDUCE 触发次数；
- REDUCE 后库存是否实际下降；
- 从 REDUCE 到 CLOSE 的延迟；
- REDUCE 期间新增的浮亏。

若 REDUCE 频繁触发但库存没有实际下降，不要继续只调阈值，应进入本文第 9 节的“主动分批减仓”结构改造。

## 3. 当前默认参数

`trading.take_profit_usdt` 的语义已调整为：

> 净利润峰值保护的启动线，不再是已实现利润达到后立即全平的固定止盈线。

当前保守默认值：

```yaml
trading:
  take_profit_usdt: 10
```

`strategy.risk.RiskConfig` 中的利润保护默认值：

```python
profit_protection_enabled = True
profit_minimum_locked_ratio = 0.25
profit_suppress_drawdown_pct = 0.25
profit_reduce_drawdown_pct = 0.35
profit_close_drawdown_pct = 0.50
profit_estimated_exit_cost_rate = 0.0007
```

这些参数是研究起点，不代表已通过样本外验证。

## 4. Codex 必须完成的第一项工作

当前实时 RiskManager 已接入利润保护，但历史回测循环尚未证明与该状态机完全一致。

Codex 必须先完成语义对齐：

1. 在 `BacktestConfig` 中加入对应字段；
2. 在 `run_grid_backtest()` 中复用 `ProfitProtectionTracker`，不得复制另一套公式；
3. 每根已闭合 Bar 结束时计算净利润快照；
4. 只有使用当时已知的成交、库存、Funding 和价格；
5. 禁止使用下一根 Bar 或窗口最终结果；
6. `SUPPRESS` 后撤销会增加净库存的订单；
7. `REDUCE` 的回测语义必须与实时 Controller 当前语义一致；
8. `CLOSE` 使用 Taker 手续费与止盈退出滑点；
9. 在 `BacktestResult` 中增加利润保护统计字段；
10. 增加固定种子可复现测试。

建议新增结果字段：

```text
profit_protection_activation_count
profit_suppress_count
profit_reduce_count
profit_close_count
profitable_to_losing_count
peak_profit_giveback_usdt
peak_profit_giveback_pct
locked_profit_usdt
profit_exit_cost
bars_from_activation_to_close
```

## 5. 回测对照组

使用完全相同的数据、成本、随机种子和窗口划分运行：

### P0：当前策略但关闭利润保护

```text
profit_protection_enabled = false
```

### P1：只启用固定净利润止盈

仅作为诊断对照：

```text
current_net_pnl >= activation
→ CLOSE
```

不得作为默认生产候选。

### P2：峰值回撤保护

```text
SUPPRESS + REDUCE + CLOSE
```

使用本次默认阈值。

### P3：峰值回撤保护 + 主动分批减仓

仅在确认 P2 的 REDUCE 没有实际降低库存时研究。

## 6. 数据和样本规则

标的：

```text
BTCUSDT
ETHUSDT
```

周期：

```text
1m
```

数据长度：尽可能使用最新完整的约 730 天。

必须按时间顺序划分：

```text
前 18 个月：Walk-Forward Train / Validation
最后 6 个月：锁定 Final OOS
```

已经被查看过的旧 Final OOS 不得再次作为“全新样本外证据”。若没有新的未查看区间：

- 把结果标记为 Research Validation；
- 不得声称达到实盘上线标准；
- 等待未来新数据再做一次真正锁定 OOS。

固定随机种子：

```text
3, 10, 17, 31, 59, 97
```

## 7. 必须统计的指标

### 7.1 核心收益风险

- 净收益；
- Profit Factor；
- 最大回撤；
- 最差 5% 窗口平均损失；
- 95% CVaR；
- 手续费；
- Funding；
- 退出滑点；
- 止损次数；
- 锁盈平仓次数。

### 7.2 利润保护专用指标

- 曾经盈利但最终转亏的窗口比例；
- 激活后最终成功锁定正利润的比例；
- 峰值利润到最终利润的回吐金额；
- 峰值利润回吐比例的中位数和 90 分位；
- 过早止盈后错失的后续利润；
- SUPPRESS 后库存增长是否停止；
- REDUCE 后 30/60/120 分钟库存下降比例；
- CLOSE 时实际净利润与触发时估计净利润的误差。

### 7.3 按市场状态拆分

只使用因果状态进行策略决策；事后状态仅用于报告。

至少拆分：

```text
RANGE
UP_TREND
DOWN_TREND
VOLATILITY_EXPANSION
TRANSITION
```

## 8. 候选必须达到的目标

利润保护候选只有同时满足以下条件才可以进入测试网候选：

1. 盈利窗口最终转亏的比例相对 P0 下降至少 30%；
2. 最差 5% 窗口平均损失改善至少 20%；
3. 最大回撤不得比 P0 恶化超过 5%；
4. 震荡行情净利润保留率至少 75%；
5. 峰值利润回吐比例中位数不高于 45%；
6. 至少 4/6 个随机种子的组合净收益为正；
7. BTC 与 ETH 不能一个明显改善、另一个灾难性恶化；
8. 手续费占毛利润比例不能明显恶化；
9. 不能依赖一两个极端盈利窗口；
10. 完整 `pytest -q` 必须通过。

若没有候选满足全部条件，必须输出：

```text
本轮没有稳健候选，保持生产参数不变。
```

不得选择“总收益最高但尾部风险更差”的参数。

## 9. 未达目标时的参数调整顺序

一次只修改一类参数，不得同时搜索全部网格参数。

### 9.1 几乎没有触发利润保护

现象：

- `profit_protection_activation_count` 接近 0；
- 盈利转亏比例没有变化。

按顺序调整：

```text
activation_profit_usdt:
10 → 6 → 4 → 3 → 2
```

限制：启动线不得低于单次完整退出预计成本的 4 倍，否则止盈可能只是在给交易所刷手续费。

### 9.2 触发太早，震荡利润被切断

现象：

- RANGE 净利润保留率低于 75%；
- 错失后续利润明显增加；
- Session 持续时间大幅缩短。

按顺序调整：

```text
activation_profit_usdt：提高
profit_suppress_drawdown_pct：0.25 → 0.30
profit_reduce_drawdown_pct：0.35 → 0.40
profit_close_drawdown_pct：0.50 → 0.55 或 0.60
```

一次只改一个维度。

### 9.3 触发太晚，盈利仍大量回吐

现象：

- 峰值利润回吐中位数高于 45%；
- 盈利转亏比例改善不足；
- CLOSE 时已经接近零利润。

按顺序调整：

```text
profit_close_drawdown_pct：0.50 → 0.45 → 0.40
profit_reduce_drawdown_pct：0.35 → 0.30
profit_suppress_drawdown_pct：0.25 → 0.20
profit_minimum_locked_ratio：0.25 → 0.30 或 0.40
```

### 9.4 REDUCE 很多，但库存没有下降

现象：

- `profit_reduce_count` 很高；
- REDUCE 后库存下降比例接近 0；
- 最终仍大量进入 CLOSE 或止损。

不要继续只降低 REDUCE 阈值。应实现：

```text
PASSIVE_REDUCE
→ 将减仓订单移近当前价
→ 每次减 20%–35%

ACTIVE_REDUCE
→ Maker 超时后使用限价 IOC 或受控 Taker
→ 每次减 20%–35%

FORCE_CLOSE
→ 只处理剩余库存和极端风险
```

首轮允许搜索：

```text
passive_reduce_after_minutes: 30, 60, 120
active_reduce_after_minutes: 120, 240, 360
passive_reduce_fraction: 0.20, 0.35
active_reduce_fraction: 0.20, 0.35
```

### 9.5 手续费显著增加

现象：

- 手续费占毛利润比例明显提高；
- PnL 改善主要被退出成本吃掉。

调整：

- 提高启动利润；
- 提高最小可锁定利润；
- 延长 REDUCE 到 CLOSE 的确认时间；
- 优先 Maker 分批减仓；
- 不得通过降低真实手续费假设解决。

### 9.6 BTC 改善但 ETH 恶化，或相反

先验证是否是：

- 波动尺度不同；
- 最小下单量或名义价值不同；
- 平均退出成本不同；
- Session 峰值利润分布不同。

只有在多个 Walk-Forward Fold 中都稳定存在差异，才允许增加按标的覆盖：

```yaml
profit_protection_overrides:
  BTCUSDT: {}
  ETHUSDT: {}
```

每个标的最多先开放：

- activation；
- minimum locked ratio；
- close drawdown。

不要一开始为每个标的开放全部参数。

## 10. 第一轮参数搜索空间

最多 24 个候选，先粗后细。

推荐粗搜索：

```text
activation_profit_usdt: 4, 6, 10
minimum_locked_profit_ratio: 0.20, 0.30
suppress_drawdown_pct: 0.20, 0.25
reduce_drawdown_pct: 0.30, 0.35
close_drawdown_pct: 0.40, 0.50
```

必须满足：

```text
suppress < reduce < close
```

不要同时调整：

- 网格层数；
- 网格宽度；
- 杠杆；
- 资金上限；
- 趋势阈值；
- LONG/SHORT 方向。

## 11. Codex 输出文件

Codex 必须生成：

```text
reports/profit-protection/data-audit.md
reports/profit-protection/baseline.md
reports/profit-protection/parameter-search.csv
reports/profit-protection/walk-forward.csv
reports/profit-protection/state-breakdown.csv
reports/profit-protection/final-report.md
reports/profit-protection/results.json
```

最终报告必须写明：

- 数据实际起止时间；
- 是否有全新未查看 Final OOS；
- 成本和成交模型；
- P0/P1/P2/P3 对照；
- 每个随机种子；
- BTC、ETH 和组合结果；
- 是否达到第 8 节全部门槛；
- 未达到时按第 9 节选择的下一项修改；
- 是否建议进入测试网。

## 12. Codex 执行提示词

可以直接把下面内容交给 Codex：

```text
阅读 docs/codex-profit-protection-backtest-v2.3.md，并严格按文档执行。

先运行完整测试和数据审计，再把 strategy/profit_protection.py 的同一状态机接入 strategy/backtest.py。不得复制另一套净利润或回撤公式。使用 BTCUSDT、ETHUSDT 近两年 1m 数据，固定种子 3、10、17、31、59、97，先完成 P0/P1/P2 对照。

只有当 P2 的 REDUCE 频繁触发但库存没有实际下降时，才进入 P3 主动分批减仓结构。最多进行三轮受约束优化；每轮只验证一个假设。最终按文档第 8 节逐条判断是否通过。没有候选全部通过时，保持生产默认参数不变，并明确输出“本轮没有稳健候选”。

禁止动态 LONG/SHORT、马丁格尔、提高杠杆、降低真实费用、使用未来数据或反复查看同一 Final OOS 调参。
```

## 13. 已知限制

1. 利润峰值当前保存在 Trader 进程内存中；进程重启后会重新建立峰值，尚未持久化到数据库。
2. 实时 REDUCE 当前主要撤销增加库存的订单，不等同于立即主动减仓。
3. `session.realized_pnl` 是否完整包含所有 Funding 与退出费用，需要在回测和实盘对账中继续确认。
4. 当前默认阈值尚未通过新的未查看 Final OOS。
5. 在完成本文回测门槛前，不应将该改动视为已验证的实盘盈利改进。
