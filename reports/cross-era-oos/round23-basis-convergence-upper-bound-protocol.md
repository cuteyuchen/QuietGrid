# Round 23：Premium Index 基差收敛乐观上界协议

协议日期：2026-07-23

## 研究目的

Round 22 的未来已知 funding 方向上界仅通过 7/16 个年代、成本和标的单元，实际 funding 单独不足以形成跨年代收益。本轮测试另一项独立收益来源：在观察期结束后按当时 premium 符号固定现货-永续方向，并允许 Oracle 事后选择唯一最佳退出分钟，评估基差收敛与实际 funding 合计后是否足以覆盖双腿 Maker 往返成本。

Round 22 结果：`reports/cross-era-oos/round22-funding-carry-upper-bound-results.json`，SHA-256：`622d359710b3f4e6f6371211a946ae4f33ed24510d5d1262def7ada29c47ab41`，结论必须为 `NO_PREREGISTERED_FUNDING_CARRY_CANDIDATE`，Final OOS 状态必须保持 `SEALED_NOT_EVALUATED`。

本轮是不可部署的乐观经济上界，不选择参数，不授权 Final OOS，不修改生产默认值。

## 冻结 Premium Index 数据

数据协议：`reports/cross-era-oos/round23-premium-index-data-protocol.md`，SHA-256：`be795fa8fac4af4bede6cb7418c8624ea9dc5064704eabda44025a6e569b1a8f`。

冻结 manifest：

- BTCUSDT：`data/backtests/round23_premium_index/binance_um_premium_index_btcusdt_202001_202306_202408_202606.manifest.json`，SHA-256：`420bab13264b2cfcc45b816c1fe30ad83bc1ff8cbc1467f20de68d9626785684`；
- ETHUSDT：`data/backtests/round23_premium_index/binance_um_premium_index_ethusdt_202001_202306_202408_202606.manifest.json`，SHA-256：`2a2c2a7a17f14e48f6f43da84b5b0a4e7a93e763e81d41e3e0eebecf6cbc0fc1`。

每个 manifest 必须包含 65 个官方月档、15 个日档补源请求、291 个完整窗口、1,056,000 行；必须记录两个共同排除的数据缺口窗口，不得包含 `2023-07..2024-07`。

## 冻结 Funding 数据

沿用 Round 22 已冻结的实际 fundingRate：

- BTCUSDT：`a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57`；
- ETHUSDT：`19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f`。

每个 funding manifest 必须包含 65 个官方月档、5,928 个事件、0 个重复事件，官方 checksum 全通过，并明确排除 `2023-07..2024-07`。

## 唯一反事实定义

BTCUSDT 与 ETHUSDT 分别独立评估，不允许标的之间相互抵消。资本固定为：

- BTCUSDT gross capital：`500 USDT`，Spot 与永续各 `250 USDT` 名义；
- ETHUSDT gross capital：`300 USDT`，Spot 与永续各 `150 USDT` 名义。

每个保留窗口固定执行一次 round trip：

1. 按 `open_time` 排序，窗口必须逐分钟完整；
2. 前 180 根 1m premium close 为观察期；第 180 根的 close 为 `entry_premium`，其 `open_time + 60,000ms` 为 `entry_time`；
3. 方向只由 `entry_premium` 决定并保持到退出：
   - `entry_premium >= 0`：long Spot、short 永续，方向系数 `d = +1`；
   - `entry_premium < 0`：short Spot、long 永续，方向系数 `d = -1`；
4. 退出候选从第 181 根开始，到窗口最后一根为止；候选 `exit_time = open_time + 60,000ms`；
5. 对每个退出候选，基差收敛收益固定为：
   - `basis_pnl = perpetual_notional * d * (entry_premium - exit_premium)`；
6. Funding 收益使用 Round 22 实际事件，纳入 `entry_time <= funding_time <= exit_time` 的事件：
   - `funding_pnl = perpetual_notional * d * sum(funding_rate)`；
7. 候选毛收益为 `gross_pnl = basis_pnl + funding_pnl`；
8. Oracle 事后选择 `gross_pnl` 最大的唯一退出分钟；并列时固定选择最早退出；方向不得改变；
9. BASE 与 COST50 都假设 Spot 和永续同步 Maker 成交：
   - BASE maker fee `0.0002`；
   - COST50 maker fee `0.0003`；
10. 入场费为 `gross_capital * maker_fee_rate`，退出费相同；总费用为 `2 * gross_capital * maker_fee_rate`；
11. 窗口净收益为 `oracle_gross_pnl - round_trip_fees`；即使所有退出候选均亏损也必须交易；
12. 窗口内路径最小收益固定为以下序列的最小值：
    - 入场后立即支付入场费：`-entry_fee`；
    - 从第 181 根到 Oracle 退出分钟，每个候选的 `gross_pnl - entry_fee`；
    - Oracle 退出后支付退出费的最终净收益；
13. 每个窗口必须在入场后至少有一个实际 funding event，且所有 eligible funding event 必须唯一映射到该窗口。

## 乐观假设与限制

本协议有意忽略：

- premium index 与真实可交易 Spot/永续成交基差之间的差异；
- 等名义双腿在价格变化后的 delta 漂移和再平衡成本；
- Spot 做空借币可得性与借币利息；
- Maker 排队失败、腿间延迟、滑点、盘口冲击与未成交；
- 保证金、强平、资金占用和交易所限额；
- funding 预测误差；
- Oracle 使用完整未来路径选择退出。

因此结果只能用于排除 family；即使通过也不能直接部署或声称稳定收益。

## 授权窗口与隔离

两个标的必须使用完全相同的 291 个窗口：

- PREHISTORY：27；
- CURRENT Development：108；
- CURRENT Validation Complete Months：49；
- POSTHISTORY：107。

固定排除 `nyse_20200117T210000Z` 与 `nyse_20260626T200000Z`，原因仅为官方 premium 数据无法补齐。禁止读取 CURRENT Final OOS，禁止添加、删除或按收益过滤其他窗口。

## 完整性审计

正式运行必须验证：

- 本协议、Round 22 结果、两个 premium manifest、两个 premium CSV、两个 funding manifest 与两个 funding CSV 的哈希全部匹配；
- 两个 premium manifest 的窗口定义、角色、拆分、边界与行数完全一致；
- 每个窗口 premium 行逐分钟完整，恰好 180 根观察行且至少一个退出候选；
- premium CSV 不包含固定排除窗口或 `2023-07..2024-07`；
- funding_time 严格递增、rate 有限、interval hours 为正；
- 每个窗口入场后至少存在一个 eligible funding event；
- PREHISTORY、CURRENT Development、CURRENT Validation 与 POSTHISTORY 之间不传递权益或方向状态；
- CURRENT Final OOS 状态保持 `SEALED_NOT_EVALUATED`。

任何完整性失败都必须终止，不得生成 family 结论。

## 上界门槛

四个年代/拆分 × 两个成本情景 × BTC/ETH 两个标的，共 16 个 cell。每个 cell 必须分别满足：

- 总净收益严格为正；
- Profit Factor 大于 1；
- 最大回撤不高于 `5%`；
- 最佳盈利窗口占全部正收益比例不高于 `35%`；
- 净收益为正的窗口比例不低于 `25%`；
- premium 路径覆盖率为 `100%`；
- 每个窗口完成一次理论 round trip，并具有至少一个入场后 funding event。

不得用年代、成本情景或标的之间的收益相互抵消。

## 结论规则

- 16/16 cell 全部通过：记录 `BASIS_CONVERGENCE_FAMILY_WORTH_PREREGISTRATION`；只允许随后冻结真实 Spot/永续成交价格、借币成本和盘口数据，并另写单一因果退出候选协议；
- 任一 cell 失败：记录 `NO_PREREGISTERED_BASIS_CONVERGENCE_CANDIDATE`，排除本协议定义的 180 分钟观察、premium 符号方向与 Oracle 退出 family；
- 禁止在看到结果后调整观察期、零值方向、费用、资本、窗口、退出范围、标的或成本情景；
- 本轮不选择候选，不授权 Final OOS，不修改生产默认值；
- `direction_mode` 保持 `NEUTRAL`，`stable_profit_claimed` 保持 false。
