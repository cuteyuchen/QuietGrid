# QuietGrid 跨周期历史 OOS 预注册协议

生成日期：2026-07-22

## 目的

当前 2024-07-19 至 2026-07-19 数据的旧 Final OOS 已经消费，且锁定策略在该区间未通过。后续不得继续使用该区间选参。本协议在下载和查看更早历史结果前冻结，用于检验现有防御结构能否跨市场周期泛化。

本协议不承诺收益，也不改变生产参数。只有全部门槛通过，才允许把候选标记为“跨周期研究候选”；实盘上线仍需要未来新增、未查看的正向 Final OOS。

## 冻结数据

- 市场：Binance USD-M 永续合约官方归档
- 标的：BTCUSDT、ETHUSDT
- 周期：1m
- 请求区间：2020-07-19T00:00:00Z 至 2024-07-19T00:00:00Z
- 与现有研究数据重叠：无
- 数据必须通过官方 SHA-256、重复行、缺失间隔和时间顺序审计
- 固定成交种子：3、10、17、31、59、97

按周末窗口严格按时间排序后切分：

- Development：前 50%
- Validation：随后 25%
- Final OOS：最后 25%

Validation 只允许在 Development 选出合格候选后打开；Final OOS 只允许在该候选同时通过 Validation BASE 与 COST50 后评估一次。

## 固定基础策略

- 方向：NEUTRAL
- BTC：range_multiplier=1.25、min_step_pct=0.0018、stop_buffer_pct=0.02、max_inventory_notional=200、max_unpaired_lots_per_side=1、reduce_target_step_fraction=0.50
- ETH：range_multiplier=1.00、min_step_pct=0.0018、stop_buffer_pct=0.02、max_inventory_notional=120、EntryFilter(0.50, 1.05, 0.25)、max_unpaired_lots_per_side=0
- Wind-down Maker：reprice_interval=5、initial_offset_steps=1.10、unwind_fraction=1.00
- 每标的本金：BTC 500 USDT、ETH 300 USDT
- Maker 成交概率：0.65
- 窗口尾部 wind-down：1440 bars
- unpaired_lot_cap_enforcement：BAR_BOUNDARY

## 预注册候选

除下列字段外，候选继承固定基础策略。

1. `X0_BASELINE`
   - 利润保护关闭
   - 波动扩张减仓关闭
2. `X1_P3_ACTIVE_PROFIT`
   - activation=2.0
   - minimum_locked_ratio=0.25
   - suppress/reduce/close drawdown=0.25/0.35/0.40
   - passive_after=30、active_after=360
   - passive_fraction=0.35、active_fraction=0.20
3. `X2_P4_VOLATILITY`
   - volatility expansion ratio=1.50
   - consecutive breaches=10
   - reduce fraction=0.20
   - mode=BOTH
4. `X3_P9_EVENT_FREEZE`
   - volatility expansion ratio=1.75
   - consecutive breaches=3
   - reduce fraction=0.20
   - mode=BOTH
   - 减仓后只减不增，连续 10 根正常 bar 后恢复
5. `X4_P3_PLUS_P4`
   - 同时启用 `X1` 与 `X2`
6. `X5_P3_PLUS_P9`
   - 同时启用 `X1` 与 `X3`

不得在查看 Development 结果后新增、删除或修改本轮候选参数。

## Development 选择门槛

候选相对 `X0_BASELINE` 必须同时满足：

1. 6/6 种子组合净收益为正；
2. BTC 与 ETH 的六种子合计净收益均为正；
3. 最差 5% 窗口平均损失改善至少 10%；
4. 最大回撤不比基线恶化超过 5%；
5. 六种子平均净收益保留率至少 75%；
6. RANGE 净利润保留率至少 70%；
7. 手续费/毛利润不高于基线的 1.25 倍；
8. 最佳窗口集中度不高于 35%。

若多个候选合格，依次按以下顺序选择一个：

1. 最差 5% 窗口改善更高；
2. 最大回撤更低；
3. 六种子最差净收益更高；
4. 六种子平均净收益更高；
5. candidate_id 字典序。

若无候选合格，停止本轮，不读取 Validation 和 Final OOS。

## Validation 与 COST50 门槛

选中候选必须在 Validation 的 BASE 与 COST50 中同时满足：

1. 6/6 种子组合净收益为正；
2. BTC 与 ETH 六种子合计净收益均为正；
3. 每个成本场景下，六个种子的组合 Profit Factor 均大于 1；
4. 最大回撤不高于 5%，且不比对应基线恶化超过 5%；
5. 最佳窗口集中度不高于 35%；
6. 最差 5% 窗口均值不差于对应基线；
7. 六种子平均净收益至少保留对应基线的 75%。

任一门槛失败，停止本轮，不读取 Final OOS。

## Final OOS 门槛

Final OOS 只评估 `X0_BASELINE` 与唯一选中候选，并同时运行 BASE 与 COST50。选中候选必须满足与 Validation 相同的全部门槛，且完整 `pytest -q` 通过。

通过时只能表述为：

> 存在跨周期历史研究候选；仍需未来新增、未查看的正向 Final OOS 才能判断是否达到实盘稳定收益标准。

失败时必须表述为：

> 本轮没有稳健候选，保持生产参数不变。
