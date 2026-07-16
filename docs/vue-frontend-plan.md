# QuietGrid Vue 前端控制台开发计划

> 阶段目标：在当前环境链路已经可控运行的基础上，将只读 Streamlit 页面升级为 Vue 现代化控制台。控制台按后端启动时连接的账户和环境工作，测试网与真实盘共用同一套展示和受控操作入口。

## 1. 产品定位

QuietGrid Vue 控制台是一个交易运维工作台，不是营销页。首屏直接进入可操作的运行视图，重点服务以下场景：

- 快速判断当前连接环境运行是否健康。
- 查看活动网格、挂单、成交、波动率和系统日志。
- 对正在运行的网格执行明确、可回滚或可兜底的控制动作。
- 调整下一轮网格的关键参数，例如波动率算法、观察窗口、并发数和单标的开关。

## 2. 视觉与交互方向

设计系统采用高对比、数据密集、现代运维控制台风格：

- 主色：`#F59E0B`，用于主操作、焦点态和关键强调。
- 背景：`#0F172A`，深色控制台基底。
- 前景：`#F8FAFC`，保证文本可读性。
- 边框/分隔：`#334155`，用于低干扰区块边界。
- 危险操作：`#EF4444`，用于停止网格、强制平仓、清扫等动作。
- 字体：Inter 或系统 sans-serif，数据列使用等宽数字特性。
- 图标：统一使用 Lucide 风格图标，不使用 emoji 作为结构化图标。
- 交互：所有按钮有 loading、disabled、success/error 反馈；危险操作必须二次确认。

页面应避免大面积装饰渐变、营销式 hero 和卡片嵌套卡片。布局以工作台为主：顶部状态栏、左侧导航、主区域数据表/控制面板、右侧风险与日志摘要。

## 3. 技术架构

### 3.1 前端

- Vue 3 + TypeScript + Vite。
- 当前实现使用单页 `App.vue` 和组合式状态管理；暂未引入 Pinia。
- 当前实现使用页内 tab 导航；暂未引入 Vue Router。
- 当前价格/网格图与 PnL 曲线使用原生 SVG 绘制；暂未引入 ECharts 或 lightweight-charts。
- Lucide Vue 图标。
- CSS 使用 token 化变量和原生 CSS，保持依赖面较小。
- 后续如果图表交互、路由深链或跨组件状态继续扩大，再评估引入 lightweight-charts、Vue Router 或 Pinia。

### 3.2 后端

保留 Python 交易进程。新增一个轻量 API 服务，向 Vue 提供：

- 只读状态 API：数据库摘要、账户摘要、流动性候选榜、活动会话、订单、成交、日志、当前环境验证状态。
- 控制 API：启动/停止网格、暂停新开仓、执行安全清扫、更新策略配置草稿。
- 安全边界：所有危险动作后端二次校验，前端只提交意图，不直接操作交易所。

建议后端 API 使用 FastAPI，独立于交易 loop 进程启动，避免页面服务阻塞交易控制。

## 4. 页面信息架构

### 4.1 总览页

- 全局运行状态：当前环境、账户、loop 状态、最近心跳、活动会话数、开放订单数。
- 账户摘要：余额、可用余额、保证金余额、占用保证金、当前暴露。
- 账户摘要的口径与交易所账户一致：`balance` 为总余额，`available_balance` 为可用余额，`margin_balance` 为保证金余额，`current_exposure` 为当前暴露。
- 流动性候选榜：按 selector 实时评分展示排名、24h 成交额、盘口深度、点差、波动率和是否入选；实时刷新成功后持久化最新快照。
- 风险摘要：剩余仓位、未清算法单、最近错误、费率健康。
- 快捷动作：启动网格有界运行、暂停/恢复新开仓、安全清扫、刷新验证。

### 4.2 网格控制页

- 标的 tab：优先展示正在运行或最近运行的网格，并按流动性候选榜补齐可启动标的。
- 单标的详情：网格参数、波动计算阶段进度、收益拆分、资金费用、保证金变化、年化估算、PnL 曲线、价格/网格图、挂单、成交次数、最近成交、状态流转、波动率快照、风控事件。
- 控制动作：
  - 启用/停用某个标的。
  - 停止某个正在运行的网格。
  - 全局暂停新开仓。
  - 全局恢复新开仓。
  - 执行安全清扫。

### 4.3 策略参数页

- 交易参数：杠杆、单标的本金、最大并发、止盈阈值、止损缓冲。
- 观察参数：观察时长、K 线周期、最小样本。
- 网格参数：最小步长、最大网格数、资金费安全倍数。
- 波动率算法：
  - `std`
  - `parkinson`
  - `garman_klass`
  - `rogers_satchell`
  - `yang_zhang`
- 参数保存策略：
  - 第一阶段只保存为“下轮生效”的配置草稿。
  - 后续阶段再支持对部分运行中参数做受控热更新。

### 4.4 环境验证页

- 当前环境检查结果：连接、手续费、签名写接口、listenKey、持仓 smoke、安全清扫。
- 一键有界流程：选择运行时长，执行“前置持仓检查 -> 有界 loop -> 安全清扫 -> 后置持仓检查”。
- 交易 loop 进程：展示 `quietgrid-trader` systemd 服务状态，或通过配置的 command 运维脚本展示状态，并提供停止/重启入口。
- 展示每一步耗时、状态、错误信息和最终残留。

### 4.5 日志与审计页

- 系统日志、状态日志、控制动作审计日志。
- 支持按级别、模块、标的、时间过滤。
- 危险动作需要记录操作者、动作、参数、结果。

## 5. 当前控制 API

当前 Vue 控制台已接入的主要 API：

```http
GET  /api/summary
GET  /api/accounts
GET  /api/events
GET  /api/sessions/active
GET  /api/sessions/{session_id}
GET  /api/orders?session_id=
GET  /api/trades?session_id=
GET  /api/logs/system
GET  /api/verification/environment
GET  /api/selection/candidates
GET  /api/process/trader
GET  /api/strategy-config

POST /api/actions/bounded-run
POST /api/actions/safety-sweep
POST /api/actions/pause-new-entries
POST /api/actions/resume-new-entries
POST /api/actions/sessions/{session_id}/stop
POST /api/actions/sessions/{session_id}/manual-close
POST /api/actions/sessions/stop-all
POST /api/actions/symbols/{symbol}/start-grid
POST /api/actions/symbols/{symbol}/disable-next-entry
POST /api/actions/symbols/{symbol}/enable-next-entry
POST /api/actions/trader-loop/stop
POST /api/actions/trader-loop/restart

POST /api/strategy-config/draft
```

`/api/verification/testnet` 和 `/api/actions/testnet-run` 仅作为旧前端/脚本兼容入口保留，新控制台使用当前环境命名的 `/api/verification/environment` 和 `/api/actions/bounded-run`。

危险动作统一请求体：

```json
{
  "confirm": true,
  "reason": "手动测试或风险处理",
  "request_id": "uuid"
}
```

## 6. 当前交付状态

### 阶段 A：Vue 项目骨架与静态控制台

- 已完成：`frontend/` Vue 3 + TypeScript + Vite 项目。
- 已完成：布局、设计 tokens、导航、空状态和 mock 数据。
- 已完成：总览、网格控制、策略参数、环境验证、日志视图。

### 阶段 B：只读 API 与真实数据接入

- 已完成：FastAPI 控制台 API。
- 已完成：数据库摘要、账户摘要、流动性候选榜、活动会话、订单、成交、系统日志和环境验证。
- 已完成：`/api/events` SSE 状态事件流，断开时保留 10 秒轮询兜底。

### 阶段 C：当前环境控制动作

- 已完成：安全清扫、一键有界运行、暂停/恢复新开仓。
- 已完成：危险动作二次确认、loading、toast 和后端审计日志。
- 已完成：`/api/actions/bounded-run` 当前环境主入口；旧 `/api/actions/testnet-run` 仅兼容保留。

### 阶段 D：运行中网格控制

- 已完成：停止单个活动会话、手动平仓、停止全部活动网格。
- 已完成：按标的启用/禁用下一轮开仓。
- 已完成：挂单、成交、成交次数、收益拆分、保证金变化、PnL 曲线和价格/网格图。

### 阶段 E：策略参数与波动率算法配置

- 已完成：编辑配置草稿。
- 已完成：波动率算法、杠杆、单标的本金、最大并发、观察窗口、K 线周期、网格步长、网格数、止损缓冲、资金费安全倍数、止盈、资金上限和费率上限。
- 已完成：展示当前运行参数和草稿差异。

## 7. 验收标准

- `npm run build` 通过。
- Python 测试仍通过。
- 前端在 375px、768px、1440px 下无横向滚动和文字重叠。
- 所有危险动作都有二次确认和后端审计日志。
- 前端展示的活动会话数、开放订单数、当前环境验证状态与数据库一致。
- 一键有界流程结束后，页面能明确显示最终仓位和挂单残留为 0。

## 8. 已知约束和外部验收

- 真实盘入口已按当前环境放开，但真实盘极小资金、低杠杆、单标的完整验收仍需要真实账户、目标环境启动配置和明确授权。
- 页面内账户切换已通过 `/api/accounts` 和请求级 `account_id` 支持；每个账户可在配置中声明测试网/真实盘环境。页面只切换账户上下文，不热切换正在运行的交易进程；交易 loop 仍需用同一账户 id 和环境启动。
- 进程级停止/重启交易 loop 已支持 systemd 运维层接口；非 systemd 环境可配置 `process_control.mode=command` 接入自定义状态、停止和重启命令。
- 实时刷新已支持 `/api/events` SSE 状态事件流；前端收到状态版本变化后刷新现有 REST 数据，SSE 断开时保留 10 秒轮询兜底。

## 9. 提交与推送规则

从本阶段开始，每完成一个小阶段：

1. 跑对应测试和构建。
2. 使用中文提交信息。
3. 推送到当前远端分支。
4. 在交付说明中列出提交哈希、验证命令和结果。
