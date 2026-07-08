# QuietGrid

美股代币休市窗口网格交易系统。当前实现处于 v1.0 基础阶段：先落地离线可测试的配置、调度、网格参数、状态机、风控和 SQLite 持久化，再接入 Binance 测试网。

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m pytest
```

## 启动入口

```powershell
python trader.py
python trader.py --mock-once
python trader.py --binance-check
python trader.py --binance-order-smoke
python trader.py --binance-test-order-smoke
python trader.py --binance-market-roundtrip-smoke
python trader.py --binance-direct-order-diagnose
python trader.py --binance-price-stream-smoke
python trader.py --binance-signed-write-health
python trader.py --binance-listen-key-smoke
python trader.py --binance-algo-stop-smoke
python trader.py --binance-position-smoke
python trader.py --binance-safety-sweep
python trader.py --backtest-csv data/example_klines.csv --backtest-observe-rows 60 --backtest-symbol BTCUSDT --backtest-output reports/backtest.json
python trader.py --backtest-dir data --backtest-observe-rows 60 --backtest-symbol BTCUSDT --backtest-output reports/backtest-batch.json
python trader.py --binance-once
python trader.py --mock-loop
python trader.py --binance-loop
python web.py
```

不带参数的 `trader.py` 只做启动前安全校验和数据库初始化，不会发起交易。`--mock-once` 使用 mock 交易所执行一轮编排验证；`--binance-check` 只检查 Binance 测试网连接、余额和交易规则，不下单；`--binance-order-smoke` 会在 Binance 测试网创建并清理最小 POST_ONLY 限价单和 STOP_MARKET 止损单，用于验证真实下单接口；`--binance-test-order-smoke` 调用 Binance Futures `order/test` 校验签名下单参数，不创建真实订单，当前可验证 LIMIT 和 MARKET，STOP_MARKET 在该测试端点会返回不支持；`--binance-market-roundtrip-smoke` 会在 Binance 测试网用最小可交易数量执行 Market 开仓并立即 reduce-only 平仓，用于验证真实订单创建/平仓接口；`--binance-direct-order-diagnose` 会绕过 python-binance 直接请求 Futures `/order`，用于排查真实下单失败是否来自 SDK 包装层；`--binance-price-stream-smoke` 接收一条 Binance Futures 价格 WebSocket 事件，用于验证真实价格流；`--binance-signed-write-health` 只执行保证金模式和杠杆设置预检，不启动网格；`--binance-listen-key-smoke` 验证 Futures 用户流 listenKey 创建、保活和关闭；`--binance-algo-stop-smoke` 创建一个远离市价的数量型 Algo STOP_MARKET 条件单并立即撤销，用于验证交易所端条件单创建/查询/撤销链路；`--binance-position-smoke` 只读检查测试网持仓模式、每个候选标的的净持仓/LONG/SHORT 暴露和普通/Algo 未成交订单数量；`--binance-safety-sweep` 会撤销测试网 allowlist 标的的普通/Algo 挂单并用 reduce-only 市价单平掉残留仓位，用于烟测失败后的显式安全清扫；`--backtest-csv` 读取单个本地 K 线 CSV 做离线回测，`--backtest-dir` 批量读取目录内 CSV 并生成汇总，两者都不连接交易所、不要求测试网密钥；`--binance-once` 使用 Binance 测试网执行一轮。真实 Binance 单轮/循环会先执行配置的观察期，观察期内不下单；mock 入口默认跳过等待，便于本地验证。循环模式只在显式传入 `--mock-loop` 或 `--binance-loop` 时运行，且仍要求 `BINANCE_TESTNET=true`。

`web.py` 是只读监控页，除原始数据库表外，会单独展示订单状态汇总、挂单费率健康、最近离线回测报告和最近 WARN/ERROR 风险或恢复事件，方便排查测试网下单状态未知、订单同步、策略回测和强制平仓恢复链路。回测面板读取 `reports/*.json` 中最新的报告；运行 `--backtest-output reports/backtest.json` 后刷新页面即可查看。

系统日志通知默认关闭。需要外部告警时，在 `config/config.yaml` 中开启 `notifications.enabled` 并填写 `webhook_url`；`min_level` 默认只推送 `WARN`/`ERROR`，`format` 支持 `generic` 和 `dingtalk`。通知发送失败不会阻断交易日志写入。

离线回测已提供最小 API：先用观察期 K 线调用 `strategy.grid_calculator.calculate_grid_params` 得到 `GridParams`，再把后续 K 线传给 `strategy.backtest.run_grid_backtest`。也可以直接用 `python trader.py --backtest-csv <csv>` 运行单文件回测，或用 `python trader.py --backtest-dir <dir>` 汇总目录内多个 CSV 窗口；CSV 至少包含 `high`、`low`、`close` 列，可选 `timestamp`/`open_time`/`close_time`/`time`。单文件 `--backtest-output <json>` 会保存摘要、网格参数、成交明细、权益曲线、最大回撤、网格胜率、平均单格盈亏、成交密度和简化 Sharpe；批量模式会保存聚合指标、每个文件摘要和失败文件错误。当前回测按 K 线高低价触达模拟网格成交，并在同一根 K 线触发止损或区间击穿时优先停止，避免高估收益。

Binance Futures 测试网当前不提供美股代币永续合约，`config/config.yaml` 中的 `BTCUSDT`、`ETHUSDT`、`BCHUSDT` 只用于测试网连通性和订单烟测。未来切换到真实美股代币环境前，必须从 `selection.symbol_allowlist` 移除这些虚拟币替代标的。

交易所端止损以 `closePosition` 语义为准。若交易所要求必须已有持仓才能挂 close-position 止损，系统会在启动时进入延迟保护模式，并在首次成交形成持仓后立即补挂对应方向的交易所端止损；补挂失败会触发安全平仓。Binance 普通 STOP_MARKET 端点要求改用 Algo Order API 时，适配器会自动切到 Algo close-position 条件单并在撤单/全撤时一并清理。数量型 Algo 条件单仅用于测试网条件单链路烟测，不作为 close-position 止损的等价替代。
Binance Hedge Mode 下，持仓响应会解析为净持仓、LONG 数量和 SHORT 数量；持仓对账和强制离场都会按 LONG/SHORT 暴露分别处理，强制离场时会分别用 `positionSide=LONG/SHORT` 的 reduce-only 市价单平掉两边持仓，避免净持仓为 0 时漏平双边仓位。
普通网格限价单也会显式带 `positionSide`：初始 BUY 开 LONG、初始 SELL 开 SHORT；成交后的补单会按开仓/平仓语义选择 LONG 或 SHORT，避免 Hedge Mode 下订单方向与持仓方向错配。

## 部署

`deploy/systemd/` 提供 Ubuntu systemd 示例。上线前必须确认 `.env` 中 `BINANCE_TESTNET=true`，并先完成测试网验证。
接入真实测试网前还要确认 `config/config.yaml` 的 `selection.symbol_allowlist` 只包含计划交易的美股代币合约，避免误选普通 USDT 合约。

```bash
sudo cp deploy/systemd/quietgrid-trader.service /etc/systemd/system/
sudo cp deploy/systemd/quietgrid-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quietgrid-trader quietgrid-web
sudo systemctl start quietgrid-web
```

交易服务建议在测试网密钥、代理和配置确认后再启动：

```bash
sudo systemctl start quietgrid-trader
```
