# Semiconductor Grid Forward OOS v2.9.1 Status

状态日期：`2026-08-22`

## 当前结论

`INSUFFICIENT_FORWARD_OOS`

- 冻结候选：`31111-NEUTRAL`
- Candidate SHA：`c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Exposure cutoff（永久冻结）：`2026-08-08T20:45:23.438783+00:00`
- 最新市场数据时间：`2026-08-22T17:33:00+00:00`
- 上次 monitor 执行：`2026-08-22T17:34:58.690630+00:00`
- 本次新合格窗口：`0`
- 本次新增 OOS 行：`0`
- 完整 Forward OOS：`2/8`
- 正式验收：`NOT_EVALUATED`

## 场景累计

| Scenario | Net PnL | Median window PnL | Positive window ratio | Profit factor | Max drawdown | Inventory drag ratio |
|---|---:|---:|---:|---:|---:|---:|
| PRIMARY_ZERO_MAKER | -4.03406218985135 | -2.017031094925675 | 0.0 | 0.0 | 6.3168050875581745 | 2.89435069529515 |
| EXECUTION_STRESS | -4.805861863343044 | -2.402930931671522 | 0.0 | 0.0 | 6.589108418112833 | 3.87810518773716 |
| MAKER_PROMO_OFF | -4.236835439181509 | -2.1184177195907545 | 0.0 | 0.0 | 6.407885578580518 | 2.89435069529515 |

## 冻结与安全边界

- `31111-NEUTRAL` 的参数、标的、场景、seed 和 exposure cutoff 均未修改。
- 不允许参数搜索、候选重选或以本次 OOS 结果调参。
- 冻结 exchange rules 未被覆盖；当前规则只作为 observation 单独记录。
- 未修改生产配置，自动交易仍关闭，经济杠杆为 `1x`。
- Forward OOS ledger 只允许追加；历史前缀在运行前后校验。
