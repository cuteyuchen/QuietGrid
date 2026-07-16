# QuietGrid 真实盘小额验收

本文档用于真实盘极小资金、低杠杆、单标的验收。未获得账户授权、未确认目标环境、未确认 allowlist 前，不执行会下单或清仓的步骤。

## 前置条件

- 目标账户明确连接真实盘：可用全局 `.env` 设置 `BINANCE_TESTNET=false`，或在 `config/config.yaml` 的目标 `accounts` 条目中设置 `testnet: false` / 对应 `testnet_env=false`。
- `.env` 或 `config/config.yaml` 的账户选择指向待验收账户，例如 `QUIETGRID_ACCOUNT_ID=main` 或命令行 `--account-id main`。
- `config/config.yaml` 的 `selection.symbol_allowlist` 只包含 1 个计划验收的真实盘标的。
- `trading.leverage` 使用低杠杆，建议从 `1` 开始。
- `trading.capital_per_symbol` 使用可接受损失的极小金额。
- `trading.max_concurrent=1`。
- 控制台和交易进程连接同一账户、同一数据库、同一 `BINANCE_TESTNET` 配置。
- 验收期间有人值守 Binance 页面和 QuietGrid 控制台。

## 禁止条件

出现任一情况即停止验收：

- 当前出口 IP 或代理无法稳定访问 Binance Futures。
- `/api/accounts` 显示的账户不是目标账户。
- `/api/summary` 显示 `mode` 不是 `真实盘`。
- allowlist 包含非本次计划交易的标的。
- 只读持仓检查发现非预期仓位或挂单。
- 安全清扫后仍有非预期仓位或挂单残留。
- 余额、杠杆、本金、最大并发与验收计划不一致。

## 只读检查

这些命令不应下单：

```powershell
python trader.py --account-id main --binance-check
python trader.py --account-id main --binance-position-smoke
```

控制台 API 检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/accounts
Invoke-RestMethod "http://127.0.0.1:8001/api/summary?account_id=main"
Invoke-RestMethod "http://127.0.0.1:8001/api/selection/candidates?account_id=main&limit=5"
```

通过条件：

- `summary.mode` 为 `真实盘`。
- `account_id` 为目标账户。
- `account_summary.status` 为 `ok`。
- 可用余额、保证金余额、占用保证金、当前暴露显示合理。
- 候选榜只入选本次计划标的或明确的 allowlist 标的。

## 交易前清扫

此步骤会撤销 allowlist 标的挂单，并尝试 reduce-only 平掉残留仓位。只在确认 allowlist 只包含本次验收标的后执行。

```powershell
python trader.py --account-id main --binance-safety-sweep
python trader.py --account-id main --binance-position-smoke
```

通过条件：

- 普通挂单数为 0。
- Algo 挂单数为 0。
- LONG/SHORT/净持仓均为 0，或只剩确认可接受的非 QuietGrid 仓位。

## 小额有界运行

从 60 秒开始，不直接长时间运行：

```powershell
python trader.py --account-id main --binance-bounded-run --loop-seconds 60
```

也可以在 Vue 控制台确认当前账户和真实盘环境后，使用“启动网格有界运行”，运行时长填 `60` 秒。

观察点：

- 控制台总览账户摘要实时刷新。
- 网格控制页出现目标标的 tab。
- 波动计算阶段进度显示正常。
- 若进入交易阶段，挂单、成交、收益拆分和 PnL 曲线有一致记录。
- 运行结束后自动执行安全清扫和后置持仓检查。

## 验收后复核

```powershell
python trader.py --account-id main --binance-position-smoke
Invoke-RestMethod "http://127.0.0.1:8001/api/summary?account_id=main"
Invoke-RestMethod "http://127.0.0.1:8001/api/sessions/active?account_id=main&include_recent=true&limit=5"
Invoke-RestMethod "http://127.0.0.1:8001/api/logs/system?account_id=main&limit=20"
```

通过条件：

- 后置持仓检查无非预期残留。
- 开放订单为 0，或仅剩明确可解释且需人工处理的订单。
- 最近系统日志包含有界运行、清扫和后置持仓检查记录。
- 数据库会话、订单、成交、收益拆分与 Binance 页面可核对。

## 失败处理

如果有界运行失败或检查不通过：

1. 立即执行：

```powershell
python trader.py --account-id main --binance-safety-sweep
python trader.py --account-id main --binance-position-smoke
```

2. 在 Binance 页面人工确认目标标的无残留挂单和仓位。
3. 保存以下证据：
   - `reports/` 下相关回测或运行报告。
   - 数据库最近 `system_logs`。
   - `/api/sessions/active?include_recent=true` 返回。
   - Binance 订单/成交/仓位页面截图。

## 完成定义

真实盘小额验收只有在以下证据齐全时才算完成：

- 使用真实盘环境执行过只读检查、交易前清扫、60 秒有界运行、后置清扫和后置持仓检查。
- 最终无非预期挂单和仓位残留。
- 控制台总览、候选榜、网格控制、日志审计均能展示同一账户的真实运行记录。
- 记录了命令输出、API 输出和 Binance 页面核对结果。
