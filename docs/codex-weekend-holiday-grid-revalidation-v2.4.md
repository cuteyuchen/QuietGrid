# QuietGrid v2.4：周末/节假日低波动中性网格原始假设再验证协议

> 目标分支：`codex/profit-protection-backtest-v2.3`  
> 研究主线：NYSE 周末/节假日休市窗口中的低波动、中性、均值回归网格。  
> 本文用于重新约束 Codex 的回测范围，避免继续把跨资产价差、Funding、Basis、期限结构或日线趋势策略混入 QuietGrid 主策略证据链。

---

## 1. 本轮要回答的唯一核心问题

QuietGrid 最初的经济假设是：

```text
美国股票基础市场进入周末或交易所节假日休市
→ 相关可连续交易的美股代币/衍生品价格发现减弱
→ 波动和方向性下降、均值回归与往返振荡增加
→ 在真实成本后，中性网格可能比普通时段更有优势
```

本轮必须直接验证这个因果链，而不是继续寻找“BTC/ETH 上任何可能盈利的其他策略”。

必须依次回答：

1. 周末/节假日窗口是否真的比普通工作日隔夜和匹配随机窗口更低波动？
2. 周末/节假日是否真的具有更多可覆盖费用的往返振荡，而不只是波动更低？
3. 在完全相同的中性网格参数、成本模型和执行假设下，周末/节假日窗口是否优于两个对照组？
4. 低波动准入过滤是否提供独立增益？
5. 库存、利润保护和盘前退出逻辑是否改善尾部风险，而不是掩盖一个本来不存在的窗口优势？
6. 真实美股相关标的与 BTC/ETH 压力测试是否得到一致结论？

只有上述问题被严格回答后，才允许讨论生产候选。

---

## 2. 研究边界：哪些属于主线，哪些不属于

### 2.1 QuietGrid 主线允许研究

仅允许：

- `NEUTRAL` 中性网格；
- NYSE `WEEKEND` 和 `HOLIDAY` 窗口；
- 与其严格匹配的 `WEEKDAY_OVERNIGHT` 和随机时间窗口对照；
- 入场前低波动、低方向性、均值回归和交易成本过滤；
- Maker 网格成交；
- 库存上限、逆势库存抑制；
- Wind-down、盘前强制离场；
- 净利润保护；
- 因果波动扩张防御；
- 真实手续费、Funding、滑点和成交概率。

### 2.2 不得作为 QuietGrid 主线证据

以下研究可以保留在仓库中，但必须标记为 `alternative_strategy`，不得用于证明周末低波动网格有效：

- Round 19 及之后的跨资产 β 中性价差；
- Cross-asset Z-score；
- Relative momentum；
- Funding carry；
- Premium/Basis convergence；
- 跨交易所 Funding spread；
- 永续/季度期限价差；
- Round 27 日线 SMA50/200 绝对趋势；
- 任何全天候、跨周持仓或动态 LONG/SHORT 策略；
- 使用未来路径选择方向、阈值或进出点的 Oracle 上界。

本轮不要删除这些历史报告，但不要继续执行 Round 27，也不要把它们的结果写进 QuietGrid 主策略最终结论。

---

## 3. 工作安全规则

1. 继续使用当前研究分支，不修改 `master`。
2. 不连接真实交易账户，不使用真实 API 密钥，不发送订单。
3. 不修改生产默认配置。
4. `direction_mode` 始终保持 `NEUTRAL`。
5. 禁止提高杠杆、资本、库存上限来制造收益。
6. 禁止降低真实手续费、Funding 或滑点假设。
7. 禁止未来数据、未闭合 K 线、事后状态进入交易决策。
8. 禁止根据 Validation 或 Final OOS 结果反向修改参数。
9. 禁止删除失败窗口、失败标的、失败年份或失败随机种子。
10. 所有随机窗口和随机成交必须使用冻结种子并写入 manifest。
11. 所有新增策略选项默认关闭，且可以单独回滚。
12. 旧的已查看 Final OOS 不得重新包装成新的样本外证据。

---

## 4. 资产范围：必须区分主验证标的与压力测试标的

### 4.1 Tier A：主验证标的

主验证标的必须同时满足：

1. 经济上与美国股票基础资产直接相关；
2. 基础市场遵循 NYSE/Nasdaq 交易日历；
3. 衍生品或代币在基础市场休市时仍可连续交易；
4. 有足够长、可审计的分钟历史数据；
5. 有真实交易规则、费用、最小数量和价格步长；
6. 周末/节假日确实存在可成交数据。

候选示例只用于数据审计，不代表自动合格：

```text
AAPLUSDT
MSFTUSDT
TSLA/TSLAPREUSDT
NVDAUSDT
METAUSDT
AMZNUSDT
GOOGLUSDT
```

Codex 必须先审计这些符号在目标交易所和历史数据源中是否真实存在、对应什么基础资产、何时上线、是否存在连续周末数据。不得因为配置文件中出现名称，就假定有可用历史数据。

### 4.2 Tier B：压力测试和负对照

```text
BTCUSDT
ETHUSDT
```

BTC/ETH 可以用于：

- 测试回测引擎；
- 测试尾部风险；
- 检查“NYSE 周末窗口”是否只是任意时间标签；
- 作为与美股休市无直接因果关系的负对照。

BTC/ETH 不得单独证明最初的美股休市网格假设成立。

### 4.3 资产审计结论规则

若没有任何 Tier A 标的满足数据要求：

- 仍可完成 BTC/ETH 的对照实验；
- 最终结论必须写明 `NO_TIER_A_DATA`；
- 不得声称原始经济假设被验证；
- 不得把 BTC/ETH 的正结果设置为生产默认依据。

---

## 5. 数据要求

### 5.1 周期与来源

优先使用：

1. 交易所官方归档；
2. 已冻结且带 SHA-256 manifest 的本地数据；
3. 官方 REST 只补最新尾部；
4. 其他来源必须单独标注并进行交叉校验。

基础回测周期：

```text
1m
```

入场观察期：

```text
180 根已闭合 1m K 线
```

### 5.2 数据质量审计

每个标的必须报告：

- 实际起止时间；
- 上线时间；
- 分钟覆盖率；
- 重复 K 线；
- 冲突重复；
- 分钟缺口；
- OHLC 合法性；
- 时区；
- 周末和节假日数据是否存在；
- Funding 覆盖；
- 交易规则历史是否可得；
- 费用和最小名义价值；
- 数据是否跨越合约规则变化。

生成：

```text
reports/weekend-grid-revalidation-v2.4/asset-data-audit.md
reports/weekend-grid-revalidation-v2.4/asset-data-manifest.json
```

---

## 6. 窗口构造：必须有两个对照组

所有窗口使用同一 NYSE 日历和 `America/New_York` 时区规则。

### 6.1 主实验组 W：周末/节假日

包括：

- `WEEKEND`；
- `HOLIDAY`；
- 从前一交易日正式收盘后开始；
- 到下一交易日盘前开始前 `force_close_minutes` 结束；
- 只有满足 `minimum_trade_minutes` 的窗口才纳入。

必须分别报告：

- 普通周末；
- 长周末；
- 单日节假日；
- 多日节假日。

### 6.2 对照组 O：普通工作日隔夜

使用相同规则构造 `WEEKDAY_OVERNIGHT`：

- 前一交易日收盘后开始；
- 下一交易日盘前强制离场前结束；
- 当前生产策略仍禁止此窗口交易；
- 本组仅用于研究对照。

### 6.3 对照组 R：匹配随机窗口

从完整数据中预先抽取随机窗口，并在计算收益前冻结。

每个随机窗口必须匹配主实验窗口的：

- 标的；
- 日历月份或季度；
- 窗口持续时间；
- UTC 小时分布；
- 数据完整性；
- 交易规则状态。

随机窗口不得与主实验组或普通隔夜组重叠。

至少使用固定抽样种子：

```text
3, 10, 17, 31, 59, 97
```

生成冻结清单：

```text
reports/weekend-grid-revalidation-v2.4/window-manifest.csv
reports/weekend-grid-revalidation-v2.4/window-manifest.json
```

### 6.4 公平性约束

三个窗口组必须使用：

- 相同观察长度；
- 相同交易时长匹配；
- 相同网格参数生成器；
- 相同费用；
- 相同成交模型；
- 相同随机成交种子；
- 相同强制离场距离；
- 相同资金和风险限制。

不得为周末组和对照组选择不同参数。

---

## 7. 第一阶段：先验证市场假设，不运行参数优化

在任何策略参数搜索之前，计算每个窗口入场后和交易期间的市场特征。

### 7.1 必须统计的市场特征

每小时标准化：

- realized volatility；
- ATR%；
- high-low range%；
- directional efficiency；
- 最大单向移动；
- 收益符号翻转率；
- 达到真实网格步长的 reversal legs；
- completed grid-sized cycles；
- `cycle_capacity = cycles × max(step_pct - 2 × maker_fee, 0)`；
- 跳空；
- 波动扩张发生率；
- spread 和深度（数据可用时）；
- Funding 绝对值。

### 7.2 统计比较

分别比较：

```text
WEEKEND/HOLIDAY vs WEEKDAY_OVERNIGHT
WEEKEND/HOLIDAY vs MATCHED_RANDOM
```

按以下维度拆分：

- Tier A / Tier B；
- 标的；
- 年份；
- 普通周末 / 长周末 / 节假日；
- 月份或季度。

使用：

- 中位数和分位数；
- paired difference；
- block bootstrap 置信区间；
- 每月方向一致率；
- 不依赖单个窗口的集中度。

### 7.3 原始假设门槛 H1

至少存在一个合格 Tier A 标的，并且在 Development 中同时满足：

1. 周末/节假日 realized volatility 中位数不高于两个对照组的 90%；
2. directional efficiency 不高于两个对照组；
3. grid-sized cycle capacity 不低于两个对照组；
4. 费用后的 cycle capacity 中位数严格为正；
5. 至少 60% 的自然月显示同方向优势；
6. 最佳单月对全部优势贡献不超过 35%；
7. bootstrap 置信区间不能显示优势完全由随机噪声解释。

若 H1 不通过：

```text
ORIGINAL_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED
```

停止主策略参数优化，只输出失败诊断。

若只有 BTC/ETH 通过而 Tier A 不通过：

```text
CRYPTO_TIME_WINDOW_EFFECT_ONLY_NOT_TRADFI_HYPOTHESIS
```

不得进入生产候选阶段。

---

## 8. 第二阶段：固定参数基线对照

只有 H1 通过才执行。

使用当前生产逻辑的同一套参数快照，不搜索参数。

### B0：主实验无过滤基础网格

```text
WEEKEND/HOLIDAY
NEUTRAL GRID
关闭新增利润保护和波动减仓
保留真实库存上限、止损和强制离场
```

### B1：普通工作日隔夜基础网格

与 B0 完全相同，只改变窗口种类。

### B2：匹配随机窗口基础网格

与 B0 完全相同，只改变窗口清单。

### B3：周末/节假日 + 低波动准入

```text
B0
+
当前因果 Regime 入场过滤
```

### B4：周末/节假日 + 完整当前防御

```text
B3
+
库存防御
+
利润保护
+
Wind-down
+
因果波动扩张防御（只使用已经注册的固定版本）
```

必须额外保留一个“当前真实生产配置快照”组，确保研究设置没有静默偏离生产语义。

---

## 9. 回测执行真实性

必须包含：

- Maker 手续费；
- Taker 手续费；
- Funding；
- 止损和止盈退出滑点；
- 最小数量；
- 数量步长；
- 价格步长；
- 最小名义价值；
- POST_ONLY 语义；
- 未成交；
- 部分成交或保守概率成交；
- 同一根 K 线多订单的最坏顺序；
- 库存 lot；
- 网格配对；
- Wind-down；
- 盘前强制离场；
- 利润保护；
- 波动防御。

必须记录回测与实时 `GridEngine`、`Controller` 和 `Scheduler` 的语义差异。

若存在会改变结论的差异，先修复语义对齐，再运行正式回测。

---

## 10. 必须统计的策略指标

### 10.1 收益和风险

- 总净收益；
- 毛网格收益；
- 手续费；
- Funding；
- 滑点；
- Profit Factor；
- 最大回撤；
- 最大回撤持续时间；
- Sharpe；
- Sortino；
- Calmar；
- 最差 5% 窗口均值；
- 95% CVaR；
- 最差单个窗口；
- 正收益窗口比例；
- 最佳窗口集中度。

### 10.2 网格质量

- 成交次数；
- 完整配对次数；
- 每小时配对次数；
- 每次配对毛利润；
- 费用/毛利润；
- 网格步长覆盖率；
- 观察期与交易期波动变化；
- 成交机会未成交比例。

### 10.3 库存与退出

- 峰值净库存；
- 峰值总库存；
- 峰值库存利用率；
- 逆势库存持续时间；
- 盈利窗口最终转亏比例；
- Wind-down 成交率；
- 强制离场损失；
- 止损损失；
- 利润保护退出次数；
- 波动扩张减仓次数。

### 10.4 窗口效应专用指标

- B0 相对 B1 的净收益差；
- B0 相对 B2 的净收益差；
- 同月份 paired PnL difference；
- 周末优势月份占比；
- 周末优势的 bootstrap 置信区间；
- 周末优势是否只来自更长持有时间；
- 普通周末、长周末和节假日分别贡献；
- Tier A 与 Tier B 的差异。

---

## 11. 样本划分与 OOS 规则

对每个标的的完整合格窗口按时间排序：

```text
Development：前 50%
Validation：随后 25%
Final OOS：最后 25%
```

规则：

1. 随机窗口 manifest 在看任何收益前冻结；
2. 第一阶段市场假设只使用 Development；
3. 固定参数基线通过 Development 后才打开 Validation；
4. 参数优化若被授权，只能使用 Development；
5. Validation 只用于选择是否接受固定候选；
6. Final OOS 只对唯一候选评估一次；
7. 已经被历史研究查看过的区间只能标为 Research Validation；
8. 没有新的未查看数据时，不得声称达到实盘稳定收益标准。

---

## 12. 固定参数基线的通过门槛 H2

在 Development 和 Validation 中，B0 必须同时满足：

1. Tier A 至少两个标的或一个标的跨两个明显不同年代有足够样本；
2. B0 六种子平均净收益严格为正；
3. B0 六种子最差净收益严格为正；
4. 每个主验证标的净收益为正；
5. Profit Factor > 1；
6. 最大回撤 <= 5%；
7. 最佳窗口集中度 <= 35%；
8. 最差 5% 窗口均值优于 B1 和 B2；
9. B0 平均净收益高于 B1 和 B2；
10. paired PnL difference 的 bootstrap 结果支持正的周末优势；
11. 费用/毛利润比例不高于 35%；
12. 不能依赖单一节假日、单一年份或单一标的；
13. COST50 情景仍保持正收益和 PF > 1；
14. 完整测试通过。

若 H1 通过但 H2 不通过，结论应为：

```text
LOW_VOLATILITY_EFFECT_OBSERVED_BUT_NOT_MONETIZABLE_BY_CURRENT_GRID
```

不要通过调整防御参数掩盖网格本身没有费后优势的问题。

---

## 13. 第三阶段：有限参数优化

只有 H1 和 H2 都通过，才允许进行一轮有限优化。

### 13.1 第一轮只允许优化网格经济性

最多 24 个组合：

```text
min_step_pct: 0.0015, 0.0018, 0.0022
range_multiplier: 1.00, 1.25, 1.50
max_grid_num: 10, 15
```

可根据交易规则剔除不可下单组合，但不得替换成新的数值。

约束：

- 周末组和两个对照组必须使用同一候选；
- 不允许按窗口类型单独调参；
- 不允许同时调整利润保护、库存、趋势和波动防御；
- 选择目标必须最大化“周末相对对照优势”，而非只最大化绝对 PnL。

### 13.2 第二轮只允许优化准入过滤

仅当第一轮证明网格经济性存在，而错误入场仍造成主要尾部损失时，最多研究：

```text
entry_max_directional_efficiency: 0.45, 0.55
max_vol_expansion_ratio: 1.25, 1.50
minimum_reversal_ratio: 当前值与一个更严格值
```

最多 8 个组合。

### 13.3 不得优化

- LONG/SHORT；
- SMA 趋势；
- 资产方向特例；
- 杠杆；
- 资本；
- 库存上限；
- 费用；
- 随机成交概率；
- 已打开的 Validation 或 Final OOS。

---

## 14. 未达目标时的诊断和修改顺序

### 14.1 周末并不更低波动

现象：

- H1 的 realized volatility 门槛失败；
- 周末方向效率不低于对照；
- Tier A 与 Tier B 均无稳定差异。

动作：

- 停止参数优化；
- 检查资产是否真的锚定美国股票基础市场；
- 检查窗口起止是否使用了错误时区；
- 检查是否混入工作日隔夜；
- 若数据和窗口无误，正式记录原始假设不成立。

不得通过加止损或缩短窗口让结果看起来更好。

### 14.2 周末波动更低，但可交易振荡不足

现象：

- realized volatility 下降；
- completed cycles 和 fee-adjusted cycle capacity 也下降；
- 毛网格收益不足。

动作：

- 只研究更宽步长和更少网格层；
- 不先研究库存防御；
- 若全部固定范围仍无法覆盖费用，记录当前网格结构不可货币化。

### 14.3 毛收益为正，但手续费吞噬收益

现象：

- gross_grid_pnl > 0；
- net_pnl <= 0；
- fee/gross 比例过高。

动作：

- 提高最小步长；
- 降低无效成交数量；
- 检查 Maker 费用和 POST_ONLY 成交假设；
- 不得降低真实费用假设；
- 若交易所产品费用结构不支持，排除该标的。

### 14.4 周末有优势，但盘前强制离场吞掉利润

现象：

- 窗口中途净利润为正；
- Wind-down 或强制离场后转亏；
- 主要损失集中在最后 2–12 小时。

动作顺序：

1. 统计不同 `time_to_force_close` 的库存和可锁定利润；
2. 研究更早的因果 Wind-down；
3. 先 Maker 被动减仓；
4. 超时后受控主动减仓；
5. 不允许跨过盘前继续持仓。

允许的首轮固定值：

```text
wind_down_bars: 720, 1440, 2160
```

一次只改该参数。

### 14.5 周末中途波动扩张造成尾部损失

现象：

- 亏损发生在窗口中部；
- 入场时符合低波动；
- 后续出现因果波动扩张。

动作：

- 使用当前 Bar 之前已闭合 K 线；
- 只研究已注册的波动扩张减仓；
- 对比 SUPPRESS、PASSIVE_REDUCE、ACTIVE_REDUCE；
- 不切换 LONG/SHORT。

### 14.6 防御降低尾部，但破坏周末优势

现象：

- B4 尾部改善；
- B4 的周末相对对照优势下降；
- RANGE 收益保留不足。

动作：

- 保留 B0/B3 作为主候选；
- 单独评估哪一层防御造成损失；
- 不把“更少交易”误当成“策略更稳健”。

### 14.7 只有 BTC/ETH 有结果

动作：

- 报告为引擎压力测试；
- 不证明美股代币休市效应；
- 不修改生产配置；
- 继续寻找 Tier A 数据或暂停该经济假设。

### 14.8 样本太少

不得降低门槛制造候选。

输出：

```text
INSUFFICIENT_TIER_A_WINDOW_SAMPLE
```

并报告达到合理统计能力还需要多少窗口或多少个月。

---

## 15. 必须新增或更新的代码

建议新增：

```text
scripts/weekend_grid_revalidation.py
scripts/freeze_weekend_grid_revalidation.py
scripts/weekend_grid_market_hypothesis.py
tests/test_weekend_grid_revalidation.py
```

要求尽量复用：

- `core.scheduler.Scheduler`；
- `strategy.backtest.run_grid_backtest`；
- 当前 `GridParams` 生成器；
- 当前成本和 Funding 模型；
- 当前利润保护与库存模型。

禁止复制第二套 Scheduler 或第二套网格盈亏公式。

---

## 16. 必须新增的测试

至少覆盖：

1. NYSE 普通周末分类正确；
2. NYSE 节假日和长周末分类正确；
3. 普通工作日隔夜分类正确；
4. 盘前强制离场缓冲正确；
5. W/O/R 三组窗口无重叠；
6. 随机窗口持续时间匹配；
7. 随机 manifest 固定种子可复现；
8. 观察期只使用入场前 180 根已闭合 K 线；
9. 同一参数快照用于三个窗口组；
10. 相同随机成交种子用于配对比较；
11. 未闭合 K 线不参与决策；
12. 不读取下一根 K 线；
13. Funding 只在真实结算时间扣除；
14. 强制离场使用 Taker 费和滑点；
15. 最终无遗留订单和仓位；
16. Tier A/Tier B 标签不会混淆；
17. 已消费 Final OOS 不会被标成新 OOS；
18. 固定输入重复运行结果一致。

最终执行：

```bash
pytest -q
```

---

## 17. 输出目录

所有本轮产物放在：

```text
reports/weekend-grid-revalidation-v2.4/
```

必须生成：

```text
asset-data-audit.md
asset-data-manifest.json
window-manifest.csv
window-manifest.json
market-hypothesis-summary.csv
market-hypothesis-report.md
baseline-comparison.csv
baseline-comparison.md
parameter-search.csv
walk-forward.csv
state-breakdown.csv
final-oos-summary.csv
final-report.md
results.json
```

`results.json` 必须包含：

- Git commit；
- 数据 manifest 哈希；
- 窗口 manifest 哈希；
- 配置快照；
- Tier A/Tier B 标的；
- H1/H2 每一项检查；
- B0–B4 结果；
- 每个标的、窗口组、年份、随机种子；
- Final OOS 状态；
- 是否修改生产默认值；
- 最终结论代码。

---

## 18. 最终结论代码

只能使用以下之一：

```text
ORIGINAL_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED
CRYPTO_TIME_WINDOW_EFFECT_ONLY_NOT_TRADFI_HYPOTHESIS
NO_TIER_A_DATA
INSUFFICIENT_TIER_A_WINDOW_SAMPLE
LOW_VOLATILITY_EFFECT_OBSERVED_BUT_NOT_MONETIZABLE_BY_CURRENT_GRID
WEEKEND_GRID_RESEARCH_CANDIDATE_REQUIRES_NEW_FORWARD_OOS
WEEKEND_GRID_FORWARD_OOS_CANDIDATE
```

只有同时满足：

- 有合格 Tier A 数据；
- H1 通过；
- H2 通过；
- 唯一候选通过 Validation；
- 全新未查看 Final OOS 通过；
- COST50 通过；
- 完整测试通过；

才允许使用：

```text
WEEKEND_GRID_FORWARD_OOS_CANDIDATE
```

即便如此，也只允许进入测试网或影子运行，不得声称稳定盈利。

---

## 19. Git 和提交规则

1. 继续提交到当前研究分支；
2. 不合并 `master`；
3. 不修改生产默认配置；
4. 不删除 Round 19–27 历史研究；
5. 在最终报告中明确把 Round 19–27 标记为替代策略研究；
6. 大型原始行情文件不提交 Git，只提交 manifest、哈希和报告；
7. 日志只提交必要摘要，不提交大量空日志或重复 stdout/stderr。

建议提交信息：

```text
research(grid): revalidate weekend holiday low-volatility hypothesis
```

若失败：

```text
research(grid): document rejected weekend holiday grid hypothesis
```

---

## 20. 可直接交给 Codex 的执行提示词

```text
你当前位于 QuietGrid 仓库的 codex/profit-protection-backtest-v2.3 分支。

阅读并严格执行：

docs/codex-weekend-holiday-grid-revalidation-v2.4.md

停止 Round 27 SMA50/200 趋势研究。不要删除 Round 19–27 的历史文件，但把它们视为 alternative_strategy，不得用于 QuietGrid 周末低波动网格的证据。

本任务必须重新验证最初的经济假设：NYSE 周末/节假日窗口是否比普通工作日隔夜和匹配随机窗口更低波动、更具费后网格往返容量，并且在相同 NEUTRAL 网格参数下产生稳定的相对优势。

先做以下工作：

1. 输出 Git 状态、commit、Python 和依赖版本；
2. 运行 pytest -q，记录原始测试状态；
3. 完成 Tier A 美股相关标的和 Tier B BTC/ETH 的数据审计；
4. 使用现有 Scheduler 构造 WEEKEND/HOLIDAY、WEEKDAY_OVERNIGHT 和 MATCHED_RANDOM 三组无重叠窗口；
5. 在查看收益前冻结随机窗口 manifest；
6. 先运行市场假设 H1，不做参数优化；
7. H1 不通过立即停止主策略搜索；
8. H1 通过后运行 B0–B4 固定参数基线；
9. H2 通过后才允许文档规定的有限参数搜索；
10. 所有组使用相同参数、成本、种子和执行模型；
11. BTC/ETH 只能作为压力测试和负对照，不能单独证明美股休市假设；
12. 不修改 direction_mode、杠杆、资金、费用或生产配置；
13. 不使用旧 Final OOS 反复调参；
14. 输出全部指定报告和 results.json；
15. 根据文档第 18 节选择唯一合法结论代码。

优先复用 core.scheduler.Scheduler 和 strategy.backtest.run_grid_backtest，不得复制第二套窗口分类或网格盈亏公式。

现在开始实际执行。只有数据源完全不可用、会破坏用户数据或需要真实凭据时才停下来询问。没有候选通过时，明确接受失败，不得转向新的趋势、Funding、Basis 或跨资产策略继续搜索。
```
