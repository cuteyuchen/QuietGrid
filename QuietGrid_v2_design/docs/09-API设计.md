# 09. API 设计

## 1. 原则

- API 进程不直接下单；
- 所有写操作转成 `control_commands`，由交易进程重新做风控；
- 所有危险操作要求幂等键、二次确认和审计；
- REST 获取快照，SSE 推送变化；
- 对外暴露前必须鉴权和限制网络来源。

## 2. 路由概览

```text
GET  /api/v2/health
GET  /api/v2/dashboard
GET  /api/v2/accounts
GET  /api/v2/sessions
GET  /api/v2/sessions/{id}
GET  /api/v2/sessions/{id}/grid
GET  /api/v2/sessions/{id}/inventory
GET  /api/v2/sessions/{id}/risk
GET  /api/v2/sessions/{id}/events
GET  /api/v2/regime/{symbol}
GET  /api/v2/backtests
GET  /api/v2/backtests/{id}
GET  /api/v2/config/active
GET  /api/v2/config/versions
POST /api/v2/commands/pause
POST /api/v2/commands/resume
POST /api/v2/commands/close-session
POST /api/v2/commands/stop-all
POST /api/v2/commands/safety-sweep
POST /api/v2/config/drafts
POST /api/v2/config/{id}/validate
POST /api/v2/config/{id}/activate
GET  /api/v2/events              # SSE
```

## 3. Dashboard 响应示例

```json
{
  "environment": "testnet",
  "trader_status": "RUNNING",
  "window": {
    "in_window": true,
    "force_close_at": "2026-07-20T06:00:00Z",
    "remaining_minutes": 515
  },
  "equity": "600.00",
  "window_pnl": "2.31",
  "window_loss_budget_remaining": "4.50",
  "active_sessions": 1,
  "global_risk_level": "LOW",
  "data_health": "HEALTHY"
}
```

## 4. 控制命令

```json
POST /api/v2/commands/close-session
{
  "session_id": 123,
  "reason": "operator requested risk reduction",
  "confirmation": "CLOSE-AAPLUSDT",
  "idempotency_key": "uuid"
}
```

响应只表示命令已排队：

```json
{
  "command_id": "cmd_xxx",
  "status": "PENDING"
}
```

交易进程可能因为会话不存在、已经关闭或安全状态冲突而拒绝命令。

## 5. 配置变更

配置分为：

- 安全降低型：减小资金、降低杠杆、降低并发，可在风控确认后应用；
- 风险提高型：提高资金、杠杆、并发或损失上限，只能从下一窗口生效，并要求验证状态；
- 算法参数：必须创建新版本，回测通过后激活。

## 6. SSE 事件

```text
session.updated
order.updated
trade.created
regime.updated
inventory.updated
risk.updated
command.updated
alert.created
backtest.completed
```

SSE 消息只传资源 ID 和版本号，前端再通过 REST 拉取完整数据，降低复杂度。

## 7. 错误格式

```json
{
  "error": {
    "code": "RISK_COMMAND_REJECTED",
    "message": "当前已触发周末损失熔断，不能恢复新开仓",
    "details": {"window_id": 42}
  },
  "request_id": "req_xxx"
}
```

## 8. 安全

- Token 不得为空；
- 建议反向代理启用 TLS；
- 限制 CORS；
- 控制接口限速；
- 不在 API 响应中返回密钥；
- 所有危险命令记录 IP、用户代理和操作者；
- 可增加只读与管理员两种角色。
