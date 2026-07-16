# QuietGrid

美股代币休市窗口网格交易系统。利用币安 TradFi 永续合约在美股休市时段（周末、节假日）的"窄幅低波动震荡"特征，配合 Maker 零手续费活动，在动态计算出的价格区间内布置等比网格，反复吃上下波动差价；以"临近开盘强制清仓"为最高优先级风控，从根源规避真实开盘的跳空风险。

> 本文档描述系统设计与实现现状，不构成投资建议。策略默认连接币安测试网，接入真实盘前请通读[部署](#部署)与[风险提示](#风险提示)。

---

## 目录

- [核心策略](#核心策略)
- [实现现状](#实现现状)
- [系统架构](#系统架构)
- [模块说明](#模块说明)
- [数据模型](#数据模型)
- [Web 控制台](#web-控制台)
- [配置](#配置)
- [本地开发](#本地开发)
- [命令行入口](#命令行入口)
- [部署](#部署)
- [验收现状与待办](#验收现状与待办)
- [风险提示](#风险提示)

---

## 核心策略

系统采用三层防护体系，优先级从高到低：

1. **时间窗口硬约束（最高优先级）**：只在 NYSE 休市窗口内交易，临近盘前 2 小时强制撤单清仓离场，彻底消除跳空风险。
2. **击穿-冷静期机制（常规退出）**：价格突破网格区间时主动撤单平仓进入观望，等待 ATR 回落且横盘达标后重新建立网格，应对休市期间的中枢漂移。
3. **动态止损（极端兜底）**：止损线跟随网格下限动态设置（下限再向下留缓冲），作为冷静期机制失灵时的最后防线。

单窗口完整流程：

```
调度器监测时间窗口
   └─ 进入休市窗口
        ├─ 选币：按 24h 成交额 + 订单簿深度选出流动性前 N 标的
        ├─ 观察期：空跑采集 1 分钟 K 线，不交易
        ├─ 区间计算：动态算区间上下沿、ATR 基准、等比间距、网格数量
        └─ 网格引擎：多标的并发，挂满等比 POST_ONLY 限价单，成交后补挂对侧单
             ├─ 止盈（已实现盈利 ≥ 阈值）──► 平仓退出
             ├─ 击穿（价格超出区间）──► 冷静期──► 达标后回到观察期
             ├─ 动态止损（跌破止损线）──► 强制平仓
             └─ 临近开盘（盘前 2h）──► 全局强制清仓，窗口结束
```

所有关键参数均为数据驱动、动态计算：网格区间/间距来自观察期实测波动率，止损线跟随下限移动，标的每窗口重新评估，网格数量反推自波动率——均不写死。网格挂单强制 `POST_ONLY`（Maker），利用零手续费政策降低盈亏平衡门槛。

---

## 实现现状

v1.0 规划的 M1–M9 九个模块全部落地，v1.1 / v1.2 的进阶功能也已实现。相较原始 plan，有两处主动演进：目录从单一 `core/` 拆分为分层包结构；Web 界面从 Streamlit 只读版升级为 Vue 3 + FastAPI 的可操作控制台。

| 模块 | 内容 | 状态 | 主要实现文件 |
|------|------|------|--------------|
| M1 | 时间窗口调度器（NYSE 日历、盘前强制离场） | ✅ | `core/scheduler.py` |
| M2 | 交易所接入层（POST_ONLY、代理、重试、对账） | ✅ | `exchange/base.py`、`exchange/binance.py`、`exchange/mock.py` |
| M3 | 选币（成交额 + 深度打分、黑白名单） | ✅ | `strategy/selector.py` |
| M4 | 观察期 + 动态区间计算（区间/ATR/间距/网格数） | ✅ | `strategy/observer.py`、`strategy/grid_calculator.py`、`strategy/volatility.py` |
| M5 | 网格执行引擎（等比挂单、成交补挂、并发） | ✅ | `strategy/grid_engine.py` |
| M6 | 击穿-冷静期状态机 | ✅ | `strategy/state_machine.py`、`strategy/cooldown.py` |
| M7 | 风控（止盈、动态止损、资金/并发上限、交易所端止损） | ✅ | `strategy/risk.py`、`strategy/grid_engine.py` |
| M8 | SQLite 数据持久化 | ✅ | `db/database.py`、`db/repository.py` |
| M9 | Web 监控 / 控制台 | ✅ | `api.py`（FastAPI）、`frontend/`（Vue 3） |

进阶功能（原 v1.1 / v1.2 TODO）：

- ✅ 外部通知（webhook / 钉钉 / Telegram）——`core/notifications.py`
- ✅ Web 手动启停网格、手动平仓（二次确认）、暂停/恢复新开仓
- ✅ Web 参数热加载（不重启进程修改止盈、并发数等）
- ✅ 离线回测模块——`strategy/backtest.py`
- ✅ Maker 费率监控与告警
- ✅ 滚动重算区间（从静态网格演进为自适应网格）
- ✅ 多账户支持（多套 API Key、独立数据库）

编排层 `strategy/controller.py` 负责把上述模块串起来，承载实际的运行循环、会话生命周期和状态持久化。

尚未闭环的是两个需要外部前置条件的**验收项**（非代码缺失），详见[验收现状与待办](#验收现状与待办)。

---

## 系统架构

双进程模型：

- **交易进程**（`trader.py`）：异步事件循环，承载调度、选币、观察、网格执行、风控、持久化，是策略的执行主体。
- **Web 进程**（`api.py` + `frontend/`）：FastAPI 后端 + Vue 3 前端，读取交易进程写入的数据库并提供受控操作入口，与交易进程通过数据库和控制状态表解耦。

```
trader.py ──► strategy/controller.py（编排）
                 ├─ core/scheduler.py        M1 时间窗口
                 ├─ strategy/selector.py     M3 选币
                 ├─ strategy/observer.py     M4 观察
                 │  └─ grid_calculator.py / volatility.py  区间与波动率
                 ├─ strategy/grid_engine.py  M5 网格 + 交易所端止损
                 ├─ strategy/state_machine.py + cooldown.py  M6 状态机
                 ├─ strategy/risk.py         M7 风控决策
                 ├─ exchange/*.py            M2 交易所（binance / mock）
                 ├─ core/notifications.py    外部告警
                 └─ db/*.py                  M8 持久化
                        │
                        ▼  SQLite（含 control_state 控制表）
                        │
api.py（FastAPI）◄──────┘  读取 + 受控写入
   └─ frontend/（Vue 3 + Vite + TS）
```

技术栈：Python 3.10+ / asyncio、python-binance（含 ccxt 备选）、pandas_market_calendars（NYSE 日历）、numpy + pandas、SQLite3、FastAPI + uvicorn、loguru、httpx[socks] + aiohttp（SOCKS5 代理）；前端 Vue 3 + Vite + TypeScript。

目录结构：

```
QuietGrid/
├── core/          scheduler(M1)、config、models、logging_config、notifications
├── exchange/      base(抽象)、binance、mock
├── strategy/      selector(M3)、observer/grid_calculator/volatility(M4)、
│                  grid_engine(M5)、state_machine/cooldown(M6)、risk(M7)、
│                  controller(编排)、backtest(回测)
├── db/            database(建表/连接)、repository(读写封装)
├── frontend/      Vue 3 控制台（src/App.vue、api.ts、mock.ts）
├── config/        config.yaml
├── deploy/        systemd 部署示例
├── docs/          live-acceptance.md、vue-frontend-plan.md
├── tests/         21 个测试模块，覆盖各模块
├── trader.py      交易进程入口
├── api.py         FastAPI 后端
└── web.py         Streamlit 时代遗留的后端工具函数
```

---

## 模块说明

**M1 调度器**（`core/scheduler.py`）：基于 `pandas_market_calendars` 的 NYSE 日历（`America/New_York` 时区），只在 NYSE 休市且距下一次盘前（04:00 NY）超过 `force_close_minutes`（默认 120）时判定为可交易窗口；进入盘前缓冲区触发强制离场。严格校验 tz-aware 时间。

**M2 交易所层**：`exchange/base.py` 定义约 20 个抽象方法（REST 行情/账户/下单 + WebSocket 成交与价格回调）。`binance.py` 用 `timeInForce=GTX` 实现 POST_ONLY，支持 REST 与 WebSocket 独立代理、测试网/真实盘 WS 分流、指数退避重试、下单状态未知时的对账重试；交易所端止损用 `closePosition` STOP_MARKET，在普通端点不可用时自动切换到 Algo 条件单。`mock.py` 是完整的内存实现，供测试与回测使用。Hedge Mode 下持仓解析为净/LONG/SHORT 三路，挂单显式携带 `positionSide`。

**M3 选币**（`strategy/selector.py`）：候选池限定 USDT 永续、`status=TRADING`，支持 allowlist / blacklist；并发拉取 24h 成交额与订单簿深度，按 `成交额×0.7 + 深度×0.3`（权重可配）归一化打分排序，截取 `max_concurrent` 个。

**M4 观察 + 区间**：`observer.py` 负责定时观察循环（可被强制离场中断）与 K 线/资金费采集；`grid_calculator.py` 计算区间（`std`：均值 ± k·σ；`quantile`：分位数；及 `volatility.py` 的 OHLC 波动率法）、ATR 基准（默认周期 14）、动态间距（`max(|资金费|×安全倍数, min_step_pct)`）、网格数量（区间/间距，上限 `max_grid_num`）与止损价（`下沿×(1 - stop_buffer_pct)`）。含最小可交易区间、最大区间（低波动策略上限）、价格漂移出界等保护。

**M5 网格引擎**（`strategy/grid_engine.py`）：启动时拉取交易规则（tick/step/最小数量/最小名义额）取整、设逐仓保证金与杠杆、下方挂 BUY 上方挂 SELL 的 POST_ONLY 单并补挂交易所端止损，失败则整体回滚。成交后 BUY→上一格补 SELL、SELL→下一格补 BUY。`sync_orders` 将本地订单与交易所实际挂单对账，处理成交/部分成交/消失订单。

**M6 状态机**（`state_machine.py` + `cooldown.py`）：`ALLOWED_TRANSITIONS` 守卫合法状态迁移并记录审计历史，状态含 IDLE / OBSERVING / RUNNING / COOLDOWN / CLOSING / STOPPED，另加运维用 `PAUSED`。冷静期评估独立于状态机：需同时满足静默时长达标、当前 ATR < `基准×atr_recovery_ratio`（0.80）、近窗振幅 < `min_step_pct×amplitude_multiplier` 才允许重新观察。

**M7 风控**（`strategy/risk.py` 决策 + `grid_engine.py` 交易所端执行）：止盈（已实现盈利 ≥ `take_profit_usdt`）、动态止损（价格触及 `stop_loss_price`）、区间击穿（转 COOLDOWN）、强制离场（委托调度器判定）、资金上限（`total_capital_limit`）与并发上限（`max_concurrent`）。所有阈值走配置，非硬编码。

**回测**（`strategy/backtest.py`）：用观察期 K 线算出 `GridParams` 后，把后续 K 线交给 `run_grid_backtest`。按 K 线高低价触达模拟成交，同根 K 线触发止损或击穿时优先停止以避免高估收益，输出权益曲线、最大回撤、网格胜率、平均单格盈亏、成交密度和简化 Sharpe。

**通知**（`core/notifications.py`）：`WebhookNotifier` 挂在系统日志的 WARN/ERROR 钩子上，支持 `generic` / `dingtalk` / `telegram` 格式；发送失败不阻断交易日志写入。

---

## 数据模型

SQLite（`db/database.py` 建表，`db/repository.py` 读写封装，开启 WAL）。共 9 张表 —— plan 规格的 6 张加 3 张实现扩展：

| 表 | 用途 |
|------|------|
| `windows` | 每个休市窗口的运行记录 |
| `sessions` | 单标的网格会话（含状态、盈亏、close_reason 等） |
| `trades` | 逐笔成交 |
| `orders` | 网格挂单及其状态 |
| `state_logs` | 状态迁移审计 |
| `system_logs` | INFO/WARN/ERROR 系统日志（通知与费率健康来源） |
| `control_state` | 控制台受控指令与状态（交易进程下一轮读取执行） |
| `selection_candidates` | 选币候选与评分快照 |
| `round_candidates` | 每轮选币候选明细 |

---

## Web 控制台

Vue 3 + FastAPI，读取交易进程数据库并提供受控操作。控制台不直接下单，而是把"停止/平仓/暂停"等写入 `control_state`，由交易循环下一轮执行——保证交易动作始终经过策略进程的安全检查。

监控：会话概览、活动会话、挂单状态汇总、成交明细、选币候选、Maker 费率健康、系统日志、最近离线回测报告、测试网/环境验证结果。

操作：手动启停单个标的网格、手动平仓（二次确认，写入独立审计与 close_reason）、暂停/恢复新开仓、停止全部网格、按标的禁用/启用下一轮开仓、保存策略参数草稿（热加载）、触发只读环境校验 / 安全清扫 / 有界运行、通过 systemd（或自定义命令）控制交易 loop 进程。

实时更新：默认连 `/api/events` SSE 状态流，检测到会话/挂单/成交/控制状态/日志变化后复用 REST 刷新；SSE 断开时自动回退到 10 秒轮询。

参数热加载：草稿中的 `capital_per_symbol`、`leverage`、`max_concurrent`、`take_profit_usdt`、`total_capital_limit`、`max_maker_fee_rate` 在下一轮 `run_once` 时生效；波动率算法、K 线周期、观察窗口、止损缓冲、资金费安全倍数、网格参数用于下一轮新建网格。

> 安全提示：`config.yaml` 中 `web.auth_token` 默认为空，且 CORS 仅放行本地开发端口。仅限本机使用尚可；对外暴露前必须配置鉴权并收紧网络访问，否则控制台操作接口是敞开的。

---

## 配置

主配置 `config/config.yaml`，密钥走 `.env`。关键分组：

- `trading`：杠杆（10x）、单标的本金（200 USDT）、总资金上限（1000 USDT）、止盈阈值（10 USDT）、止损缓冲、最大并发、Maker 费率上限与检查周期。
- `timing`：观察时长、强制离场提前量（120 min）、冷静期重观察、调度检查周期、循环间隔；含测试网加速开关。
- `grid`：区间方法（std/quantile）、std_k、分位数、最小间距、安全倍数、最大网格数、最大区间、滚动重算开关与周期。
- `cooldown`：ATR 周期、静默窗口、ATR 回落比例、振幅倍数。
- `selection`：候选数量、成交额/深度权重、深度档位、allowlist / blacklist。
- `proxy`：SOCKS5 / HTTP 代理。
- `web` / `process_control`：控制台端口、鉴权、交易 loop 进程控制方式（systemd 或自定义命令）。
- `notifications`：开关、webhook 地址、格式、最低推送级别。
- `database` / `accounts`：数据库路径与可选多账户配置。

多账户：在 `accounts` 中声明账户 id、显示名、测试网/真实盘、密钥环境变量名和独立数据库路径。运行时用 `--account-id <id>` 或 `.env` 的 `QUIETGRID_ACCOUNT_ID` 选择；`--all-accounts` 对全部账户执行同一检查/烟测/有界运行。未配置 `accounts` 时沿用旧的 `BINANCE_API_KEY` / `BINANCE_API_SECRET` 和全局 `BINANCE_TESTNET`。

> 币安测试网不提供美股代币永续，`allowlist` 中的 `BTCUSDT`/`ETHUSDT`/`BCHUSDT` 仅用于测试网连通与烟测；切换真实美股代币环境前必须移除这些替代标的。

---

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m pytest
```

前端：

```powershell
cd frontend
npm install
npm run dev      # 开发服务器
npm run build    # 构建（含 vue-tsc 类型检查）
```

---

## 命令行入口

不带参数的 `python trader.py` 只做启动前安全校验和数据库初始化，不交易。常用入口：

| 命令 | 说明 |
|------|------|
| `--mock-once` / `--mock-loop` | mock 交易所跑一轮 / 循环，默认跳过等待，本地验证编排 |
| `--binance-check` | 只检查连接、余额、交易规则和 Maker 费率，不下单 |
| `--binance-once` / `--binance-loop` | 用 Binance 跑一轮 / 循环（先执行观察期，期内不下单） |
| `--binance-bounded-run` | 有界运行（默认 60 秒；`--binance-test-run` 为兼容别名） |
| `--binance-position-smoke` | 只读检查持仓模式、各标的净/LONG/SHORT 暴露和挂单数 |
| `--binance-safety-sweep` | 撤销 allowlist 标的挂单并 reduce-only 平掉残留仓位 |
| `--backtest-csv <csv>` / `--backtest-dir <dir>` | 单文件 / 批量离线回测，不连交易所 |
| `--account-id <id>` / `--all-accounts` | 选择账户 / 对全部账户执行 |
| `python api.py`（或 uvicorn） | 启动 Web 后端 |

另有一批 Binance 链路烟测（`--binance-order-smoke`、`--binance-test-order-smoke`、`--binance-market-roundtrip-smoke`、`--binance-direct-order-diagnose`、`--binance-price-stream-smoke`、`--binance-signed-write-health`、`--binance-listen-key-smoke`、`--binance-algo-stop-smoke`），分别验证下单/平仓、签名参数、价格流、写健康、用户流 listenKey 和 Algo 条件单链路。回测面板读取 `reports/*.json` 中最新报告。

---

## 部署

`deploy/systemd/` 提供 Ubuntu systemd 示例。启动前务必确认 `.env` 的 `BINANCE_TESTNET`、账户密钥和 `QUIETGRID_ACCOUNT_ID` 指向目标环境（`BINANCE_TESTNET=false` 连真实盘），并确认 `config.yaml` 的 `selection.symbol_allowlist` 只包含计划交易的美股代币合约。

```bash
sudo cp deploy/systemd/quietgrid-trader.service /etc/systemd/system/
sudo cp deploy/systemd/quietgrid-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quietgrid-trader quietgrid-web
sudo systemctl start quietgrid-web
# 密钥、代理、配置确认无误后再启动交易服务：
sudo systemctl start quietgrid-trader
```

真实盘极小资金验收按 `docs/live-acceptance.md` 执行并留存命令、API 和 Binance 页面证据。

---

## 验收现状与待办

已完成（测试网，2026-07-09 记录于 plan.md §10.4）：`--binance-check` / `--binance-position-smoke` / `--binance-safety-sweep` 前置验证通过；单账户 60s / 180s / 600s 有界运行通过，结束后挂单与仓位残留均为 0；当前 `default` 单账户下 `--all-accounts` 入口兼容性验证通过。

待办（均卡在外部前置条件，非代码缺失）：

- [ ] 配置第二套测试网 API Key 后，用 `--all-accounts` 验证多账户隔离（目前只配了 `default` 一个账户）。
- [ ] 测试网稳定后，安排极小资金、低杠杆、单标的实盘验证（`docs/live-acceptance.md` 目前只有流程清单，无完成记录）。

---

## 风险提示

- 策略默认连接测试网。接入真实盘前必须移除 allowlist 中的测试币替代标的、确认账户与环境变量、完成实盘小资金验收。
- Web 控制台默认无鉴权，切勿在未配置 `web.auth_token` 和网络隔离的情况下对外暴露。
- 通知目前挂在 WARN/ERROR 日志级别上，成交类事件仅在写入相应级别日志时触发；若需逐笔成交推送需另行扩展。
- 杠杆交易存在爆仓与极端行情风险，本系统的强制离场、冷静期和动态止损是防护而非保证。请自行评估并对自己的资金负责。
