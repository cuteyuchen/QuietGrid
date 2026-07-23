# Round 4：扩展开发集最终候选协议

生成日期：2026-07-23

## 目的与证据边界

Round 3 的唯一联合入口候选在原 Validation 的 BASE 与 COST50 上均未通过全部门槛。原 Validation 已经消费，不能再作为独立验收证据。本轮明确把原 Development 与已消费 Validation 降级为“扩展开发集”，只用于从预注册的小型候选集合中选择最后一个候选。

- 原 Development、原 Validation、每种成本场景分别验收，禁止跨单元盈亏或风险指标相互抵消；
- Final OOS 54 个窗口继续封存，不计算收益、市场状态或入口标签；
- 固定种子：3、10、17、31、59、97；
- `direction_mode` 保持 `NEUTRAL`；
- 网格、库存、wind-down、利润保护、成交模型和生产参数保持不变。

冻结输入结果：

- `entry-development-results.json`：`8a555feab5a54507f8edd96dc6590d6a6a28caa7f72f73b7a9c2ac7a31c5a7f8`
- `joint-entry-development-results.json`：`797b7ecd775e874351bad4db9ca0d03f3065eabaa77db7b24cf466e06f96e5f4`
- `joint-entry-validation-results.json`：`56a171d59d07520b4a295fbe13721acf4f6ab348dca6d81955cc3c9bdac7c65b`

## 候选集合的冻结方法

BTC 只从 Round 3 Development 中 31 个通过全部 11 项门槛的过滤器产生。以以下四项全部最大化构造非支配 Pareto 前沿：

1. 最差 5% 窗口平均 PnL；
2. 最差种子总 PnL；
3. 六种子平均总 PnL；
4. BTC 交易覆盖率。

前沿共有 8 个条目。其中四组 `rr0.30` 与 `rr0.35` 在 Development 的逐种子结果、汇总和覆盖率完全相同。对 Development 响应完全相同的过滤器，固定保留准入范围更严格者：较低的 `max_directional_efficiency`、较低的 `max_volatility_expansion`、较高的 `min_reversal_ratio`。因此 BTC 冻结为：

```text
de0.40_ve0.95_rr0.35
de0.40_ve1.05_rr0.35
de0.55_ve0.95_rr0.35
de0.55_ve1.05_rr0.35
```

ETH 只保留 Round 2 Development 上 6/6 种子为正、最差种子为正、双标的为正、Profit Factor 与覆盖率合格的四个条目。`de0.45_ve1.00_rr0.55` 与 `de0.50_ve1.00_rr0.55` 的 Development 逐种子结果、汇总和覆盖率完全相同，按同一严格度规则保留前者。因此 ETH 冻结为：

```text
de0.35_ve1.00_rr0.55
de0.35_ve1.05_rr0.55
de0.45_ve1.00_rr0.55
```

本轮只评估上述 4 × 3 = 12 个 BTC/ETH 组合，不扩展网格。

## 四个独立验收单元

每个候选分别评估：

1. `DEV_BASE`：原 Development 108 个窗口，Maker 0.02%、Taker 0.05%、止损滑点 10 bps；
2. `DEV_COST50`：原 Development 108 个窗口，Maker 0.03%、Taker 0.075%、止损滑点 20 bps；
3. `VAL_BASE`：已消费 Validation 54 个窗口，BASE 成本；
4. `VAL_COST50`：已消费 Validation 54 个窗口，COST50 成本。

## 每个单元必须同时通过

1. 6/6 种子组合净收益为正；
2. 六种子最差组合净收益为正；
3. BTC 与 ETH 六种子合计净收益均为正；
4. 六个种子的组合 Profit Factor 均大于 1；
5. 最大回撤不高于 5%；
6. 最大回撤不比同拆分、同成本原始基线恶化超过 5%；
7. 最佳窗口集中度不高于 35%；
8. 最差 5% 窗口平均 PnL 不差于同拆分、同成本原始基线；
9. 六种子平均净收益至少保留同拆分、同成本原始基线的 75%；若基线不为正，则候选必须为正；
10. BTC 至少保留同拆分、同成本基线已交易窗口的 25%；
11. ETH 至少保留同拆分、同成本基线已交易窗口的 25%；
12. 手续费/毛利润不高于同拆分、同成本基线的 1.25 倍。

任一验收单元失败即淘汰该候选。

## 唯一选择规则

若多个候选通过四个单元，依次按以下 minimax 规则选择唯一候选：

1. 最大化四个单元中最小的“最差种子总 PnL”；
2. 最大化四个单元、两个标的中最小的标的总 PnL；
3. 最大化四个单元中最小的六种子平均总 PnL；
4. 最大化四个单元中最小的种子 Profit Factor；
5. 最小化四个单元中最大的最佳窗口集中度；
6. 最小化四个单元中最大的回撤；
7. 按候选 ID 字典序打破完全相同的并列。

## 停止条件

- 没有候选通过四个单元：记录 `NO_ROBUST_CANDIDATE`，Final OOS 保持封存，生产参数不变；
- 选择出唯一候选：先写入包含确切 BTC/ETH 过滤器与 BASE/COST50 门槛的 Final OOS 协议，再对 54 个 Final OOS 窗口执行一次评估；
- Final OOS 任一场景失败：记录 `NO_ROBUST_CANDIDATE`，不得围绕该 Final OOS 再调参或重跑；
- 本协议及后续结果不授权修改生产默认值。
