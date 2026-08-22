# Semiconductor Grid Forward OOS v2.9.1 Status

状态日期：`2026-08-22`

## 当前结论

`INSUFFICIENT_FORWARD_OOS`

- 冻结候选：`31111-NEUTRAL`
- 候选 SHA-256：`c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774`
- Exposure cutoff：`2026-08-08T20:45:23.438783+00:00`
- 完整 Forward OOS 窗口：`0/8`
- 首个合格 Forward OOS 窗口：`NONE_YET`
- 正式验收：`NOT_EVALUATED`

完整窗口数为 0 时，仅能报告样本不足。收益、Profit Factor、正收益窗口比例、
回撤、库存拖累、集中度、执行压力和多标的支持等门槛均不得用零值判定为
`PASS` 或 `FAIL`，必须保持 `NOT_EVALUATED`，直到至少取得 8 个唯一、完整且
未暴露的 canonical 休市窗口。

## 冻结基线

| 项目 | 冻结值 |
|---|---|
| v2.9 分支 | `codex/semiconductor-grid-forward-oos-v2.9` |
| v2.9 冻结提交 | `f5c2a6a45b28b348dcc50c3cbbda6d206b11e102` |
| v2.8 来源提交 | `7a24b18c0f027853fcfd939e8d2cb375f2e99c59` |
| 候选冻结提交 | `190c2e49c797ba1b8d1b986270d866b16f8cd201` |
| 冻结标签 | `semiconductor-grid-forward-oos-v2.9-freeze`（已创建，固定指向 `f5c2a6a45b28b348dcc50c3cbbda6d206b11e102`） |
| 参数组合 | `31111`，方向 `NEUTRAL` |
| 执行场景 | `PRIMARY_ZERO_MAKER`、`EXECUTION_STRESS`、`MAKER_PROMO_OFF` |
| 随机种子 | `3, 10, 17, 31, 59, 97` |

冻结标签必须始终指向上表中的 v2.9 冻结提交。创建和推送均不得使用
`--force`，已存在时必须校验目标提交一致；目标不一致时立即失败，不得移动、
删除后重建或覆盖标签。本次标签目标为完整 v2.9 冻结证据提交
`f5c2a6a45b28b348dcc50c3cbbda6d206b11e102`；候选参数冻结提交仍记录为
`190c2e49c797ba1b8d1b986270d866b16f8cd201`。

## 监控与追加规则

监控器只检测开始时间严格晚于 exposure cutoff 的新休市窗口，并且仅在行情、
funding、交易规则、强制平仓覆盖和预期标的组合全部完整后运行冻结候选
`31111-NEUTRAL`。不完整窗口与已暴露历史只能报告，不能进入 Forward OOS
分母，也不能触发回测追加。

新结果只能通过冻结 v2.9 监控/追加入口写入：

```powershell
python -m scripts.semiconductor_grid_forward_oos_v29_monitor `
  --run-time-utc <UTC timestamp>
```

兼容入口 `scripts.semiconductor_grid_forward_oos_v29_append` 现在仅转发到同一
监控器，不绕过冻结校验。

`forward-oos-ledger.csv` 和 `forward-oos-ledger.json` 是 append-only 证据。追加前
必须校验已有记录的有序内容和冻结哈希；若检测到历史行被修改、删除、重排，
或候选、配置、规则、代码哈希漂移，必须停止且不得重写 ledger。相同
`candidate + window + scenario + seed` 的记录不得重复追加。

## 安全边界

- `31111` 参数保持冻结，不允许参数搜索、候选重选或利用 Forward OOS 结果调参。
- 不修改 `config/config.yaml` 或任何生产配置。
- 不开启 `startup_auto_entry`、`testnet_force_window` 或快速观察模式。
- 不启动交易进程，不连接交易所下单，不开启自动交易。
- 杠杆保持 `1x`；Forward OOS 工具只读取冻结研究输入并生成研究证据。

## 证据索引

- `candidate-31111-freeze.json`：候选、参数、场景和 seed 冻结记录。
- `config-freeze.json`：配置快照及生产安全开关。
- `run-manifest.json`：提交、哈希、暴露边界和当前窗口计数。
- `acceptance-gates.json`：正式验收状态；0 个窗口时应为 `NOT_EVALUATED`。
- `window-manifest.json`：窗口完整性、暴露状态和 OOS 资格。
- `forward-oos-ledger.csv` / `forward-oos-ledger.json`：不可变历史与新增结果。
