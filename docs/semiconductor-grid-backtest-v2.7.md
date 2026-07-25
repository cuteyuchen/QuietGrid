# QuietGrid v2.7：休市窗口高流动性半导体网格

## 1. 目的

v2.7 不再把“基础市场休市后波动较低”直接等同于“适合网格”。新的核心假设是：

> 在基础证券休市期间，少数高流动性半导体相关永续仍可能保持足够的短周期价格穿越和成交活动；在 Maker 费率为 0 的实际环境中，只有这些活跃窗口才可能被密集网格变现。

本版本将此前的固定周末网格改为 **休市窗口 + 可成交振荡门槛**。大多数低活跃窗口应被跳过，而不是勉强开仓。

这是一套研究与策略注册版本，不是实盘收益承诺。合入 `master` 后，启动自动开仓默认关闭；必须先完成冻结数据回测、执行压力测试和后续前向验证。

## 2. 研究标的与日历

### 2.1 美国个股组

- `SNDKUSDT`
- `MUUSDT`

参考市场：NYSE 日历，参考时区 `America/New_York`。交易窗口从上一正常交易日收盘后开始，在下一交易日 04:00 美东时间前 120 分钟强制结束。

### 2.2 美国杠杆 ETF 组

- `SOXLUSDT`

SOXL 单独作为 `US_LEVERAGED_ETF` 组报告，不与普通个股合并得出同一经济结论。研究资金乘数为 0.5、格距下限更宽、库存上限更严格，并禁止用额外高杠杆发现策略。

### 2.3 韩国个股组

- `SKHYNIXUSDT`

参考市场：`XKRX` 日历，参考时区 `Asia/Seoul`。KRX 没有按本策略定义的独立盘前时段，因此下一次正常开盘是退出参考点，仍提前 120 分钟强制结束。

`SKHYNIXUSDT` 当前只进入 v2.7 回测研究池，不进入生产 NYSE 单调度器的实时白名单。等多市场运行编排、独立窗口状态和组合资金竞争全部验证后，才允许考虑实时启用。

## 3. 费用与执行假设

### 3.1 主场景：`PRIMARY_ZERO_MAKER`

- Maker：0；
- Taker：0.05%；
- Maker 成交概率：0.65；
- 每根 1m Bar 最多成交 2 笔；
- 止损或强制退出滑点：10 bps；
- Funding：使用真实事件；
- 最终退出：强制计入 Taker 费用和滑点。

Maker 为 0 只适用于挂单成交。方向网格初始种子仓位、止损、主动减仓、窗口结束清仓、Funding、滑点和未成交风险均不能忽略。

### 3.2 执行压力：`EXECUTION_STRESS`

- Maker：0；
- Taker：0.075%；
- Maker 成交概率：0.45；
- 每根 Bar 最多成交 1 笔；
- 强制退出滑点：25 bps。

### 3.3 优惠失效：`MAKER_PROMO_OFF`

- Maker：0.02%；
- Taker：0.05%；
- Maker 成交概率：0.65；
- 强制退出滑点：15 bps。

## 4. 策略 Profile

中性与方向网格必须完全分开报告。

- `N20`：`NEUTRAL`，3–20 格，通用最小格距 0.15%。
- `N100`：`NEUTRAL`，20–100 格，通用最小格距 0.06%。
- `L20`：`LONG`，3–20 格，必须通过预注册做多信号。
- `L100`：`LONG`，20–100 格，必须通过预注册做多信号。

当前只有 `SKHYNIXUSDT` 允许参与 L Profile。SNDK、MU、SOXL 的做多结果不得通过事后观察未来路径来补标。

## 5. 标的专属格距与资金

| 标的 | 普通格距下限 | 密集格距下限 | 资金乘数 | 中性 | 做多 |
|---|---:|---:|---:|---:|---:|
| SNDKUSDT | 0.15% | 0.06% | 1.0 | 是 | 否 |
| MUUSDT | 0.15% | 0.08% | 1.0 | 是 | 否 |
| SOXLUSDT | 0.18% | 0.10% | 0.5 | 是 | 否 |
| SKHYNIXUSDT | 0.15% | 0.06% | 1.0 | 是 | 是 |

所有正式经济性结果先用 1 倍杠杆。20 倍只可用于复现 Binance 页面保证金收益展示，不得用于策略发现或通过判定。

## 6. Grid Viability Gate

观察期仍为 180 根已闭合 1m K 线。自适应网格生成后，再用最近 60 根 Bar 计算：

- 内部网格价位平均穿越次数/小时；
- 收益符号反转比例；
- 方向效率；
- 零成交分钟比例；
- 成交笔数/小时；
- 成交额/小时；
- 点差占格距比例；
- 预计净循环容量/小时。

默认门槛：

```yaml
min_crossings_per_hour: 1.0
min_reversal_ratio: 0.25
max_zero_activity_ratio: 0.20
min_trade_count_per_hour: 60
min_quote_volume_per_hour: 10000
max_spread_to_step_ratio: 0.50
min_net_capacity_per_hour: 0.00025
```

预计净循环容量：

```text
crossings_per_hour × max(step_pct - 2 × maker_fee - projected_funding, 0)
```

该指标是准入近似，不等同于最终收益。最终结果仍由保守成交回测决定。

## 7. 做多信号

做多 Profile 只能使用观察期末已经可见的数据：短窗 15 分钟、长窗 60 分钟、长窗收益至少 0.10%、短窗收益不得为负、方向效率介于 0.10 和 0.65、反转比例至少 0.20，且短窗移动不得超过 3σ。

其目的不是做纯趋势策略，而是在“上涨但仍持续回撤”的路径里测试方向网格。L Profile 的盈利不能作为 N Profile 有效的证据。

## 8. 数据要求

每个标的必须提供完整连续的冻结 1m 数据：

```text
data/backtests/semiconductor-v2.7/SNDKUSDT-1m.csv
data/backtests/semiconductor-v2.7/MUUSDT-1m.csv
data/backtests/semiconductor-v2.7/SOXLUSDT-1m.csv
data/backtests/semiconductor-v2.7/SKHYNIXUSDT-1m.csv
```

CSV 必须包含：

```text
open_time,close_time,open,high,low,close,volume,quote_volume,trade_count
```

每个标的还必须提供 `<SYMBOL>-1m.funding.json`。正式运行不允许缺失 Funding 后自动按零处理；`--allow-missing-funding` 只用于 smoke。

## 9. 交易规则冻结

先运行：

```bash
python scripts/freeze_semiconductor_grid_rules.py \
  --config config/config.yaml \
  --output data/backtests/semiconductor-v2.7/exchange-rules.json
```

脚本只调用公共 `exchangeInfo`，不使用 API Key、不访问账户、不下单。输出包含 tick size、step size、minQty、minNotional、上线时间、合约状态、订单类型和原始响应 SHA-256。

## 10. 回测执行

```bash
python scripts/semiconductor_grid_backtest.py \
  --config config/config.yaml \
  --data-dir data/backtests/semiconductor-v2.7 \
  --output-dir reports/semiconductor-grid-v2.7
```

每个休市窗口按以下顺序处理：

1. 使用对应参考市场日历构造周末和节假日窗口；
2. 从下一参考开盘前 120 分钟截断；
3. 前 180 根 Bar 作为观察期；
4. 剩余部分至少需要 120 根交易 Bar；
5. 生成 N20、N100、L20、L100 候选；
6. 运行 Grid Viability Gate；
7. 使用六个固定种子 `3, 10, 17, 31, 59, 97`；
8. 使用真实 Funding 事件；
9. 窗口末强制清仓；
10. 分市场、Profile、场景和标的输出结果。

## 11. 输出与统计单位

默认目录为 `reports/semiconductor-grid-v2.7/`，输出：

- `window-results.csv`；
- `blocked-windows.csv`；
- `profile-summary.csv`；
- `assessment.csv`；
- `results.json`；
- `final-report.md`。

随机种子不是独立市场样本。汇总时先对同一标的、同一日历窗口的六个种子取均值，再在同一市场组内合并标的，避免把执行随机性伪装成更多历史样本。

## 12. 预注册通过门槛

正式判断按 `market_group + profile` 分开进行，禁止把三种市场合成一个统一假设。主场景需要：

- 至少 8 个独立休市窗口；
- 总净收益为正；
- 正收益窗口比例至少 55%；
- Profit Factor 至少 1.05；
- 最大回撤不超过投入资金的 5%；
- 平均库存拖累比例不超过 35%；
- 最佳单一窗口贡献不超过正收益的 35%；
- `EXECUTION_STRESS` 总收益仍为正。

通过代码：`SEMICONDUCTOR_GRID_RESEARCH_CANDIDATE`。

失败代码：`SEMICONDUCTOR_GRID_NOT_VALIDATED` 或 `NO_VALID_BACKTEST_RUNS`。

即使通过，也只能进入前向模拟和新增数据验证，不能自动授权实盘或提高杠杆。

## 13. 生产策略变化

合入主分支后的生产配置变化：

- Maker 费率上限设为 0，并从交易所核验；
- NYSE 实时白名单改为 `SNDKUSDT`、`MUUSDT`、`SOXLUSDT`；
- BTC、ETH、BCH 不再混入生产候选池；
- 允许最多 100 格的自适应候选；
- 使用按标的格距、区间和库存限制；
- 提高 Regime 入场门槛，增加流动性和均值回归权重；
- 自动开仓默认关闭；
- 测试网强制全天窗口默认关闭；
- 仍保持全局 `NEUTRAL`、1 倍杠杆和 NYSE 盘前强制退出。

`SKHYNIXUSDT` 只在研究注册表中启用，避免 NYSE 全局调度器错误管理 KRX 标的。

## 14. 不允许的解释

以下说法不成立：

- “三笔手工网格盈利，所以全部周末都应开网格”；
- “20 倍保证金收益高，所以杠杆是优势来源”；
- “SKHYNIX 做多盈利，所以中性网格已验证”；
- “Maker 为零，所以不需要考虑 Taker、Funding、滑点和库存”；
- “多跑几个随机种子就等于增加了历史窗口”；
- “四个标的汇总为正，所以每个市场机制都成立”。

v2.7 的唯一目标是判断：**开仓前可见的活跃度和路径特征，能否稳定区分适合密集网格的休市窗口。**
