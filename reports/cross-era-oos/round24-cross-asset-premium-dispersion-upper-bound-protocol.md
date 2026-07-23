# Round 24：BTC/ETH Cross-Asset Premium Dispersion 乐观上界协议

协议日期：2026-07-23

## 研究目的

Round 23 按每个标的自身 premium 符号执行基差收敛，仅通过 13/16 个年代、成本和标的单元；ETH Validation 的盈利集中度超限，BTC POSTHISTORY COST50 则净亏损且正收益窗口不足。该协议已失败，禁止继续搜索相邻观察期、退出范围、资本或费用参数。

Round 23 结果：`reports/cross-era-oos/round23-basis-convergence-upper-bound-results.json`，SHA-256：`cb7d46463744cb3a0df092ed1081d1f7577537984cb1ae38ed06f63654ddd206`。结果必须为 `NO_PREREGISTERED_BASIS_CONVERGENCE_CANDIDATE`，Final OOS 状态必须为 `SEALED_NOT_EVALUATED`。

本轮测试结构独立的相对价值 family：比较同一分钟 BTC 与 ETH premium，建立两个各自 delta-neutral、方向相反的 Spot/永续 basis book，只赚取 BTC/ETH premium dispersion 的相对收敛和 funding 差异。它不是 Round 23 的单标的参数调整。

## 冻结数据

沿用 Round 23 已冻结的 291 个共同完整窗口：

- BTCUSDT Premium Index manifest SHA-256：`420bab13264b2cfcc45b816c1fe30ad83bc1ff8cbc1467f20de68d9626785684`；
- ETHUSDT Premium Index manifest SHA-256：`2a2c2a7a17f14e48f6f43da84b5b0a4e7a93e763e81d41e3e0eebecf6cbc0fc1`；
- Premium 数据协议 SHA-256：`be795fa8fac4af4bede6cb7418c8624ea9dc5064704eabda44025a6e569b1a8f`。

沿用 Round 22 实际 fundingRate：

- BTCUSDT funding manifest SHA-256：`a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57`；
- ETHUSDT funding manifest SHA-256：`19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f`。

禁止请求新月份、读取 `2023-07..2024-07`、恢复两个已固定排除的数据缺口窗口，或按 Round 23 收益增删窗口。

## 资本与四腿结构

总 gross capital 固定为 `600 USDT`，BTC 与 ETH basis book 等名义分配：

- BTC book gross capital：`300 USDT`，Spot `150 USDT`、永续 `150 USDT`；
- ETH book gross capital：`300 USDT`，Spot `150 USDT`、永续 `150 USDT`。

等名义分配用于使 dimensionless premium spread 的两侧权重对称；不得使用 Round 23 的 500/300 单标的资本，也不得在结果后调整权重。

## 唯一方向规则

每个窗口前 180 根 1m premium close 为观察期。第 180 根 close 分别记为 `btc_entry_premium` 与 `eth_entry_premium`，定义：

`entry_spread = btc_entry_premium - eth_entry_premium`

方向固定为：

- `entry_spread >= 0`：
  - BTC：long Spot、short 永续，`d_btc = +1`；
  - ETH：short Spot、long 永续，`d_eth = -1`；
- `entry_spread < 0`：
  - BTC：short Spot、long 永续，`d_btc = -1`；
  - ETH：long Spot、short 永续，`d_eth = +1`。

两个 basis book 的方向在窗口内均不得改变。`entry_spread = 0` 时固定使用第一种方向，不允许跳过交易。

## 唯一退出与 PnL 定义

1. BTC 与 ETH 每个窗口的 premium `open_time` 必须逐分钟完全对齐；
2. 第 180 根的 `open_time + 60,000ms` 为共同 `entry_time`；
3. 退出候选从第 181 根开始，到窗口最后一根为止；每个候选只允许四腿同时退出；
4. 每个标的的基差收益为：
   - `basis_pnl_symbol = 150 * d_symbol * (entry_premium_symbol - exit_premium_symbol)`；
5. Funding 收益纳入 `entry_time <= funding_time <= exit_time` 的实际事件：
   - `funding_pnl_symbol = 150 * d_symbol * sum(funding_rate_symbol)`；
6. 联合候选毛收益为 BTC 与 ETH 两个标的 basis PnL 和 funding PnL 的总和；
7. Oracle 事后选择联合毛收益最大的唯一退出分钟；并列时固定选择最早退出；
8. BASE maker fee 固定 `0.0002`，COST50 固定 `0.0003`；
9. 四腿入场名义总和为 `600 USDT`，入场费 `600 * maker_fee_rate`，退出费相同；
10. 窗口净收益为 `oracle_joint_gross_pnl - 2 * 600 * maker_fee_rate`；
11. 窗口内路径最小收益为以下序列的最小值：
    - 入场后 `-entry_fee`；
    - 第 181 根至 Oracle 退出分钟的每个 `joint_gross_pnl - entry_fee`；
    - 支付退出费后的最终净收益；
12. 即使所有退出候选均亏损也必须完成一次四腿 round trip；
13. BTC 与 ETH 在入场后都必须至少存在一个实际 funding event，且事件不得跨窗口重复分配。

## 乐观假设与限制

本协议仍故意忽略：真实成交基差与 premium index 的差异、Spot 做空借币、delta 漂移、再平衡、四腿同步成交、Maker 排队、腿间延迟、滑点、盘口冲击、保证金、强平和 funding 预测误差；同时 Oracle 使用完整未来路径选择联合退出。

因此即使通过也不能直接部署或声称稳定收益，只允许进入因果退出与真实成本验证。

## 授权窗口与单元

窗口固定为：

- PREHISTORY：27；
- CURRENT Development：108；
- CURRENT Validation Complete Months：49；
- POSTHISTORY：107。

每个窗口只产生一个 BTC/ETH 联合结果。四个年代/拆分 × BASE/COST50，共 8 个 cell；不同 cell 之间不传递权益或方向状态。

## 上界门槛

8 个 cell 必须分别满足：

- 总净收益严格为正；
- Profit Factor 大于 1；
- 最大回撤不高于 `5%`；
- 最佳盈利窗口占全部正收益比例不高于 `35%`；
- 净收益为正的窗口比例不低于 `25%`；
- BTC/ETH premium 路径覆盖与时间对齐均为 `100%`；
- 每个窗口完成一次四腿 round trip；
- BTC 与 ETH 每个窗口入场后均至少有一个 funding event。

不得用不同年代或成本情景相互抵消。

## 结论规则

- 8/8 cell 全部通过：记录 `CROSS_ASSET_PREMIUM_DISPERSION_WORTH_PREREGISTRATION`；只允许随后定义单一因果退出、冻结真实成交基差和借币成本；
- 任一 cell 失败：记录 `NO_PREREGISTERED_CROSS_ASSET_PREMIUM_DISPERSION_CANDIDATE`，排除本协议定义的等名义四腿 dispersion family；
- 禁止在看到结果后调整观察期、方向、权重、费用、退出范围、窗口或门槛；
- 本轮不选择生产候选，不授权 Final OOS，不修改生产默认值；
- `direction_mode` 保持 `NEUTRAL`，`stable_profit_claimed` 保持 false。
