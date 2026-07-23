# Round 13 后续研究：既有 BTC 防御机制排重审计

审计时间：2026-07-23

本审计只读取 `reports/robustness/` 中已经生成的 Development/Validation 诊断结果，不运行或读取 Final OOS。用途是避免把已经失败的结构换一个阈值后重复注册为新候选。

## 汇总结论

- 共复核 17 个 seed 17 的 BTC 机制诊断；
- 17/17 的 Validation PnL 为负；
- 17/17 的 Validation Profit Factor 不高于 1；
- 因此 adverse stop、breakout handoff、breakout reversal、opening momentum 和 equity drawdown guard 均不能直接作为下一轮候选；
- 最接近盈亏平衡的是 equity guard 1.5：Validation PnL `-0.058884`、PF `0.990603`，但仍未通过，而且只有单一种子证据；
- breakout reversal 的阈值越宽，Development 收益越高而 Validation 越差，呈现明确的开发集过拟合方向；
- 后续若没有新的可观测因果状态，不继续搜索这些机制的相邻阈值。

| 机制 | 已测变体 | 最佳 Development | 对应 Validation | 最佳 Validation PF | 判定 |
| --- | --- | ---: | ---: | ---: | --- |
| Adverse stop | 0.5 / 1.0 / 1.5 / 2.0 | `+1.219675`（2.0） | `-5.734028` | `0.613488`（1.0） | Validation 全部失败 |
| Breakout handoff | 固定结构 | `+7.746031` | `-1.746601` | `0.765418` | Validation 失败 |
| Breakout reversal | 0.5 / 1.0 / 1.5 / 2.0 | `+23.252486`（2.0） | `-4.695777` | `0.696453`（0.5） | 阈值扩大时过拟合加重 |
| Equity guard | 1.0 / 1.5 / 2.0 / 3.0 | `+13.445871`（3.0） | `-1.637957` | `0.990603`（1.5） | 最接近盈亏平衡但仍失败 |
| Opening momentum | 30 / 60 / 120 / 240 分钟 | `-6.424255`（30m） | `-0.417155` | `0.935716`（30m） | Development 与 Validation 均无正证据 |

## 输入结果与哈希

| 结果文件 | Development PnL / PF | Validation PnL / PF | SHA-256 |
| --- | ---: | ---: | --- |
| `btc-adverse-stop-0p5-seed17-dev-validation-20260721.json` | `-10.848227 / 0.439368` | `-6.264656 / 0.112328` | `7f7c46fdcd91a80b25cfb90a0033052e888dc402ad6c78e299ab676d4784cfde` |
| `btc-adverse-stop-1p0-seed17-dev-validation-20260721.json` | `-1.725596 / 0.905255` | `-2.712165 / 0.613488` | `9364483935557637b83adeb2ead9dd12986c7ffe77ed34698273da15db629bf4` |
| `btc-adverse-stop-1p5-seed17-dev-validation-20260721.json` | `-2.462837 / 0.894218` | `-4.177696 / 0.490913` | `867834b23270dd4ba169d826f58f1547286402cef523d429a5422b46deb16921` |
| `btc-adverse-stop-2p0-seed17-dev-validation-20260721.json` | `+1.219675 / 1.049053` | `-5.734028 / 0.396397` | `223c11e60c8c2fc63d94494b71c9cad0a5084f8dc87988d0de3c6eb4d7ad29b3` |
| `btc-breakout-handoff-seed17-dev-validation-20260720.json` | `+7.746031 / 1.276471` | `-1.746601 / 0.765418` | `0860e45fa9eb599c013ff49f205e7895d0f77e5e0bb136119b11e4a1b0fe7f96` |
| `btc-breakout-reversal-0p5-seed17-dev-validation-20260721.json` | `+11.601143 / 1.488641` | `-2.483895 / 0.696453` | `8af870081dafba5db619f6ffe5b9a7d02e0e88223ed014d9497bd56ab79fc6b2` |
| `btc-breakout-reversal-1p0-seed17-dev-validation-20260721.json` | `+15.484924 / 1.596986` | `-3.221189 / 0.638888` | `25228856052da3cf7d076579dc4b12c8fc8b0e4d01a98bbfe95eeef686cf9870` |
| `btc-breakout-reversal-1p5-seed17-dev-validation-20260721.json` | `+19.368705 / 1.655890` | `-3.958483 / 0.590112` | `75bdb3ce398feac830af92edde0e342a282fb29c5f2e2dce0dd4ba3ca94e400e` |
| `btc-breakout-reversal-2p0-seed17-dev-validation-20260721.json` | `+23.252486 / 1.689895` | `-4.695777 / 0.548256` | `960574170e5d17047155ac549578582a7e50ada550049bc945521984b6c04748` |
| `btc-equity-guard-1p0-seed17-dev-validation-20260720.json` | `+8.916538 / 1.571608` | `-2.196632 / 0.609325` | `d6972c1821289521e039431ff15d4aecf7c01864527bd61456d582642e13d998` |
| `btc-equity-guard-1p5-seed17-dev-validation-20260720.json` | `+2.277961 / 1.099107` | `-0.058884 / 0.990603` | `6ffcf83b65e78f9200b258398accd6fa04fbd565c89336790b43ec48e106c00a` |
| `btc-equity-guard-2p0-seed17-dev-validation-20260720.json` | `+8.699417 / 1.305019` | `-0.650596 / 0.907940` | `dbbf1f2b9daaaa9be0b4c72f1dccb695876a72b8791a64e0b13bf233a5dc4f9f` |
| `btc-equity-guard-3p0-seed17-dev-validation-20260720.json` | `+13.445871 / 1.494315` | `-1.637957 / 0.783906` | `9705b4530e2639738278b7fc800f55eb8c5b010544a3d6e7e740d7abcbf1f489` |
| `btc-opening-momentum-30m-seed17-dev-validation-20260720.json` | `-6.424255 / 0.790532` | `-0.417155 / 0.935716` | `8628075635bcb2cedd3d3677eb2cccfd83cdf6152abd1110a00b391f25ffeb3a` |
| `btc-opening-momentum-60m-seed17-dev-validation-20260720.json` | `-7.814184 / 0.761127` | `-2.490702 / 0.674121` | `4f48e702b1593ffa54cdf617c8eb23fbf8d8dd705f43e1d90f5dca1b7c7d0a17` |
| `btc-opening-momentum-120m-seed17-dev-validation-20260720.json` | `-15.621574 / 0.553377` | `-2.911921 / 0.638735` | `3eb3e3152097177a3473fef7ba2642144d6d30909abbee1eac07049a603c5c94` |
| `btc-opening-momentum-240m-seed17-dev-validation-20260720.json` | `-12.423301 / 0.599568` | `-6.306817 / 0.357851` | `9eb445a8eaa365e89b0a406fbda20215fb03bb0e7d5a86003351a9c96d658f28` |

## 研究边界

- 这些报告只覆盖单一 seed 17，不能单独证明跨种子稳定性；
- Validation 已被消费，只能用于排除重复方向，不能继续据此调阈值；
- Final OOS 继续保持 `SEALED_NOT_EVALUATED`；
- `direction_mode` 继续保持 `NEUTRAL`；
- 本审计没有注册新候选，也没有修改生产默认值。
