# QuietGrid v2.5：Binance 美股永续合约周末/节假日中性网格回测计划

> 目标分支：`codex/profit-protection-backtest-v2.3`  
> 研究对象：Binance 上与美国股票基础资产直接相关、且在美股休市期间仍可交易的 USDT 本位永续合约。  
> 样本长度：使用每个合约从真实上线时间起至执行日前最后一个完整 UTC 日的全部可审计数据，预计最长约五个多月。  
> 研究性质：短历史 Research Validation，不构成稳定盈利或实盘上线证明。

---

## 1. 本轮唯一研究问题

本轮只验证 QuietGrid 最初的经济假设：

```text
美国股票基础市场进入 NYSE/Nasdaq 周末或节假日休市
→ 相关股票永续合约的价格发现、方向性和波动结构发生变化
→ 出现更多能够覆盖真实交易成本的往返振荡
→ NEUTRAL 中性网格在周末/节假日窗口优于普通工作日隔夜和匹配随机窗口
```

本轮禁止转向以下策略：

- SMA 或其他趋势策略；
- 动态 LONG/SHORT；
- Funding carry；
- Premium/Basis 收敛；
- 跨资产价差；
- 跨交易所套利；
- 跨周长期持仓；
- 提高杠杆、资本或库存上限制造收益。

必须先证明“周末/节假日窗口效应”存在，再判断当前中性网格是否能够将其变现。

---

## 2. 工作安全规则

1. 只在 `codex/profit-protection-backtest-v2.3` 分支工作，不修改 `master`。
2. 不连接真实交易账户，不读取私有 API Key，不发送订单。
3. 所有市场数据只使用公开接口和官方公共归档。
4. `direction_mode` 始终为 `NEUTRAL`。
5. 杠杆固定为 `1x`。
6. 不修改生产默认配置。
7. 不允许未来数据、未闭合 K 线、事后市场状态或事后最优方向进入交易决策。
8. Validation 和 Short OOS 一旦打开，不得根据其结果修改参数。
9. 不删除失败标的、月份、窗口或随机种子。
10. 所有随机窗口、成交随机性和抽样种子必须在收益计算前冻结并写入 manifest。
11. 所有新增策略选项默认关闭，并提供回滚路径。
12. 已经被此前研究查看过的数据不得伪装成新的 Final OOS。

---

## 3. 术语和资产边界

本文件中的“美股代币”统一按以下研究定义处理：

> 与美国上市公司股票价格直接相关、在 Binance 衍生品市场以 USDT 结算并可连续交易的股票永续合约。

Codex 不得仅凭配置文件中的符号名称认定合约真实存在。必须通过公开 `exchangeInfo`、历史数据和合约元数据完成发现和审计。

### 3.1 Tier A-Core：正式主验证标的

必须同时满足：

1. 能明确映射到美国上市公司股票；
2. 基础资产遵循 NYSE 或 Nasdaq 交易日历；
3. 合约在基础市场休市时仍有可成交数据；
4. 至少有 4 个完整自然月的 1m 数据；
5. 至少有 14 个完整周末/节假日窗口；
6. Funding、tick size、step size、min qty、min notional 等交易规则可审计；
7. 周末成交量、成交笔数和价格变化不是长期接近零；
8. 数据缺口不影响主要窗口。

正式 H1/H2 判定至少需要：

```text
3 个 Tier A-Core 标的
```

### 3.2 Tier A-Short：短样本标的

满足经济映射，但完整月少于 4 个月或完整周末少于 14 个：

- 只做描述性统计；
- 不参与参数选择；
- 不参与正式 H1/H2 通过判定；
- 不得用其盈利抵消 Tier A-Core 失败。

### 3.3 Tier B：压力测试和负对照

```text
BTCUSDT
ETHUSDT
```

Tier B 只用于验证回测引擎和比较时间窗口效应，不能证明美股休市经济假设成立。

### 3.4 排除条件

出现任一情况即排除：

- 合约不存在、停止交易或无法确认对应股票；
- 周末没有真实成交；
- 价格长期固定或成交量接近零；
- 1m 数据连续性严重不足；
- Funding 或关键交易规则无法取得；
- 上市时间过短，无法形成至少一个独立 Research 和一个 Short OOS 月；
- 交易规则导致当前本金无法构造合法网格订单。

若没有足够 Tier A-Core 标的，输出：

```text
INSUFFICIENT_STOCK_PERP_SAMPLE
```

并停止正式参数研究。

---

## 4. 第一阶段：自动发现合约

新增：

```text
scripts/discover_stock_perpetuals.py
```

必须从 Binance USDⓈ-M Futures 的公开合约信息中记录：

- `symbol`；
- 基础股票或指数映射；
- `contractType`；
- `status`；
- `onboardDate`；
- 首根有效 1m K 线时间；
- 最后完整数据时间；
- `tickSize`；
- `stepSize`；
- `minQty`；
- `minNotional`；
- 价格和数量精度；
- 支持的订单类型；
- Funding 间隔和已取得事件范围；
- 周末是否存在有效成交。

输出：

```text
reports/stock-perp-weekend-grid-v1/symbol-discovery.json
reports/stock-perp-weekend-grid-v1/symbol-discovery.md
```

不得硬编码某个符号一定存在。候选符号只能作为发现提示，最终范围以官方公开元数据和实际历史数据为准。

---

## 5. 数据下载和冻结

新增：

```text
scripts/freeze_stock_perp_data.py
```

### 5.1 数据区间

每个标的独立确定：

```text
start = max(合约真实上线时间, 首个完整 UTC 日)
end   = 执行日前最后一个完整 UTC 日 23:59:59
```

禁止使用当天未闭合数据。

### 5.2 必须下载的数据

1. **1m 合约 K 线**：回测主价格路径；
2. **真实 Funding 事件**：按实际结算时间计入；
3. **Mark Price / Premium Index**：用于脱锚、Funding 和退出合理性审计；
4. **aggTrades 或 trades（可取得时）**：约束 Maker 成交和零成交窗口；
5. **交易规则快照**：价格、数量、名义价值和订单限制。

### 5.3 下载优先级

```text
完整历史月份 → 官方 monthly archive
边界月份/当前月份 → official daily archive
归档缺失的最新尾部 → 公共 REST 补齐
```

每个官方 ZIP 必须校验对应 CHECKSUM。REST 补齐数据必须记录请求参数、时间范围和本地 SHA-256。

### 5.4 不可变数据集

建议目录：

```text
data/backtests/stock-perp-weekend-grid-v1/
  <SYMBOL>-1m.csv
  <SYMBOL>-funding.json
  <SYMBOL>-mark-price.csv
  <SYMBOL>-premium-index.csv
  <SYMBOL>-agg-trades.csv
  manifest.json
```

不得覆盖已冻结数据，除非字节内容与哈希完全一致。

### 5.5 Manifest 必须包含

- 下载源和请求路径；
- 下载时间；
- 文件大小；
- 官方 CHECKSUM；
- 本地 SHA-256；
- 首尾时间；
- 行数；
- 合约交易规则快照；
- Git commit；
- 数据缺口和排除窗口；
- 数据是否被此前研究查看过。

---

## 6. 数据质量审计

新增测试和审计：

```text
scripts/stock_perp_data_audit.py
tests/test_stock_perp_data.py
```

### 6.1 1m K 线检查

- `open_time` 严格递增；
- 无重复时间戳；
- 正常区间为 1 分钟连续；
- `high >= max(open, close)`；
- `low <= min(open, close)`；
- `high >= low`；
- 价格严格为正；
- 成交量、成交额和交易笔数非负；
- 没有未来时间；
- 最后一根已经闭合。

缺失数据处理：

```text
缺口位于观察期 → 当前窗口不可交易
缺口位于交易期 → 当前窗口 DATA_INVALID
缺口位于不相关区间 → 记录但不影响其他窗口
```

禁止对缺口线性插值后继续交易。

### 6.2 Funding 检查

- 时间严格递增；
- 无重复结算事件；
- 费率有限；
- 结算时间可以唯一映射到当时价格；
- Funding 方向正确：正费率时多头支付、空头收取；
- 首次建仓前 Funding 不计入；
- 最终平仓后 Funding 不计入。

### 6.3 上市初期敏感性

分别报告：

- 上市第 1–14 天；
- 上市第 15–30 天；
- 上市 30 天以后。

主结果必须额外提供“排除上市前 14 天”的敏感性版本，避免上市初期价格发现主导结论。

---

## 7. 构造三个公平窗口组

新增：

```text
scripts/build_stock_perp_windows.py
tests/test_stock_perp_windows.py
```

必须复用：

```text
core.scheduler.Scheduler
```

禁止复制第二套 NYSE 日历或自行硬编码周末边界。

### 7.1 W：周末/节假日主实验组

按现有 Scheduler 构造：

```text
前一交易日收盘后
→ 180 根已闭合 1m K 线观察
→ 通过准入后运行网格
→ 下一交易日盘前开始前 120 分钟强制退出
```

标记：

- 普通周末；
- 三日长周末；
- 单日节假日；
- 多日节假日。

### 7.2 O：普通工作日隔夜对照

与 W 使用相同观察期、交易规则和盘前退出距离：

```text
普通交易日收盘后
→ 观察 180 分钟
→ 相同网格
→ 次日盘前前 120 分钟退出
```

生产策略仍禁止 O 窗口交易，本组只用于研究对照。

### 7.3 R：匹配随机窗口对照

每个 W 窗口生成匹配随机窗口，必须匹配：

- 同一标的；
- 同一自然月或季度；
- 相同持续时间；
- 相近 UTC 起始小时；
- 相同数据完整性；
- 相同上市阶段；
- 相同交易规则版本。

不得与 W 或 O 重叠。

固定抽样种子：

```text
3, 10, 17, 31, 59, 97
```

### 7.4 公平性约束

W/O/R 必须使用：

- 相同观察长度；
- 相同交易时长匹配；
- 相同网格参数生成器；
- 相同费用和 Funding；
- 相同成交模型；
- 相同成交随机种子；
- 相同强制退出距离；
- 相同资金、杠杆和库存限制。

不得为周末组单独选择更有利参数。

### 7.5 冻结输出

在计算任何收益前生成：

```text
reports/stock-perp-weekend-grid-v1/window-manifest.csv
reports/stock-perp-weekend-grid-v1/window-manifest.json
reports/stock-perp-weekend-grid-v1/window-overlap-audit.md
```

---

## 8. 样本划分和防止偷看

只使用完整自然月。

### 8.1 五个或更多完整月

```text
最早 3 个完整月 → Research Development
倒数第 2 个完整月 → Validation
最后 1 个完整月 → Sealed Short OOS
```

### 8.2 四个完整月

```text
前 3 个完整月 → Research Development
最后 1 个完整月 → Sealed Short OOS
```

此时没有独立 Validation，只能输出更弱结论。

### 8.3 少于四个完整月

仅作描述性 Tier A-Short 分析，不参与正式结论。

### 8.4 严格规则

1. 窗口 manifest 在收益计算前冻结；
2. H1 只使用 Development；
3. H1 通过后才运行固定策略基线；
4. 固定基线通过 Development 后才打开 Validation；
5. Validation 通过后才打开 Short OOS；
6. Short OOS 只允许对一个冻结候选评估一次；
7. Leave-one-month-out 只作诊断，不用于选择参数；
8. 五个月样本即使全部通过，也只能称为短历史研究候选。

---

## 9. 第一项正式检验：H1 市场假设

新增：

```text
scripts/stock_perp_market_hypothesis.py
```

在任何参数搜索前，对每个 W/O/R 窗口计算：

- realized volatility；
- ATR%；
- high-low range%；
- directional efficiency；
- 最大单向移动；
- 收益符号翻转率；
- 达到真实网格步长的 reversal legs；
- completed grid-sized cycles；
- `fee_adjusted_cycle_capacity`；
- 零成交 K 线比例；
- 每小时成交量和成交笔数；
- Funding 绝对值；
- Premium/Mark 偏离；
- 波动扩张发生率；
- 上市阶段标签。

定义：

```text
fee_adjusted_cycle_capacity
=
completed_grid_cycles × max(step_pct - 2 × maker_fee_rate, 0)
```

低波动本身不是通过条件；必须同时存在足以覆盖双边成本的真实往返振荡。

### 9.1 H1 通过门槛

至少存在 3 个 Tier A-Core 标的，并且 Development 中 W 相对 O 与 R 同时满足：

1. realized volatility 中位数不高于两个对照组的 90%；
2. directional efficiency 不高于两个对照组；
3. fee-adjusted cycle capacity 不低于两个对照组；
4. fee-adjusted cycle capacity 中位数严格为正；
5. 至少 60% 的完整自然月表现出同方向优势；
6. 优势不能只来自一个标的；
7. 最佳单个窗口对全部优势贡献不超过 35%；
8. 按日历窗口聚类的 block bootstrap 不支持“优势完全由随机噪声解释”；
9. 排除上市前 14 天后方向不反转；
10. W 的成交质量不能显著差于 O/R。

统计抽样单位必须是“日历窗口”，不能把同一个周末的多个股票当作完全独立样本。

### 9.2 H1 失败结论

```text
STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED
```

H1 失败后必须停止参数优化和策略回测，只输出失败诊断。

### 9.3 样本不足结论

```text
INSUFFICIENT_STOCK_PERP_SAMPLE
```

不得降低门槛制造候选。

---

## 10. 第二项正式检验：固定策略基线

只有 H1 通过才执行。

新增：

```text
scripts/stock_perp_weekend_grid_backtest.py
tests/test_stock_perp_weekend_grid.py
```

必须复用：

```text
strategy.backtest.run_grid_backtest
当前 GridParams 生成器
当前 Funding 模型
当前 Inventory Manager
当前 ProfitProtectionTracker
```

禁止复制第二套网格 PnL 公式。

### 10.1 固定生产参数快照

第一轮不得调参，固定使用当前生产语义：

```yaml
direction_mode: NEUTRAL
leverage: 1
capital_per_symbol: 500
total_capital_limit: 2000
max_concurrent: 3
observe_hours: 3
force_close_minutes: 120

grid:
  range_method: std
  std_k: 1.8
  min_step_pct: 0.0015
  min_grid_num: 3
  max_grid_num: 20
  max_step_pct: 0.01
  max_range_pct: 0.03
```

每笔订单必须经过真实：

- tick size 价格取整；
- step size 数量取整；
- min qty；
- min notional；
- 最大挂单数；
- POST_ONLY 合法性；
- 资金和库存限制。

### 10.2 B0–B5 对照

#### B0：周末基础中性网格

```text
W 窗口
固定 NEUTRAL 网格
关闭新增利润保护
关闭波动扩张主动减仓
保留真实库存上限、硬止损和强制离场
```

#### B1：工作日隔夜基础中性网格

与 B0 完全相同，仅使用 O 窗口。

#### B2：匹配随机窗口基础中性网格

与 B0 完全相同，仅使用 R 窗口。

#### B3：周末网格 + 当前因果准入

```text
B0 + 当前 Regime 入场过滤
```

#### B4：周末网格 + 完整当前防御

```text
B3
+ 库存防御
+ 净利润峰值保护
+ Wind-down
+ 已注册的因果波动扩张防御
```

#### B5：当前真实生产配置快照

从当前配置自动构造，检测研究设置是否静默偏离生产语义。

不得把 B4 的“交易更少”自动解释为“策略更稳健”。必须分解每层防御对收益、尾部、费用和库存的独立影响。

---

## 11. 执行和成本模型

至少运行三种情景。

### 11.1 BASE

```text
Maker fee: 0.02%
Taker fee: 0.05%
强制退出滑点: 10 bps
Maker fill probability: 0.65
每根 1m Bar 最多成交 2 笔
```

### 11.2 COST50

```text
Maker fee: 0.03%
Taker fee: 0.075%
强制退出滑点: 15 bps
Maker fill probability: 0.65
每根 1m Bar 最多成交 2 笔
```

### 11.3 EXECUTION_STRESS

```text
Maker fill probability: 0.45
Maker fee: 0.03%
Taker fee: 0.10%
强制退出滑点: 25 bps
每根 1m Bar 最多成交 1 笔
同一 Bar 使用最不利成交顺序
```

每个情景使用固定成交种子：

```text
3, 10, 17, 31, 59, 97
```

必须模拟：

- 未成交；
- 部分成交；
- Post Only 拒绝；
- 同 Bar 多价位触及；
- 数量取整后的残余仓位；
- 主动减仓成本；
- 盘前 Taker 退出；
- 真实 Funding；
- 交易规则变化；
- 无成交 K 线。

有 aggTrades 时，应使用真实成交证据约束 Maker fill；没有逐笔数据时必须明确标记模型局限。

---

## 12. 组合层回测

不能简单相加每个标的独立收益。

固定组合约束：

```text
total_capital_limit = 2000 USDT
capital_per_symbol = 500 USDT
max_concurrent = 3
effective_leverage_cap = 1x
```

多个标的同时符合准入时，选择顺序必须在回测前冻结：

```text
Regime 得分更高
→ 预计成本更低
→ 流动性更高
→ symbol 字典序
```

组合层必须统计：

- 资本利用率；
- 同时库存暴露；
- 单一公司集中度；
- 科技股共同方向风险；
- 周末重大新闻共同风险；
- 组合最大回撤；
- 组合强制退出损失；
- 被并发限制阻止的机会；
- 标的选择稳定性。

---

## 13. H2：当前网格能否变现窗口效应

B0 必须在 Development、Validation（若存在）和 Short OOS 中分别满足：

1. 六种子平均净收益严格为正；
2. 至少 4/6 个种子净收益为正；
3. 六种子最差净收益不得灾难性为负；
4. 每个 Tier A-Core 标的不出现灾难性亏损；
5. 组合 Profit Factor > 1；
6. 组合最大回撤 <= 5%；
7. 费用/网格毛利润 <= 35%；
8. 最佳窗口集中度 <= 35%；
9. 最差 5% 窗口均值优于 B1 与 B2；
10. W 的 paired net PnL 高于 O 与 R；
11. COST50 下组合净收益仍为正且 PF > 1；
12. EXECUTION_STRESS 不得出现不可接受的尾部崩溃；
13. 收益不能只来自一个股票、一个月份或一个周末；
14. 排除上市前 14 天后结论不反转；
15. 盘前强制离场后仍保持正的总收益；
16. 最终无遗留订单和仓位；
17. 完整 `pytest -q` 通过。

### 13.1 H1 通过但 H2 失败

```text
STOCK_PERP_WEEKEND_EFFECT_NOT_MONETIZABLE_BY_CURRENT_GRID
```

### 13.2 BASE 通过但成本压力失败

```text
STOCK_PERP_GRID_EDGE_TOO_THIN_AFTER_EXECUTION_COST
```

### 13.3 短历史全部通过

```text
SHORT_HISTORY_STOCK_PERP_WEEKEND_GRID_CANDIDATE
```

该结论只表示值得继续积累未来 OOS，不授权实盘。

---

## 14. 本轮禁止正式参数优化

五个月数据不足以同时承担：

```text
发现规律
选择参数
验证参数
最终样本外确认
```

因此本轮：

- 不搜索止盈阈值；
- 不搜索减仓比例；
- 不为每个股票独立调参；
- 不调整杠杆、资本或库存上限；
- 不切换 LONG/SHORT；
- 不根据 Validation/Short OOS 修改配置。

只允许运行一个“不参与候选选择”的邻域敏感性矩阵：

```text
min_step_pct: 0.0015, 0.0018, 0.0022
range_multiplier: 1.00, 1.25, 1.50
```

用途仅为判断结果是否对附近参数极端敏感。不得从矩阵中挑选最赚钱组合重新运行 Short OOS。

只有未来新增至少 3 个完整自然月后，才允许另写预注册协议并选择唯一候选做向前验证。

---

## 15. 未达目标时的诊断顺序

### 15.1 周末不比对照低波动

- 检查股票映射；
- 检查 NYSE/Nasdaq 时区和节假日日历；
- 检查 W/O/R 是否重叠；
- 检查上市初期影响；
- 数据无误则接受原始假设失败。

禁止通过加止损或缩短窗口让结果变好。

### 15.2 周末波动更低，但往返容量不足

- 比较 completed cycles；
- 比较费后 cycle capacity；
- 判断是否“太平静而无法覆盖费用”；
- 不先优化库存防御。

### 15.3 毛网格收益为正，但费用吞噬

- 核验 Maker 费和 POST_ONLY 假设；
- 分析无效成交和过密网格；
- 不降低真实费用；
- 若成本结构不支持，排除该标的或当前网格结构。

### 15.4 窗口中途盈利、盘前退出后转亏

依次分析：

1. `time_to_force_close` 与库存；
2. Wind-down 成交率；
3. 被动 Maker 减仓；
4. 超时后受控主动减仓；
5. 不允许跨盘前继续持仓。

本轮只记录诊断，不据此在 Short OOS 上调参。

### 15.5 防御改善尾部但消灭收益

- 对比 B0/B3/B4；
- 单独归因每一层防御；
- 不把少交易当作稳定性；
- 若防御只能在收益与尾部间机械交换，记录原始 edge 太薄。

---

## 16. 必须新增的代码和测试

建议新增：

```text
scripts/discover_stock_perpetuals.py
scripts/freeze_stock_perp_data.py
scripts/stock_perp_data_audit.py
scripts/build_stock_perp_windows.py
scripts/stock_perp_market_hypothesis.py
scripts/stock_perp_weekend_grid_backtest.py

tests/test_stock_perp_data.py
tests/test_stock_perp_windows.py
tests/test_stock_perp_market_hypothesis.py
tests/test_stock_perp_weekend_grid.py
```

至少覆盖：

1. 合约发现不依赖硬编码；
2. 上线日期和首根 K 线一致性；
3. NYSE 普通周末分类；
4. 节假日和长周末分类；
5. 普通工作日隔夜分类；
6. 盘前强制离场缓冲；
7. W/O/R 无重叠；
8. 随机窗口持续时间和月份匹配；
9. manifest 固定种子可复现；
10. 观察期只使用入场前 180 根闭合 K 线；
11. 同一参数快照用于 W/O/R；
12. 相同成交随机种子用于 paired comparison；
13. 未闭合 K 线不参与决策；
14. 不读取下一根 K 线；
15. Funding 只在真实结算时间扣除；
16. Funding 方向正确；
17. tick/step/min notional 规则正确；
18. 强制离场使用 Taker 费和滑点；
19. 最终无遗留订单和仓位；
20. Tier A-Core/Tier A-Short/Tier B 不混淆；
21. 已查看数据不被标为新 OOS；
22. 固定输入重复运行结果一致；
23. H1 失败时不会继续运行参数搜索；
24. Validation 失败时不会读取 Short OOS。

最终必须运行：

```bash
pytest -q
```

---

## 17. 输出目录和文件

所有本轮产物放在：

```text
reports/stock-perp-weekend-grid-v1/
```

必须生成：

```text
symbol-discovery.json
symbol-discovery.md
asset-data-audit.md
asset-data-manifest.json
window-manifest.csv
window-manifest.json
window-overlap-audit.md
market-features.csv
market-hypothesis-report.md
baseline-comparison.csv
baseline-comparison.md
symbol-breakdown.csv
month-breakdown.csv
window-breakdown.csv
execution-stress.csv
portfolio-summary.csv
short-oos-summary.csv
sensitivity-matrix.csv
final-report.md
results.json
```

`results.json` 必须包含：

- Git commit；
- 数据和窗口 manifest 哈希；
- 每个数据文件 SHA-256；
- 合约规则快照；
- Tier 分类及排除原因；
- Research/Validation/Short OOS 月份；
- 全部配置和成本假设；
- 六个随机种子；
- H1/H2 每一项判断；
- B0–B5 全部结果；
- BASE/COST50/EXECUTION_STRESS；
- 每个标的、月份、窗口和种子；
- Short OOS 是否被打开；
- 是否修改生产参数；
- 最终结论代码。

---

## 18. 最终结论代码

只能使用以下之一：

```text
NO_STOCK_PERP_DATA
INSUFFICIENT_STOCK_PERP_SAMPLE
STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED
STOCK_PERP_WEEKEND_EFFECT_NOT_MONETIZABLE_BY_CURRENT_GRID
STOCK_PERP_GRID_EDGE_TOO_THIN_AFTER_EXECUTION_COST
SHORT_HISTORY_STOCK_PERP_WEEKEND_GRID_CANDIDATE
```

无论结论如何：

```text
production_defaults_changed = false
master_modified = false
stable_profit_claimed = false
real_money_authorized = false
```

---

## 19. Codex 执行顺序

严格按以下顺序执行：

```text
1. 检查当前分支和 Git 状态
2. 运行完整 pytest
3. 发现和审计真实股票永续合约
4. 下载并冻结公开历史数据
5. 完成数据质量审计
6. 冻结 W/O/R 窗口 manifest
7. 冻结 Research / Validation / Short OOS 月份
8. 只运行 H1 市场假设
9. H1 失败则停止
10. H1 通过后运行固定 B0–B5
11. 运行 BASE、COST50、EXECUTION_STRESS
12. Development 通过后才打开 Validation
13. Validation 通过后才打开 Short OOS
14. 生成全部报告和结论
15. 运行完整 pytest
16. 提交代码、测试、manifest 和报告到当前分支
17. 不修改 master 和生产参数
```

---

## 20. 可直接交给 Codex 的提示词

```text
你当前位于 QuietGrid 仓库的 codex/profit-protection-backtest-v2.3 分支。

阅读并严格执行：

docs/codex-stock-perp-weekend-grid-backtest-v2.5.md

本任务只验证 Binance 美股永续合约在 NYSE 周末/节假日窗口中的低波动中性网格假设。停止 Round 27 和任何 SMA、Funding Carry、Basis、跨资产价差或动态方向研究。

先完成合约发现、数据下载与 CHECKSUM/SHA-256 冻结、Tier A-Core/Tier A-Short/Tier B 分类、W/O/R 窗口 manifest 和 H1 市场假设。H1 未通过时立即停止，不得调网格、止盈、减仓或方向参数。

H1 通过后，使用当前生产参数快照和同一套 NEUTRAL 网格分别运行 B0–B5，并完成 BASE、COST50、EXECUTION_STRESS。Development 未通过不得打开 Validation；Validation 未通过不得打开 Short OOS。Short OOS 只允许对冻结候选运行一次。

不得硬编码股票合约一定存在；以 Binance 公开 exchangeInfo、历史归档和真实交易规则为准。不得连接真实账户、读取私钥或发送订单。不得修改 master 和生产默认参数。

最终把代码、测试、数据 manifest、窗口 manifest 和 reports/stock-perp-weekend-grid-v1/ 下的全部报告提交到当前分支。没有足够数据或没有通过门槛时，必须使用文档规定的失败结论，不得降低标准或继续搜索其他策略。
```
