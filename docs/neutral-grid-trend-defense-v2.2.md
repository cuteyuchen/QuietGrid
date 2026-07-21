# QuietGrid v2.2 中性网格趋势防御修改说明

> 基线分支：`v2.1-runtime-autostart`  
> 修改分支：`v2.2-neutral-trend-defense`  
> 目标：保留中性网格在震荡市场中的收益来源，同时减少单边行情中逆势库存累积造成的尾部亏损。

## 1. 修改结论

当前生产方向保持：

```text
NEUTRAL GRID
+
趋势入场硬阻断
+
运行中趋势连续确认后进入 DEFENSIVE
+
逆势库存提前抑制、减仓和关闭
+
强趋势下不自动切换 LONG/SHORT
```

本次没有把动态 LONG/SHORT 接入生产链路。仓库已有动态方向研究结果显示，开发集表现不能稳定延续到验证集，固定 LONG、固定 SHORT 和动态路由均未形成可靠样本外证据。因此本次修改优先解决“单边行情少亏”，而不是要求网格在所有行情中都交易。

## 2. 原问题

原 Regime 逻辑虽然会降低高方向效率行情的趋势分，但趋势不是入场硬阻断。流动性、成本和波动率等其他分项较高时，中性网格仍可能达到综合入场分数。

运行过程中，Inventory Manager 已把“库存方向与趋势相反”计入 `risk_score`，但实际动作主要依赖库存利用率：

```text
40% -> CAUTION
60% -> HIGH
80% -> CRITICAL
```

这会出现：

```text
行情进入单边
-> 中性网格逐层形成逆势库存
-> 库存利用率尚未达到 40%，系统仍继续运行
-> 累积到较大库存后才减仓或止损
-> 一次趋势止损吞掉之前多轮网格收益
```

## 3. Regime 修改

文件：`strategy/regime.py`

### 3.1 版本

```python
REGIME_MODEL_VERSION = "regime-rules-v2.2.0"
```

Feature 计算方法未改变，因此 Feature Version 保持 `regime-features-v2.1.0`。

### 3.2 新增默认参数

```python
trend_filter_enabled = True
entry_max_directional_efficiency = 0.55
running_max_directional_efficiency = 0.70
```

含义：

- 新建中性网格时，最近短窗方向效率超过 `0.55`，直接禁止入场；
- 网格已运行时允许更宽的滞回区间；方向效率超过 `0.70` 时，产生软违约；
- `entry <= running`，避免边界附近频繁启动和停止。

### 3.3 入场趋势硬阻断

当 `running=False`：

```python
if directional_efficiency > entry_max_directional_efficiency:
    hard_blocks.append("方向效率超过中性网格入场上限")
```

结果：

```text
allowed = false
verdict = BLOCKED_TREND
state = TREND_UP / TREND_DOWN
```

即使其他分项把综合分推到入场门槛以上，也不允许中性网格在明确方向行情中启动。

### 3.4 运行中趋势软违约

当 `running=True` 且方向效率超过保持上限：

```text
allowed = false
hard_blocks = empty
verdict = BLOCKED_SCORE
```

这是有意设计。当前 Controller 对 `BLOCKED_SCORE` 使用 `soft_breach_count` 连续确认，并进入 `DEFENSIVE`；而硬阻断会立即关闭会话。趋势变化可能出现短时噪声，因此不在单根 K 线上直接市价止损。

现有 Controller 链路保持不变：

```text
Regime running soft breach
-> 连续达到 soft_breach_limit
-> _enter_defensive
-> 撤销会增加风险的网格订单
-> 保留减仓和保护性退出
```

### 3.5 状态分类

`_regime_state` 不再固定使用 `0.70`，而是根据当前阶段使用：

```text
入场：0.55
运行：0.70
```

这样处于 `0.55 ~ 0.70` 的方向行情在入场阶段会被正确显示为 `TREND_UP` 或 `TREND_DOWN`，不会出现“状态仍显示 QUIET_RANGE，但其实被趋势规则阻断”的矛盾。

## 4. Inventory 修改

文件：`strategy/inventory.py`

### 4.1 新增默认参数

```python
wrong_way_reduce_utilization = 0.20
wrong_way_reduce_risk_score = 35.0
wrong_way_close_risk_score = 65.0
```

### 4.2 逆势库存定义

```text
净多库存 + 下跌趋势 = 逆势库存
净空库存 + 上涨趋势 = 逆势库存
```

### 4.3 动作优先级

新动作顺序：

```text
1. CRITICAL utilization -> CLOSE
2. 逆势且 risk_score >= 65 -> CLOSE
3. 逆势且 utilization >= 20% 或 risk_score >= 35 -> REDUCE
4. HIGH utilization -> REDUCE
5. 逆势但风险较低 -> SUPPRESS_LONG / SUPPRESS_SHORT
6. 普通 CAUTION -> 原有同向订单抑制
7. 其余 -> ALLOW
```

### 4.4 低库存阶段也停止逆势加仓

过去库存利用率低于 40% 时，通常返回 `ALLOW`。现在只要库存方向与趋势相反，即使利用率仍处于 `NORMAL`，也会取消继续增加该方向库存的订单。

例如：

```text
已有少量多仓
市场趋势转为下跌
库存利用率只有 5%
```

新结果：

```text
SUPPRESS_LONG
```

不会继续在更低价逐层买入。

### 4.5 提前进入只减仓模式

逆势库存达到 20% 利用率时，返回：

```text
REDUCE
```

当前 Controller 已处理该动作：取消所有会增加当前净库存的 OPEN 订单，保留减仓订单和交易所保护性止损。

### 4.6 提前关闭

当逆势库存的综合风险分达到 65，即使利用率尚未达到 CRITICAL，也可以提前关闭。

综合分已包含：

- 库存利用率；
- 浮动亏损；
- 库存与趋势错配；
- 距离强制离场时间。

这比单独等待 80% 库存利用率更接近真实尾部风险。

## 5. 与现有 Controller 的兼容性

本次没有修改 `strategy/controller.py`，因为当前代码已经具备所需执行链路：

- 入场阶段检查 `structural_decision.hard_blocks`；
- 运行阶段通过 `_update_regime_retention` 连续确认软违约；
- `BLOCKED_SCORE` 进入 `DEFENSIVE`；
- Inventory 的 `SUPPRESS_*` 和 `REDUCE` 会调用 `suppress_inventory_increasing_orders`；
- Inventory 的 `CLOSE` 会调用 `_close_session`。

因此改动集中在决策模块，减少对约 5,000 行 Controller 的侵入和回归风险。

## 6. 方向模式政策

当前配置继续保持：

```yaml
trading:
  direction_mode: "NEUTRAL"
  direction_overrides: {}
```

LONG 和 SHORT 保留为研究功能，但不应由当前版本自动选择。

原因：方向网格启动时会先以市价建立种子仓位，这不是轻微偏斜，而是立即承担方向风险。已有研究报告中固定 LONG、固定 SHORT 和动态 Momentum/Contrarian 路由均未稳定通过开发与验证门槛。

后续若研究动态方向，应使用独立策略模块，而不是在运行中的中性 Session 上直接翻转方向：

```text
关闭旧中性 Session
-> 清理库存
-> 重新确认趋势
-> 新建独立 Trend Session
```

## 7. 新增测试

文件：`tests/test_neutral_trend_defense.py`

覆盖：

1. 震荡行情在新趋势过滤下仍允许；
2. 趋势行情入场被硬阻断；
3. 运行中趋势超限为软违约，不立即硬关闭；
4. 低于 CAUTION 的逆势库存也会抑制继续加仓；
5. 逆势库存达到 20% 时提前进入 REDUCE；
6. 逆势亏损可在利用率低于 CRITICAL 时提前关闭。

隔离行为测试结果：

```text
6 passed
```

该结果验证修改模块的语法和核心决策行为；合并前仍应在完整仓库环境运行全部测试。

## 8. 必须执行的完整验证

```bash
pytest -q
```

重点关注：

```text
tests/test_regime.py
tests/test_inventory.py
tests/test_neutral_trend_defense.py
tests/test_v2_controller.py
tests/test_grid_engine_v2.py
tests/test_backtest.py
tests/test_robustness_research.py
```

## 9. 回测实验设计

不要直接重新搜索大量参数。先固定现有网格参数，只比较防御机制的增量效果。

### A：原 v2.1 中性网格

```text
旧 Regime
旧 Inventory
```

### B：仅趋势入场硬阻断

```text
新 Regime
旧 Inventory
```

### C：仅逆势库存提前防御

```text
旧 Regime
新 Inventory
```

### D：完整 v2.2

```text
新 Regime
新 Inventory
```

核心指标：

- 总盈亏；
- Profit Factor；
- 最大回撤；
- 最差 5% 周末窗口；
- 趋势窗口亏损；
- 震荡窗口收益保留率；
- 峰值库存；
- 逆势库存平均持续时间；
- 止损次数；
- `DEFENSIVE` 进入次数；
- 提前关闭后价格继续单边运行的比例；
- 错误防御后价格快速回归的比例。

## 10. 参数选择约束

默认参数只是安全起点，不是已证明最优参数：

```text
entry DE = 0.55
running DE = 0.70
wrong-way reduce utilization = 20%
wrong-way reduce risk = 35
wrong-way close risk = 65
```

必须遵守：

```text
development 选参数
validation 选规则
final OOS 只评估一次
```

不得继续使用已经查看过的同一 Final OOS 反复调参。当前 Final OOS 已经被评估，应为本策略假设创建新的冻结研究版本和新的未来样本窗口。

## 11. 已知限制

1. `RegimeConfig` 和 `InventoryConfig` 新字段当前使用代码默认值，尚未接入 `config.yaml` 显式映射；这是为了避免在超大 `trader.py` 中进行高风险整体替换。下一步可单独完成配置解析接线。
2. `REDUCE` 当前是被动去风险：取消增加库存的订单并保留减仓单，不会立即主动市价减仓。
3. 运行中趋势信号仍来自短窗 1 分钟 K 线，可能出现噪声；使用 `soft_breach_limit` 连续确认降低误触发。
4. 本次不解决趋势策略收益，只解决中性网格在趋势行情中的尾部损失。

## 12. 合并门槛

只有满足以下条件才合并到生产分支：

- 全量测试通过；
- 震荡样本收益没有被明显破坏；
- 趋势样本最大亏损和峰值库存明显下降；
- Validation 同时改善，而不是只改善 Development；
- 成本压力场景仍成立；
- 多随机成交种子结果方向一致；
- Testnet 影子运行中没有重复撤单、残留仓位或保护性止损丢失。

## 13. 回滚

本次修改在独立分支：

```text
v2.2-neutral-trend-defense
```

未修改：

```text
v2.1-runtime-autostart
```

若验证不通过，直接停止合并即可；原可运行分支不受影响。
