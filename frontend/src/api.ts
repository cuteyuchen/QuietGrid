import type {
  AuditLog,
  ConsoleSummary,
  ControlState,
  GridSession,
  StrategyConfigData,
  StrategyDiff,
  StrategySettings,
  VerificationRow,
  VolatilityOption,
} from './mock'

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
  next_entry_disabled: boolean
  stop_requested: boolean
  stop_request_status: string
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

type ApiControlState = {
  new_entries_paused: boolean
  new_entries_paused_updated_at: string
  disabled_symbols: string[]
  disabled_symbols_updated_at: string
  session_stop_requests: Array<Record<string, unknown>>
}

type ApiStrategySettings = {
  volatility_method: string
  max_concurrent: number
  observe_hours: number
  min_step_pct: number
  max_grid_num: number
}

type ApiStrategyDiff = {
  key: string
  label: string
  current: string | number
  draft: string | number
}

type ApiStrategyConfig = {
  current: ApiStrategySettings
  draft: ApiStrategySettings
  diff: ApiStrategyDiff[]
  draft_updated_at: string
  options: {
    volatility_methods: VolatilityOption[]
  }
}

export type ConsoleAction =
  | 'safety-sweep'
  | 'testnet-run'
  | 'pause-new-entries'
  | 'resume-new-entries'
  | 'session-stop'
  | 'all-sessions-stop'
  | 'symbol-disable-next-entry'
  | 'symbol-enable-next-entry'

export type ConsoleActionPayload = {
  reason: string
  loopSeconds?: number
  sessionId?: number
  symbol?: string
}

export type ConsoleActionResult = {
  ok: boolean
  action: string
  label: string
  request_id: string
  message: string
  control_state?: ApiControlState
  result?: unknown
}

export type ConsoleData = {
  summary: ConsoleSummary
  controlState: ControlState
  strategyConfig: StrategyConfigData
  sessions: GridSession[]
  verificationRows: VerificationRow[]
  auditLogs: AuditLog[]
}

export async function loadConsoleData(): Promise<ConsoleData> {
  const [summary, controlState, strategyConfig, sessions, verificationRows, auditLogs] = await Promise.all([
    fetchJson<ApiSummary>('/api/summary'),
    fetchJson<ApiControlState>('/api/control-state'),
    fetchJson<ApiStrategyConfig>('/api/strategy-config'),
    fetchJson<ApiList<ApiSession>>('/api/sessions/active?include_recent=true&limit=20'),
    fetchJson<ApiList<ApiVerificationRow>>('/api/verification/testnet'),
    fetchJson<ApiList<ApiAuditLog>>('/api/logs/system?limit=20'),
  ])

  return {
    summary: mapSummary(summary),
    controlState: mapControlState(controlState),
    strategyConfig: mapStrategyConfig(strategyConfig),
    sessions: sessions.items.map(mapSession),
    verificationRows: verificationRows.items.map(mapVerificationRow),
    auditLogs: auditLogs.items.map(mapAuditLog),
  }
}

export async function saveStrategyConfigDraft(draft: StrategySettings): Promise<{ message: string; config: StrategyConfigData }> {
  const response = await fetch('/api/strategy-config/draft', {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      volatility_method: draft.volatilityMethod,
      max_concurrent: draft.maxConcurrent,
      observe_hours: draft.observeHours,
      min_step_pct: draft.minStepPct,
      max_grid_num: draft.maxGridNum,
    }),
  })
  const body = (await response.json()) as (ApiStrategyConfig & { message?: string }) | { detail?: string }
  if (!response.ok) {
    throw new Error('detail' in body && body.detail ? body.detail : `请求失败：${response.status}`)
  }
  return {
    message: 'message' in body && body.message ? body.message : '策略参数草稿已保存',
    config: mapStrategyConfig(body as ApiStrategyConfig),
  }
}

export async function executeConsoleAction(
  action: ConsoleAction,
  payload: ConsoleActionPayload,
): Promise<ConsoleActionResult> {
  const response = await fetch(actionUrl(action, payload), {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      confirm: true,
      reason: payload.reason,
      request_id: crypto.randomUUID(),
      loop_seconds: payload.loopSeconds,
    }),
  })
  const body = (await response.json()) as ConsoleActionResult | { detail?: string }
  if (!response.ok) {
    throw new Error('detail' in body && body.detail ? body.detail : `请求失败：${response.status}`)
  }
  return body as ConsoleActionResult
}

function actionUrl(action: ConsoleAction, payload: ConsoleActionPayload): string {
  if (action === 'session-stop') {
    if (typeof payload.sessionId !== 'number') {
      throw new Error('缺少会话编号，无法停止网格')
    }
    return `/api/actions/sessions/${payload.sessionId}/stop`
  }
  if (action === 'all-sessions-stop') {
    return '/api/actions/sessions/stop-all'
  }
  if (action === 'symbol-disable-next-entry' || action === 'symbol-enable-next-entry') {
    if (!payload.symbol) {
      throw new Error('缺少标的，无法切换下一轮开仓状态')
    }
    const operation = action === 'symbol-disable-next-entry' ? 'disable-next-entry' : 'enable-next-entry'
    return `/api/actions/symbols/${encodeURIComponent(payload.symbol)}/${operation}`
  }
  return `/api/actions/${action}`
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!response.ok) {
    throw new Error(`请求失败：${response.status}`)
  }
  return (await response.json()) as T
}

function mapControlState(value: ApiControlState): ControlState {
  return {
    newEntriesPaused: Boolean(value.new_entries_paused),
    newEntriesPausedUpdatedAt: compactTime(value.new_entries_paused_updated_at),
    disabledSymbols: Array.isArray(value.disabled_symbols) ? value.disabled_symbols : [],
    disabledSymbolsUpdatedAt: compactTime(value.disabled_symbols_updated_at),
    sessionStopRequests: Array.isArray(value.session_stop_requests) ? value.session_stop_requests : [],
  }
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

function mapStrategyConfig(value: ApiStrategyConfig): StrategyConfigData {
  return {
    current: mapStrategySettings(value.current),
    draft: mapStrategySettings(value.draft),
    diff: Array.isArray(value.diff) ? value.diff.map(mapStrategyDiff) : [],
    draftUpdatedAt: compactTime(value.draft_updated_at),
    volatilityOptions: value.options?.volatility_methods || [],
  }
}

function mapStrategySettings(value: ApiStrategySettings): StrategySettings {
  return {
    volatilityMethod: value.volatility_method || 'std',
    maxConcurrent: Math.trunc(toNumber(value.max_concurrent)),
    observeHours: toNumber(value.observe_hours),
    minStepPct: toNumber(value.min_step_pct),
    maxGridNum: Math.trunc(toNumber(value.max_grid_num)),
  }
}

function mapStrategyDiff(value: ApiStrategyDiff): StrategyDiff {
  return {
    key: value.key,
    label: value.label,
    current: value.current,
    draft: value.draft,
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
    nextEntryDisabled: Boolean(value.next_entry_disabled),
    stopRequested: Boolean(value.stop_requested),
    stopRequestStatus: value.stop_request_status || '',
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
