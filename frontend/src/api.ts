import type { AuditLog, ConsoleSummary, GridSession, VerificationRow } from './mock'

type ApiList<T> = {
  items: T[]
}

type ApiSummary = {
  mode: string
  loop_state: string
  heartbeat: string
  active_sessions: number
  open_orders: number
  realized_pnl: number
  latest_system_message: string
  risk_level: string
  balance: number | null
}

type ApiSession = {
  id: number
  symbol: string
  state: string
  state_label: string
  upper: number | null
  lower: number | null
  grid_num: number | null
  step_pct: number | null
  volatility_method: string | null
  volatility_method_label: string
  current_volatility: number | null
  realized_pnl: number | null
  open_order_count: number
}

type ApiVerificationRow = {
  module: string
  name: string
  status: string
  status_label: string
  last_checked: string
  latest_message: string
  detail: string
}

type ApiAuditLog = {
  id: number
  time: string
  level: string
  level_label: string
  module: string
  module_label: string
  message: string
}

export type ConsoleData = {
  summary: ConsoleSummary
  sessions: GridSession[]
  verificationRows: VerificationRow[]
  auditLogs: AuditLog[]
}

export async function loadConsoleData(): Promise<ConsoleData> {
  const [summary, sessions, verificationRows, auditLogs] = await Promise.all([
    fetchJson<ApiSummary>('/api/summary'),
    fetchJson<ApiList<ApiSession>>('/api/sessions/active?include_recent=true&limit=20'),
    fetchJson<ApiList<ApiVerificationRow>>('/api/verification/testnet'),
    fetchJson<ApiList<ApiAuditLog>>('/api/logs/system?limit=20'),
  ])

  return {
    summary: mapSummary(summary),
    sessions: sessions.items.map(mapSession),
    verificationRows: verificationRows.items.map(mapVerificationRow),
    auditLogs: auditLogs.items.map(mapAuditLog),
  }
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!response.ok) {
    throw new Error(`请求失败：${response.status}`)
  }
  return (await response.json()) as T
}

function mapSummary(value: ApiSummary): ConsoleSummary {
  return {
    mode: value.mode,
    loopState: value.loop_state,
    heartbeat: compactTime(value.heartbeat),
    activeSessions: toNumber(value.active_sessions),
    openOrders: toNumber(value.open_orders),
    realizedPnl: toNumber(value.realized_pnl),
    balance: typeof value.balance === 'number' ? value.balance : null,
    riskLevel: value.risk_level,
    latestSystemMessage: value.latest_system_message,
  }
}

function mapSession(value: ApiSession): GridSession {
  return {
    id: value.id,
    symbol: value.symbol,
    state: value.state,
    stateLabel: value.state_label,
    upper: toNumber(value.upper),
    lower: toNumber(value.lower),
    gridNum: Math.trunc(toNumber(value.grid_num)),
    stepPct: toNumber(value.step_pct),
    pnl: toNumber(value.realized_pnl),
    volatilityMethod: value.volatility_method || '',
    volatilityMethodLabel: value.volatility_method_label || value.volatility_method || '-',
    currentVolatility: toNumber(value.current_volatility),
    openOrderCount: toNumber(value.open_order_count),
  }
}

function mapVerificationRow(value: ApiVerificationRow): VerificationRow {
  return {
    name: value.name,
    status: value.status_label,
    statusCode: value.status,
    detail: value.detail || value.latest_message || '暂无验证记录',
    module: value.module,
    lastChecked: compactTime(value.last_checked),
  }
}

function mapAuditLog(value: ApiAuditLog): AuditLog {
  return {
    level: value.level_label || value.level,
    time: compactTime(value.time),
    module: value.module_label || value.module,
    message: value.message,
  }
}

function toNumber(value: number | null | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function compactTime(value: string): string {
  if (!value) {
    return '-'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}
