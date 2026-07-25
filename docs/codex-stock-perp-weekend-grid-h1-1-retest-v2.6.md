# QuietGrid v2.6：股票永续周末网格 H1.1 方法修正与最终止损回测计划

> 目标分支：`codex/profit-protection-backtest-v2.3`  
> 研究阶段：H1.1 方法修正版；通过后才允许执行 H2.1 固定策略基线。  
> 研究对象：Binance USDT 本位美股相关永续合约。  
> 本轮性质：对原始周末/节假日低波动中性网格假设进行最后一次公平验证。  
> 重要限制：不调参、不修改生产配置、不连接真实账户、不授权实盘。

---

## 1. 背景与本轮目的

v2.5 已完成真实股票永续合约发现、分钟数据冻结、Funding/Mark/Premium/AggTrades 审计以及 W/O/R 窗口构造。

当前正式结果为：

```text
STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED
```

但当前 H1 实现存在四项会影响结论的方法学问题：

1. `realized_volatility = sqrt(sum(minute_return^2))` 是整段累计波动，没有按交易小时标准化；周末窗口明显长于普通工作日隔夜，直接比较会机械性抬高 W。
2. 当前流程先冻结 R，再删除与 R 重叠的 O，导致普通工作日隔夜对照样本被随机窗口大量挤占。
3. 当前 R 的 `calendar_key` 以 `symbol + month + seed` 聚合，并非真实随机时间区间，不能正确表达共同市场冲击和独立统计块。
4. 当前 `market-features.csv` 已经计算并写出了原“SEALED_SHORT_OOS”月份的市场特征，因此该月份不能再被诚实地称为未查看样本外。

本轮不是为了把失败结果“调成成功”，而是为了回答：

> 在修正时长口径、控制组抽样、统计块和样本外标记之后，股票永续的 NYSE 周末/节假日窗口是否仍然缺少独特、稳定、可执行的网格优势？

本轮是原策略主线的最终止损测试。

---

## 2. 唯一允许研究的策略假设

QuietGrid 原始经济假设保持不变：

```text
美国股票基础市场进入 NYSE/Nasdaq 周末或节假日休市
→ 股票永续的方向性和价格发现结构发生变化
→ 在足够成交质量下，出现更多能够覆盖双边费用的往返振荡
→ 固定 NEUTRAL 中性网格优于普通工作日隔夜和匹配随机窗口
```

本轮禁止转向：

- SMA、EMA 或其他趋势策略；
- 动态 LONG/SHORT；
- Funding Carry；
- Premium/Basis 收敛；
- 跨资产价差；
- 跨交易所套利；
- 跨周长期持仓；
- 提高杠杆、资本或库存上限；
- 根据当前结果搜索新的阈值；
- 使用未来路径选择方向或窗口。

`direction_mode` 始终为 `NEUTRAL`，杠杆始终为 `1x`。

---

## 3. 工作安全与研究诚信规则

1. 只在 `codex/profit-protection-backtest-v2.3` 分支工作，不修改 `master`。
2. 不连接真实交易账户，不读取私有 API Key，不发送订单。
3. 不修改生产默认配置。
4. 不覆盖或删除 v2.5 结果；新结果写入独立目录。
5. 优先复用已经冻结且审计通过的本地数据；除非文件哈希或审计失败，否则不得重新下载或替换数据。
6. 所有输入文件必须校验现有 SHA-256。
7. 不允许未来数据、未闭合 K 线或事后状态进入决策。
8. 不删除失败标的、月份、窗口或种子。
9. 所有窗口、分组和种子必须在计算 H1.1 指标前冻结。
10. 不得根据 H1.1 结果改变公式、门槛、资产分组或窗口定义。
11. H1.1 未完整通过时，不运行 B0–B5，不运行参数搜索。
12. 现有 2026-06 及其他已输出市场特征的区间统一标记为 `RESEARCH_VALIDATION_EXPOSED`，不得再称为 Sealed OOS。
13. 新的严格 Forward OOS 必须从本协议提交后的第一个完整 UTC 日之后开始积累。
14. 新 Forward OOS 在达到最低样本要求前不得读取收益汇总。
15. 所有新增策略选项默认关闭，并提供回滚路径。

---

## 4. 输入数据与资产范围

### 4.1 冻结输入

默认复用：

```text
reports/stock-perp-weekend-grid-v1/symbol-discovery.json
reports/stock-perp-weekend-grid-v1/asset-data-manifest.json
reports/stock-perp-weekend-grid-v1/asset-data-audit.json

data/backtests/stock-perp-weekend-grid-v1/*
```

必须读取并验证：

- 1m K 线；
- Funding 事件；
- Mark Price；
- Premium Index；
- AggTrades；
- 交易规则；
- 文件 SHA-256；
- 数据起止边界；
- 数据缺口和重复记录。

不得对缺失分钟插值后参与交易或特征统计。

### 4.2 当前 Tier A-Core

当前正式样本：

```text
AMZNUSDT
COINUSDT
CRCLUSDT
HOODUSDT
INTCUSDT
MSTRUSDT
PLTRUSDT
TSLAUSDT
```

### 4.3 预注册资产分层

为避免加密市场在周末持续价格发现掩盖传统股票休市效应，必须固定分组：

#### A. 传统公司组 `TRADITIONAL_EQUITY`

```text
AMZNUSDT
INTCUSDT
PLTRUSDT
TSLAUSDT
```

#### B. 加密敏感股票组 `CRYPTO_SENSITIVE_EQUITY`

```text
COINUSDT
CRCLUSDT
HOODUSDT
MSTRUSDT
```

规则：

- 总体结果、两个子组结果和逐标的结果必须同时报告；
- 不允许根据结果重新移动标的；
- 原始美股休市经济假设的关键判断以 `TRADITIONAL_EQUITY` 为主；
- 加密敏感组只作为机制差异和压力测试，不得单独证明原始假设成立。

Tier A-Short 可做描述性统计，但不能进入 H1.1 通过判定。

---

## 5. 样本状态重新标记

当前历史数据已被用于市场特征输出，因此重新定义：

```text
2026-03 至 2026-05：RESEARCH_DEVELOPMENT
2026-06：RESEARCH_VALIDATION_EXPOSED
其他已输出特征区间：DESCRIPTIVE_EXPOSED
协议提交后新增完整月份：FORWARD_OOS_FUTURE
```

规则：

1. H1.1 主判断只使用 `RESEARCH_DEVELOPMENT`。
2. `RESEARCH_VALIDATION_EXPOSED` 只能做方向一致性诊断，不得称为新 OOS。
3. H2.1 若获准执行，可报告 Development 和 Exposed Validation，但最终结论只能是“需要未来前向验证”。
4. 当前仓库中任何已写出的 June 特征或窗口不得删除以恢复“封存”假象。

---

## 6. 重新冻结公平的 W/O/R 窗口

必须生成全新的 H1.1 窗口 manifest，不修改 v2.5 manifest。

### 6.1 W：NYSE 周末/节假日

继续复用：

- `core.scheduler.Scheduler`；
- `data_sources.window_slicer.NyseWindowSlicer`；
- `force_close_minutes = 120`；
- 入场前观察 `180` 根已闭合 1m K 线。

W 窗口从基础市场收盘开始，到下一交易日盘前强制退出边界结束。

分别标记：

- 普通周末；
- 三日长周末；
- 单日节假日；
- 多日节假日。

### 6.2 O：完整普通工作日隔夜控制组

先于 R 冻结所有合格 O：

```text
普通 NYSE 交易日收盘
→ 观察 180 分钟
→ 到下一交易日盘前 120 分钟结束
```

要求：

- 使用同一 Scheduler；
- 不与 W 重叠；
- 数据连续；
- 上市阶段、月份和标的标签完整；
- R 的构造不得删除、覆盖或改变 O；
- 输出 `o_count_before_random` 与 `o_count_after_random`，两者必须完全相同。

### 6.3 R：全市场共同随机时间块

R 必须在 W 和 O 全部冻结后生成。

对每个 W 日历窗口和每个固定种子：

```text
3, 10, 17, 31, 59, 97
```

选择一个真实 UTC 时间区间，满足：

- 与目标 W 持续时间相同；
- 同一自然月；
- UTC 起始小时差不超过 1 小时；
- 上市阶段相同；
- 不与任何 W 或 O 重叠；
- 同一种子内不同 R 不重叠；
- 数据连续且所有参与标的可用；
- 先选择全市场共同时间区间，再将相同区间应用于各标的。

随机块键必须使用真实时间：

```text
R:<start_utc>:<end_utc>:seed=<seed>
```

禁止使用：

```text
R:<symbol>:<month>:<seed>
```

作为统计块键。

若候选空间不足：

- 标记 `NO_MATCHED_RANDOM_BLOCK`；
- 可减少可用 R 数量；
- 不得删除 O；
- 不得放宽月份、时长或上市阶段匹配；
- 不得为了凑数允许 W/O/R 重叠。

### 6.4 重叠审计

必须检查：

- W 与 O：0 重叠；
- W 与 R：0 重叠；
- O 与 R：0 重叠；
- 同一种子的 R：0 重叠；
- 不同种子的 R 复用需记录，但统计时按种子分别处理；
- 同一真实日历区间的多个股票必须聚合为一个 block，不能当作独立样本。

---

## 7. H1.1 指标的正确标准化

本轮所有与持续时间相关的指标必须按交易小时标准化，同时保留整段值用于诊断。

设：

```text
hours = tradable_rows / 60
r_i = log(close_i / close_{i-1})
step_pct = 0.0015
maker_fee = 0.0002
```

### 7.1 波动率

必须同时输出：

```text
window_realized_volatility = sqrt(sum(r_i^2))

hourly_realized_volatility = sqrt(sum(r_i^2) / hours)
```

H1.1 门槛只使用 `hourly_realized_volatility`。

### 7.2 往返振荡

必须输出：

```text
reversal_legs
completed_grid_cycles
reversal_legs_per_hour = reversal_legs / hours
completed_grid_cycles_per_hour = completed_grid_cycles / hours
```

### 7.3 费后网格容量

必须输出：

```text
gross_cycle_edge = max(step_pct - 2 * maker_fee, 0)

fee_adjusted_cycle_capacity_window
  = completed_grid_cycles * gross_cycle_edge

fee_adjusted_cycle_capacity_per_hour
  = completed_grid_cycles_per_hour * gross_cycle_edge
```

H1.1 门槛只使用每小时版本。

此指标是市场路径容量上界，不是实际 PnL，不得写成“策略收益”。

### 7.4 方向性

保留：

```text
directional_efficiency
max_single_direction_move_pct
return_sign_flip_rate
```

`directional_efficiency` 为路径比例指标，不需要除以小时。

### 7.5 成交与流动性

必须输出：

```text
zero_trade_ratio
trades_per_hour
base_volume_per_hour
quote_volume_per_hour
median_trade_size
aggtrade_event_count_per_hour
```

若 AggTrades 可用，必须用真实成交事件约束流动性统计，不只依赖 K 线 `trade_count`。

### 7.6 其他诊断

继续输出：

- ATR%；
- High-low range%；
- Funding 绝对值/小时；
- Premium 绝对值；
- Mark 与合约成交价偏离；
- 窗口前半/后半波动比；
- 波动扩张发生率；
- 上市阶段；
- 普通周末/长周末/节假日标签。

---

## 8. 公平比较视图

H1.1 必须分开报告：

```text
W vs O
W vs R
```

禁止把 O 和 R 简单合并后只给一个控制组结果。

同时提供三个视图：

### 8.1 完整窗口每小时视图（主门槛）

使用完整 W/O/R，但所有累计指标按小时标准化。

### 8.2 相同持续时间诊断

由于 O 通常约 10 小时，额外生成固定 `10h` 诊断：

- `W_HEAD_10H`：W 观察期结束后的前 10 个交易小时；
- `W_TAIL_10H`：W 强制退出前的最后 10 个交易小时；
- `O_FULL`：O 完整可交易区间，若不足 10 小时则跳过；
- `R_HEAD_10H`：R 对应前 10 小时。

该视图只用于识别周末开头、尾部和普通隔夜差异，不单独决定 H1.1。

### 8.3 传统公司组与加密敏感组

每项指标必须分别输出：

- ALL_CORE；
- TRADITIONAL_EQUITY；
- CRYPTO_SENSITIVE_EQUITY；
- 每个 symbol。

---

## 9. 统计单位和 Bootstrap

### 9.1 独立统计块

- W：真实 NYSE 休市日历区间；
- O：真实普通工作日隔夜区间；
- R：真实随机 UTC 起止区间与种子。

同一时间区间的多个股票先取组内中位数，再作为一个 block。

禁止把：

```text
8 个股票 × 同一个周末
```

当作 8 个完全独立样本。

### 9.2 Bootstrap

使用至少：

```text
5000 reps
```

分别计算：

- W vs O 的中位数差；
- W vs R 的中位数差；
- hourly volatility；
- directional efficiency；
- capacity per hour；
- zero trade ratio；
- trades per hour。

必须输出：

- observed delta；
- 95% percentile CI；
- favorable support probability；
- block 数；
- 有效月份数；
- 有效传统公司标的数。

固定 Bootstrap 种子：

```text
31
```

---

## 10. H1.1 技术前置门槛

在评估经济假设前，以下必须全部通过：

1. 8 个 Tier A-Core 数据审计仍为 PASS；
2. 输入哈希与冻结 manifest 一致；
3. W/O/R 重叠为 0；
4. O 不因 R 被删除；
5. `o_count_before_random == o_count_after_random`；
6. 至少 12 个独立 W Development 日历块；
7. 至少 30 个独立 O Development 日历块；
8. 每个种子至少 10 个有效 R Development 日历块；
9. 至少 3 个完整 Development 月；
10. 传统公司组至少 3 个标的有完整结果；
11. 所有时长相关主指标均有 `_per_hour` 字段；
12. 原 June 样本被标记为 `RESEARCH_VALIDATION_EXPOSED`；
13. 未计算任何未来 Forward OOS 收益；
14. 全部测试通过。

任一技术前置门槛失败，输出：

```text
H1_1_METHOD_OR_SAMPLE_INVALID
```

不得继续 H1.1 经济判定，也不得运行 B0–B5。

---

## 11. H1.1 经济假设通过门槛

主判断只使用 `RESEARCH_DEVELOPMENT`，并按真实日历 block 聚合。

### 11.1 W vs O 必须全部满足

1. W `hourly_realized_volatility` 中位数不高于 O 的 90%；
2. W `directional_efficiency` 不高于 O；
3. W `completed_grid_cycles_per_hour` 不低于 O；
4. W `fee_adjusted_cycle_capacity_per_hour` 不低于 O；
5. W `fee_adjusted_cycle_capacity_per_hour` 中位数严格为正；
6. W `zero_trade_ratio` 不得显著差于 O；
7. W `trades_per_hour` 至少达到 O 的 75%；
8. capacity-per-hour 的 Bootstrap 优势支持概率至少 95%。

成交质量定义：

```text
W_zero_trade_ratio <= O_zero_trade_ratio * 1.25 + 0.01
```

### 11.2 W vs R 必须全部满足

1. W `hourly_realized_volatility` 中位数不高于 R 的 90%；
2. W `directional_efficiency` 不高于 R；
3. W `completed_grid_cycles_per_hour` 不低于 R；
4. W `fee_adjusted_cycle_capacity_per_hour` 不低于 R；
5. W `zero_trade_ratio` 不得显著差于 R；
6. W `trades_per_hour` 至少达到 R 的 75%；
7. capacity-per-hour 的 Bootstrap 优势支持概率至少 95%。

成交质量定义：

```text
W_zero_trade_ratio <= R_zero_trade_ratio * 1.25 + 0.01
```

### 11.3 稳定性门槛

还必须全部满足：

1. 至少 60% 的完整 Development 月份中，W capacity/hour 同时不低于 O 和 R；
2. `TRADITIONAL_EQUITY` 组整体通过 W vs O 和 W vs R 的方向门槛；
3. 至少 2 个传统公司标的分别显示 W capacity/hour 同时不低于 O/R；
4. `CRYPTO_SENSITIVE_EQUITY` 可失败，但不得反向主导总体结果；
5. 排除上市前 30 天后结论方向不反转；
6. 普通周末与长周末分别报告，不能只靠一个节假日；
7. 最佳单一 W 日历 block 对总正优势贡献不超过 35%；
8. 不依赖单个标的；
9. 结果在六个 R 种子中的方向一致率至少 5/6；
10. 没有统计块、时长或流动性异常解释全部优势。

### 11.4 H1.1 通过条件

上述技术、W vs O、W vs R 和稳定性门槛必须全部通过。

不得通过“只优于普通隔夜”进入策略回测，因为这不能证明周末具有独特优势。

---

## 12. H1.1 结论代码与停止规则

只能使用以下结论之一：

### 技术或样本无效

```text
H1_1_METHOD_OR_SAMPLE_INVALID
```

### 修正后仍不低波动

```text
STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_REJECTED_AFTER_METHOD_FIX
```

### 只优于工作日隔夜，但不优于随机窗口

```text
WEEKEND_EFFECT_NOT_UNIQUE_VS_MATCHED_RANDOM
```

### 市场路径有优势，但成交质量不足

```text
WEEKEND_STRUCTURE_NOT_EXECUTABLE_AT_OBSERVED_LIQUIDITY
```

### 只有加密敏感股票有结果

```text
CRYPTO_SENSITIVE_EQUITY_EFFECT_ONLY
```

### H1.1 完整通过

```text
H1_1_STOCK_PERP_WEEKEND_EFFECT_SUPPORTED
```

除最后一个结论外，全部必须立即停止：

- 不运行 B0–B5；
- 不运行参数搜索；
- 不读取未来 Forward OOS；
- 不修改生产配置；
- 将“固定周末中性网格”标记为已否决或不可执行研究方向。

---

## 13. H2.1：固定策略基线（仅 H1.1 通过后）

H1.1 完整通过后，才允许执行固定参数 B0–B5。

### 13.1 固定参数

必须从当前生产配置读取并冻结快照，至少保持：

```text
direction_mode = NEUTRAL
leverage = 1
capital_per_symbol = 500 USDT
observe_hours = 3
force_close_minutes = 120
min_step_pct = 0.0015
std_k = 1.8
min_grid_num = 3
max_grid_num = 20
max_step_pct = 0.01
max_range_pct = 0.03
```

不允许按标的、月份或窗口调参。

### 13.2 基线组

```text
B0：W + 基础 NEUTRAL 网格
B1：O + 与 B0 完全相同的基础网格
B2：R + 与 B0 完全相同的基础网格
B3：W + 当前因果 Regime 入场过滤
B4：W + 当前完整库存/利润/Wind-down/波动防御
B5：当前真实生产配置快照
```

B0、B1、B2 必须使用：

- 相同参数；
- 相同费用；
- 相同成交模型；
- 相同随机种子；
- 相同资金和库存限制；
- 相同强制退出规则。

### 13.3 成本与执行情景

#### BASE

```text
maker_fee = 0.0002
taker_fee = 0.0005
force_close_slippage = 0.0010
maker_fill_probability = 0.65
max_fills_per_bar = 2
```

#### COST50

```text
maker_fee = 0.0003
taker_fee = 0.00075
force_close_slippage = 0.0015
```

#### EXECUTION_STRESS

```text
maker_fee = 0.0003
taker_fee = 0.0010
force_close_slippage = 0.0025
maker_fill_probability = 0.45
max_fills_per_bar = 1
same_bar_fill_order = worst_case
```

固定成交种子：

```text
3, 10, 17, 31, 59, 97
```

必须模拟真实 Funding、价格/数量取整、Min Notional、Post Only、部分成交、强制退出和残余库存。

---

## 14. H2.1 输出指标

### 14.1 收益与成本

- Gross grid PnL；
- Realized PnL；
- Unrealized/forced-close PnL；
- Funding PnL；
- Maker fee；
- Taker fee；
- Slippage；
- Net PnL；
- Net PnL per active hour；
- Net PnL per capital-hour；
- Profit Factor；
- Fee/Gross ratio。

### 14.2 风险

- 最大回撤；
- 最大回撤持续时间；
- 最差 5% 窗口均值；
- 95% CVaR；
- 最差单一窗口；
- 盈利窗口最终转亏比例；
- 峰值净库存；
- 逆势库存持续时间；
- 强制退出损失。

### 14.3 执行

- 挂单数量；
- 成交数量；
- 完整配对次数；
- 每小时配对次数；
- 未成交机会；
- 部分成交次数；
- Post Only 拒绝；
- 强制 Taker 次数；
- Funding 事件覆盖率；
- 最终残余订单和持仓。

### 14.4 公平比较

必须同时报告：

- W、O、R 完整 Session 总收益；
- 每小时收益；
- 每资本小时收益；
- 同月份 paired difference；
- 同日历 block 聚合；
- Traditional/Crypto-sensitive 分组；
- 普通周末/长周末/节假日；
- BASE/COST50/EXECUTION_STRESS。

---

## 15. H2.1 固定基线门槛

H2.1 只能使用固定参数，不得搜索参数。

B0 在 Research Development 中必须同时满足：

1. 六种子平均 Net PnL 严格为正；
2. 六种子最差 Net PnL 严格为正；
3. Profit Factor > 1；
4. 最大回撤 <= 5%；
5. Fee/Gross <= 35%；
6. 最佳单一窗口贡献 <= 35%；
7. 最差 5% 窗口优于 B1 和 B2；
8. Net PnL per capital-hour 高于 B1 和 B2；
9. Traditional 组净收益不为负；
10. 至少两个传统公司标的为正；
11. COST50 下仍为正且 PF > 1；
12. EXECUTION_STRESS 不出现灾难性亏损或库存失控；
13. 收益不依赖上市前 30 天；
14. 收益不依赖单一周末、单一标的或单一月份；
15. 最终无遗留订单和持仓；
16. 全部测试通过。

`RESEARCH_VALIDATION_EXPOSED` 只用于方向一致性诊断，不得作为样本外通过门槛。

---

## 16. H2.1 结论代码

### H1.1 通过但固定网格不能变现

```text
WEEKEND_EFFECT_SUPPORTED_BUT_CURRENT_GRID_NOT_MONETIZABLE
```

### BASE 通过但成本压力失败

```text
STOCK_PERP_GRID_EDGE_TOO_THIN_AFTER_EXECUTION_COST
```

### 固定历史基线通过，但没有新 OOS

```text
HISTORICAL_STOCK_PERP_WEEKEND_GRID_CANDIDATE_REQUIRES_FORWARD_OOS
```

不得在本轮使用：

```text
PRODUCTION_READY
STABLE_PROFIT
REAL_MONEY_AUTHORIZED
```

---

## 17. 未来 Forward OOS 规则

只有 H1.1 和 H2.1 都通过后，才创建 Forward OOS 候选。

### 17.1 起点

```text
FORWARD_OOS_START
= 本协议提交后的第一个完整 UTC 日 00:00
```

### 17.2 冻结内容

在 Forward OOS 开始前必须冻结：

- Git commit；
- 所有策略参数；
- W/O/R 构造规则；
- 资产分组；
- 成本模型；
- 成交模型；
- 组合选择规则；
- 最终验收门槛。

### 17.3 最低样本

至少积累：

```text
3 个完整自然月
且
至少 12 个独立 W 日历窗口
```

期间不得根据运行结果修改参数。

Forward OOS 结果只能打开一次。

---

## 18. 需要新增的代码

建议新增，不覆盖 v2.5 脚本：

```text
scripts/rebuild_stock_perp_windows_h1_1.py
scripts/stock_perp_market_hypothesis_h1_1.py
scripts/stock_perp_weekend_grid_fixed_baseline_h2_1.py

tests/test_stock_perp_windows_h1_1.py
tests/test_stock_perp_market_hypothesis_h1_1.py
tests/test_stock_perp_fixed_baseline_h2_1.py
```

必须复用：

- `core.scheduler.Scheduler`；
- `data_sources.window_slicer.NyseWindowSlicer`；
- `strategy.backtest.run_grid_backtest`；
- 当前 GridParams 生成器；
- 当前 Funding 模型；
- 当前 Inventory Manager；
- 当前 ProfitProtectionTracker；
- 当前交易规则取整逻辑。

禁止复制第二套 Scheduler、第二套 NYSE 日历或第二套网格 PnL 公式。

---

## 19. 必须新增的测试

至少覆盖：

1. hourly RV 对相同每分钟收益、不同窗口长度给出相同值；
2. window RV 随时长增长，但 hourly RV 不产生机械偏差；
3. cycles/hour 和 capacity/hour 正确；
4. W、O 在 R 前冻结；
5. R 不得删除 O；
6. `o_count_before_random == o_count_after_random`；
7. W/O/R 无重叠；
8. 同一种子 R 无重叠；
9. R 使用真实 start/end block key；
10. 同一真实时间的多个 symbol 聚合为一个 block；
11. 不把 symbol 数当作独立日历样本数；
12. Traditional/Crypto-sensitive 分组固定；
13. June 被标记为 `RESEARCH_VALIDATION_EXPOSED`；
14. 不读取未来 Forward OOS；
15. Bootstrap 固定种子可复现；
16. W vs O 与 W vs R 分开计算；
17. 样本不足时快速失败；
18. H1.1 未通过时 H2.1 拒绝运行；
19. H2.1 使用同一参数快照运行 W/O/R；
20. Funding 只在真实结算时间扣除；
21. 强制退出使用 Taker 费用和滑点；
22. 价格与数量遵守 tick/step/minNotional；
23. 最终无遗留订单和仓位；
24. 固定输入重复运行结果一致；
25. 生产默认配置未改变。

最终执行：

```bash
pytest -q
```

必须保存完整测试输出与退出码。

---

## 20. 输出目录

所有新产物写入：

```text
reports/stock-perp-weekend-grid-h1-1-v2.6/
```

必须生成：

```text
input-hash-audit.md
input-hash-manifest.json

window-manifest-h1-1.csv
window-manifest-h1-1.json
window-overlap-audit-h1-1.md
window-count-audit.md

market-features-hourly.csv
market-hypothesis-h1-1.json
market-hypothesis-h1-1.md
w-vs-o-summary.csv
w-vs-r-summary.csv
asset-group-breakdown.csv
symbol-breakdown.csv
month-breakdown.csv
calendar-block-breakdown.csv
bootstrap-results.csv
matched-10h-diagnostics.csv

baseline-comparison-h2-1.csv
baseline-comparison-h2-1.md
execution-stress-h2-1.csv
portfolio-summary-h2-1.csv
research-validation-exposed.csv

results.json
final-report.md
pytest.stdout.log
pytest.stderr.log
```

若 H1.1 未通过，H2.1 文件应明确写入：

```text
NOT_RUN_H1_1_FAILED
```

不得伪造空收益行或候选结果。

---

## 21. `results.json` 必须包含

- schema version；
- 协议路径；
- Git branch 和 commit；
- 所有输入文件 SHA-256；
- 原 v2.5 manifest 哈希；
- 新 H1.1 window manifest 哈希；
- 数据此前已查看标记；
- 样本状态重新分类；
- W/O/R 完整窗口计数；
- O 在 R 前后的数量；
- 真实日历 block 计数；
- Traditional/Crypto-sensitive 分组；
- 每小时标准化公式；
- W vs O 全部门槛；
- W vs R 全部门槛；
- Bootstrap 结果；
- 月份、标的、上市阶段和窗口类型拆分；
- H1.1 最终结论；
- H2.1 是否获准；
- B0–B5 是否运行；
- Validation/OOS 暴露状态；
- 是否修改生产配置；
- 是否修改 master；
- 是否授权真实资金；
- pytest 命令、退出码和摘要。

---

## 22. 严格执行顺序

Codex 必须按以下顺序工作：

```text
1. 读取本协议和 v2.5 最终结果
2. git status / git rev-parse HEAD
3. 运行完整 pytest
4. 校验现有冻结数据和哈希
5. 重新标记样本暴露状态
6. 先冻结所有 W
7. 再冻结所有 O
8. 最后冻结不与 W/O 重叠的全市场 R
9. 运行窗口重叠和计数审计
10. 计算每小时标准化 H1.1 特征
11. 分开运行 W vs O 与 W vs R
12. 运行资产分组、月份和 Bootstrap 检验
13. 写出 H1.1 结论
14. H1.1 失败则立即停止
15. H1.1 通过才运行固定 B0–B5
16. 运行 BASE/COST50/EXECUTION_STRESS
17. 输出 H2.1 历史研究结论
18. 不打开或声称新的 OOS
19. 不修改生产参数和 master
20. 再次运行完整 pytest
21. 提交代码、测试、manifest 和报告
```

---

## 23. 最终决策原则

本轮不是继续寻找“某组能赚钱的参数”，而是对原始策略做最终资格判断。

严格接受以下结果：

1. 修正后 H1.1 失败：结束“固定周末/节假日中性网格”主策略研究。
2. 只优于 O、不优于 R：周末效应不独特，结束主策略研究。
3. 市场结构通过但成交质量失败：休市结构不可执行，结束主策略研究。
4. H1.1 通过、H2.1 失败：市场效应存在，但当前网格不能变现。
5. H1.1 与 H2.1 都通过：只形成历史研究候选，等待至少三个月全新 Forward OOS。

不得通过增加风控层、降低费用、提高杠杆或选择个别标的绕过失败。

> 最终目标不是证明 QuietGrid 一定有效，而是在公平、可复现、没有样本外伪装的条件下，明确它是否值得继续投入研究资源。
