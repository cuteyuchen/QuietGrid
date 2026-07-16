import type {
  AuditLog,
  AccountOption,
  AccountSummary,
  ConsoleSummary,
  ControlState,
  GridOrder,
  GridPerformance,
  GridRound,
  GridSession,
  GridTrade,
  LiquidityCandidate,
  TraderProcessState,
  StrategyConfigData,
  StrategyDiff,
  StrategySettings,
  VerificationRow,
  VolatilityOption,
} from './mock'

type ApiList<T> = {
  items: T[]
}

type ApiAccounts = {
  mode: string
  current_account_id: string
  current_account_label: string
  accounts: ApiAccountOption[]
}

type ApiAccountOption = {
  id: string
  label: string
  mode: string
  binance_testnet: boolean
  database: string
  selected: boolean
  has_api_key: boolean
}

type ApiSummary = {
  mode: string
  account_id: string
  account_label: string
  loop_state: string
  heartbeat: string
  active_sessions: number
  open_orders: number
  realized_pnl: number
  latest_system_message: string
  risk_level: string
  balance: number | null
  available_balance: number | null
  margin_balance: number | null
  initial_margin: number | null
  maintenance_margin: number | null
  unrealized_pnl: number | null
  current_exposure: number | null
  account_summary: ApiAccountSummary
}

type ApiAccountSummary = {
  status: string
  error: string
  asset: string
  balance: number | null
  available_balance: number | null
  margin_balance: number | null
  initial_margin: number | null
  maintenance_margin: number | null
  unrealized_pnl: number | null
  current_exposure: number | null
}

type ApiV2RegimeDecision = {
  symbol: string
  state: string
  grid_score: number
  allowed: number | boolean
  reasons?: string[]
  hard_blocks?: string[]
  component_scores?: Record<string, number>
  model_version?: string
  as_of_time?: string
}

type ApiV2InventorySnapshot = {
  session_id: number
  symbol: string
  net_qty: number
  net_notional: number
  gross_notional: number
  avg_entry_price: number | null
  unrealized_pnl: number
  utilization: number
  risk_score: number
  risk_level: string
  unpaired_lots: number
  as_of_time: string
}

type ApiV2RiskSnapshot = {
  session_id: number | null
  symbol: string | null
  risk_level: string
  action: string
  reason: string
  session_pnl: number | null
  window_pnl: number | null
  inventory_utilization: number | null
  limits?: Record<string, number>
  as_of_time: string
}

type ApiV2Dashboard = {
  environment: string
  trader_status: string
  account_id: string
  equity: number
  available_balance: number | null
  current_exposure: number | null
  window_id: number | null
  window_pnl: number
  window_loss_budget: number
  window_loss_budget_remaining: number
  active_sessions: number
  open_orders: number
  global_risk_level: string
  data_health: string
  latest_regime: ApiV2RegimeDecision | null
  latest_inventory: ApiV2InventorySnapshot | null
  latest_risk: ApiV2RiskSnapshot | null
}

export type V2RegimeDecision = {
  symbol: string
  state: string
  gridScore: number
  allowed: boolean
  reasons: string[]
  hardBlocks: string[]
  componentScores: Record<string, number>
  modelVersion: string
  asOfTime: string
}

export type V2InventorySnapshot = {
  sessionId: number
  symbol: string
  netQty: number
  netNotional: number
  grossNotional: number
  avgEntryPrice: number | null
  unrealizedPnl: number
  utilization: number
  riskScore: number
  riskLevel: string
  unpairedLots: number
  asOfTime: string
}

export type V2RiskSnapshot = {
  sessionId: number | null
  symbol: string
  riskLevel: string
  action: string
  reason: string
  sessionPnl: number | null
  windowPnl: number | null
  inventoryUtilization: number | null
  limits: Record<string, number>
  asOfTime: string
}

export type V2DashboardData = {
  environment: string
  traderStatus: string
  accountId: string
  equity: number
  availableBalance: number | null
  currentExposure: number | null
  windowId: number | null
  windowPnl: number
  windowLossBudget: number
  windowLossBudgetRemaining: number
  activeSessions: number
  openOrders: number
  globalRiskLevel: string
  dataHealth: string
  latestRegime: V2RegimeDecision | null
  latestInventory: V2InventorySnapshot | null
  latestRisk: V2RiskSnapshot | null
}

export type V2CommandType = 'pause' | 'resume' | 'close-session' | 'stop-all' | 'safety-sweep'

export type V2CommandResult = {
  command_id: string
  status: string
}

export type V2SessionEvent = {
  eventId: number
  eventType: string
  aggregateType: string
  aggregateId: string
  eventTime: string
  availableTime: string
  payload: Record<string, unknown>
}

export type V2BacktestDataset = {
  name: string
  relativePath: string
  sizeBytes: number
  modifiedAt: string
}

export type V2BacktestRun = {
  runId: string
  symbol: string
  status: string
  startedAt: string
  completedAt: string
  dataStart: string
  dataEnd: string
  fillModel: string
  parameterVersion: string
  codeCommit: string
  reportPath: string
  config: Record<string, unknown>
  metrics: Record<string, number | string | null>
}

export type V2BacktestDetail = V2BacktestRun & {
  report: {
    summary?: Record<string, number | string | null>
    grid_params?: Record<string, unknown>
    fills?: Array<Record<string, unknown>>
    equity_curve?: Array<{
      bar_index?: number
      timestamp?: string
      equity?: number
      drawdown?: number
      realized_pnl?: number
      unrealized_pnl?: number
      close?: number
    }>
    validation?: {
      sample_label?: string
      warning?: string
      walk_forward?: {
        status?: string
        fold_count?: number
        profitable_fold_ratio?: number
        aggregate_pnl?: number
        average_pnl?: number
        worst_fold_pnl?: number
        worst_fold_drawdown?: number
        folds?: Array<Record<string, unknown>>
      }
      monte_carlo?: {
        status?: string
        simulations?: number
        total_pnl_p05?: number
        total_pnl_p50?: number
        total_pnl_p95?: number
        max_drawdown_p95?: number
        max_drawdown_p99?: number
        loss_probability?: number
      }
    }
  } | null
}

export type V2BacktestRequest = {
  dataset: string
  symbol: string
  observeRows: number
  capital: number
  leverage: number
  makerFeeRate: number
  fillModel: string
}

type ApiLiquidityCandidate = {
  rank: number
  symbol: string
  score: number | null
  volume_score: number | null
  depth_score: number | null
  volume_24h: number | null
  depth_usdt: number | null
  bid_price: number | null
  ask_price: number | null
  spread_pct: number | null
  selected: boolean
  disabled: boolean
  status: string
  error: string
  volatility_method: string | null
  volatility_method_label: string
  volatility_value: number | null
  current_volatility: number | null
  volatility_window: number | null
  current_volatility_window: number | null
  stage: string
  snapshot_at?: string
  price?: number | null
  range_lower?: number | null
  range_upper?: number | null
  range_width_pct?: number | null
  threshold_met?: boolean
  session_id?: number | null
  market_updated_at?: string
  last_kline_close_at?: string
  data_stale?: boolean
}

type ApiSession = {
  id: number
  window_id: number
  symbol: string
  state: string
  state_label: string
  upper: number | null
  lower: number | null
  grid_num: number | null
  step_pct: number | null
  volatility_method: string | null
  volatility_method_label: string
  volatility_value: number | null
  volatility_window: number | null
  current_volatility: number | null
  current_volatility_window: number | null
  current_volatility_at: string
  baseline_atr: number | null
  stop_loss_price: number | null
  capital: number | null
  leverage: number | null
  open_time: string
  close_time: string
  close_reason: string
  volatility_stage: string
  volatility_stage_label: string
  volatility_progress_pct: number | null
  volatility_remaining_seconds: number | null
  realized_pnl: number | null
  open_order_count: number
  trade_count: number
  next_entry_disabled: boolean
  stop_requested: boolean
  stop_request_status: string
  stop_request_type: string
  control_requested: boolean
  control_request_status: string
  control_request_action: string
}

type ApiGridRound = {
  window_id: number
  window_start: string
  window_end: string
  status: string
  status_label: string
  total_pnl: number | null
  session_count: number
  active_session_count: number
}

type ApiOrder = {
  id: number
  session_id: number
  symbol: string
  order_id: string
  grid_index: number | null
  side: string
  side_label: string
  price: number | null
  qty: number | null
  status: string
  status_label: string
  created_at: string
  filled_at: string
  fill_price: number | null
}

type ApiTrade = {
  id: number
  session_id: number
  symbol: string
  order_id: string
  grid_index: number | null
  side: string
  side_label: string
  price: number | null
  qty: number | null
  quote_qty: number | null
  grid_pnl: number | null
  fee: number | null
  funding_fee: number | null
  trade_time: string
}

type ApiPnlPoint = {
  time: string
  value: number | null
}

type ApiGridPerformance = {
  gross_grid_pnl: number | null
  trading_fees: number | null
  funding_fee: number | null
  realized_pnl: number | null
  unpaired_pnl: number | null
  initial_margin: number | null
  current_margin: number | null
  margin_change: number | null
  roi: number | null
  annualized_roi: number | null
  duration_hours: number | null
  trade_count: number
  unpaired_trade_count: number
  pnl_curve: ApiPnlPoint[]
}

type ApiSessionDetail = {
  session: ApiSession
  orders: ApiOrder[]
  trades: ApiTrade[]
  performance: ApiGridPerformance
  position: Record<string, unknown>
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
  startable_symbols: string[]
  session_stop_requests: Array<Record<string, unknown>>
  session_control_requests: Array<Record<string, unknown>>
  round_start_request: Record<string, unknown> | null
  runtime_id: string
  runtime_started_at: string
  round_start_available: boolean
  current_round_id: number | null
  round_state: string
  round_started_at: string
  last_scan_at: string
  next_scan_at: string
}

type ApiTraderProcessState = {
  available: boolean
  mode: string
  service: string
  state: string
  detail: string
}

type ApiStrategySettings = {
  volatility_method: string
  leverage: number
  capital_per_symbol: number
  max_concurrent: number
  scan_candidate_count: number
  observe_hours: number
  observe_kline_interval: string
  min_step_pct: number
  min_tradable_range_pct: number
  max_grid_num: number
  stop_buffer_pct: number
  safety_multiplier: number
  take_profit_usdt: number
  total_capital_limit: number
  max_maker_fee_rate: number
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
  | 'bounded-run'
  | 'pause-new-entries'
  | 'resume-new-entries'
  | 'session-stop'
  | 'session-manual-close'
  | 'all-sessions-stop'
  | 'symbol-disable-next-entry'
  | 'symbol-enable-next-entry'
  | 'symbol-start-grid'
  | 'grid-round-start'
  | 'session-pause'
  | 'session-resume'
  | 'environment-verify-readonly'
  | 'trader-loop-stop'
  | 'trader-loop-restart'

export type ConsoleActionPayload = {
  accountId?: string
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
  gridRounds: GridRound[]
  selectedGridRoundId: number | null
  sessions: GridSession[]
  liquidityCandidates: LiquidityCandidate[]
  traderProcessState: TraderProcessState
  verificationRows: VerificationRow[]
  auditLogs: AuditLog[]
}

export type AccountsData = {
  mode: string
  currentAccountId: string
  currentAccountLabel: string
  accounts: AccountOption[]
}

export async function loadAccounts(): Promise<AccountsData> {
  const value = await fetchJson<ApiAccounts>('/api/accounts')
  return {
    mode: value.mode,
    currentAccountId: value.current_account_id || 'default',
    currentAccountLabel: value.current_account_label || value.current_account_id || '默认账户',
    accounts: Array.isArray(value.accounts) ? value.accounts.map(mapAccountOption) : [],
  }
}

export async function loadV2Dashboard(accountId?: string): Promise<V2DashboardData> {
  const value = await fetchJson<ApiV2Dashboard>(accountUrl('/api/v2/dashboard', accountId))
  return {
    environment: value.environment,
    traderStatus: value.trader_status,
    accountId: value.account_id,
    equity: value.equity,
    availableBalance: value.available_balance,
    currentExposure: value.current_exposure,
    windowId: value.window_id,
    windowPnl: value.window_pnl,
    windowLossBudget: value.window_loss_budget,
    windowLossBudgetRemaining: value.window_loss_budget_remaining,
    activeSessions: value.active_sessions,
    openOrders: value.open_orders,
    globalRiskLevel: value.global_risk_level,
    dataHealth: value.data_health,
    latestRegime: value.latest_regime ? mapV2Regime(value.latest_regime) : null,
    latestInventory: value.latest_inventory ? mapV2Inventory(value.latest_inventory) : null,
    latestRisk: value.latest_risk ? mapV2Risk(value.latest_risk) : null,
  }
}

export async function executeV2Command(
  command: V2CommandType,
  payload: {
    accountId?: string
    reason: string
    confirmation: string
    sessionId?: number
  },
): Promise<V2CommandResult> {
  const response = await fetch(accountUrl(`/api/v2/commands/${command}`, payload.accountId), {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      reason: payload.reason,
      confirmation: payload.confirmation,
      idempotency_key: crypto.randomUUID(),
      session_id: payload.sessionId,
      requested_by: 'quietgrid-console',
    }),
  })
  const body = (await response.json()) as V2CommandResult | { detail?: string }
  if (!response.ok) {
    throw new Error('detail' in body && body.detail ? body.detail : `请求失败：${response.status}`)
  }
  return body as V2CommandResult
}

export async function loadV2SessionEvents(sessionId: number, accountId?: string): Promise<V2SessionEvent[]> {
  const value = await fetchJson<{ items: Array<{
    id: number
    event_type: string
    aggregate_type: string
    aggregate_id: string
    event_time: string
    available_time: string
    payload: Record<string, unknown>
  }> }>(accountUrl(`/api/v2/sessions/${sessionId}/events`, accountId))
  return value.items.map((item) => ({
    eventId: item.id,
    eventType: item.event_type,
    aggregateType: item.aggregate_type,
    aggregateId: item.aggregate_id,
    eventTime: item.event_time,
    availableTime: item.available_time,
    payload: item.payload || {},
  }))
}

export async function loadV2BacktestDatasets(accountId?: string): Promise<V2BacktestDataset[]> {
  const value = await fetchJson<{ items: Array<{
    name: string
    relative_path: string
    size_bytes: number
    modified_at: string
  }> }>(accountUrl('/api/v2/backtests/datasets', accountId))
  return value.items.map((item) => ({
    name: item.name,
    relativePath: item.relative_path,
    sizeBytes: item.size_bytes,
    modifiedAt: item.modified_at,
  }))
}

export async function loadV2Backtests(accountId?: string): Promise<V2BacktestRun[]> {
  const value = await fetchJson<{ items: Array<Record<string, unknown>> }>(
    accountUrl('/api/v2/backtests', accountId),
  )
  return value.items.map(mapV2BacktestRun)
}

export async function loadV2BacktestDetail(runId: string, accountId?: string): Promise<V2BacktestDetail> {
  const value = await fetchJson<Record<string, unknown>>(
    accountUrl(`/api/v2/backtests/${encodeURIComponent(runId)}`, accountId),
  )
  return {
    ...mapV2BacktestRun(value),
    report: value.report && typeof value.report === 'object'
      ? value.report as V2BacktestDetail['report']
      : null,
  }
}

export async function startV2Backtest(
  request: V2BacktestRequest,
  accountId?: string,
): Promise<V2BacktestDetail> {
  const response = await fetch(accountUrl('/api/v2/backtests', accountId), {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      dataset: request.dataset,
      symbol: request.symbol,
      observe_rows: request.observeRows,
      capital: request.capital,
      leverage: request.leverage,
      maker_fee_rate: request.makerFeeRate,
      fill_model: request.fillModel,
    }),
  })
  const body = await response.json() as Record<string, unknown> & { detail?: string }
  if (!response.ok) {
    throw new Error(body.detail ? String(body.detail) : `请求失败：${response.status}`)
  }
  return {
    ...mapV2BacktestRun(body),
    report: body.report && typeof body.report === 'object'
      ? body.report as V2BacktestDetail['report']
      : null,
  }
}

function mapV2Regime(value: ApiV2RegimeDecision): V2RegimeDecision {
  return {
    symbol: value.symbol,
    state: value.state,
    gridScore: Number(value.grid_score || 0),
    allowed: Boolean(value.allowed),
    reasons: Array.isArray(value.reasons) ? value.reasons : [],
    hardBlocks: Array.isArray(value.hard_blocks) ? value.hard_blocks : [],
    componentScores: value.component_scores || {},
    modelVersion: value.model_version || '',
    asOfTime: value.as_of_time || '',
  }
}

function mapV2Inventory(value: ApiV2InventorySnapshot): V2InventorySnapshot {
  return {
    sessionId: value.session_id,
    symbol: value.symbol,
    netQty: value.net_qty,
    netNotional: value.net_notional,
    grossNotional: value.gross_notional,
    avgEntryPrice: value.avg_entry_price,
    unrealizedPnl: value.unrealized_pnl,
    utilization: value.utilization,
    riskScore: value.risk_score,
    riskLevel: value.risk_level,
    unpairedLots: value.unpaired_lots,
    asOfTime: value.as_of_time,
  }
}

function mapV2Risk(value: ApiV2RiskSnapshot): V2RiskSnapshot {
  return {
    sessionId: value.session_id,
    symbol: value.symbol || '',
    riskLevel: value.risk_level,
    action: value.action,
    reason: value.reason,
    sessionPnl: value.session_pnl,
    windowPnl: value.window_pnl,
    inventoryUtilization: value.inventory_utilization,
    limits: value.limits || {},
    asOfTime: value.as_of_time,
  }
}

function mapV2BacktestRun(value: Record<string, unknown>): V2BacktestRun {
  const metrics = value.metrics && typeof value.metrics === 'object'
    ? value.metrics as Record<string, number | string | null>
    : {}
  const config = value.config && typeof value.config === 'object'
    ? value.config as Record<string, unknown>
    : {}
  return {
    runId: String(value.run_id || ''),
    symbol: String(value.symbol || ''),
    status: String(value.status || 'UNKNOWN'),
    startedAt: String(value.started_at || ''),
    completedAt: String(value.completed_at || ''),
    dataStart: String(value.data_start || ''),
    dataEnd: String(value.data_end || ''),
    fillModel: String(value.fill_model || ''),
    parameterVersion: String(value.parameter_version || ''),
    codeCommit: String(value.code_commit || ''),
    reportPath: String(value.report_path || ''),
    config,
    metrics,
  }
}

export function consoleEventsUrl(accountId?: string): string {
  return accountUrl('/api/events', accountId)
}

export async function loadConsoleData(accountId?: string, gridRoundId?: number): Promise<ConsoleData> {
  const [summary, controlState, strategyConfig, gridRoundResponse] = await Promise.all([
    fetchJson<ApiSummary>(accountUrl('/api/summary', accountId)),
    fetchJson<ApiControlState>(accountUrl('/api/control-state', accountId)),
    fetchJson<ApiStrategyConfig>(accountUrl('/api/strategy-config', accountId)),
    fetchJson<ApiList<ApiGridRound>>(accountUrl('/api/grid-rounds', accountId)),
  ])
  const [liquidityCandidates, traderProcessState, verificationRows, auditLogs] = await Promise.all([
    fetchJson<ApiList<ApiLiquidityCandidate>>(
      accountUrl('/api/selection/candidates?limit=20', accountId),
    ).catch(() => ({ items: [] })),
    fetchJson<ApiTraderProcessState>(
      accountUrl('/api/process/trader', accountId),
    ).catch(() => ({
      available: false,
      mode: 'unavailable',
      service: 'quietgrid-trader',
      state: 'unknown',
      detail: '交易进程状态暂不可用',
    })),
    fetchJson<ApiList<ApiVerificationRow>>(
      accountUrl('/api/verification/environment', accountId),
    ).catch(() => ({ items: [] })),
    fetchJson<ApiList<ApiAuditLog>>(
      accountUrl('/api/logs/system?limit=20', accountId),
    ).catch(() => ({ items: [] })),
  ])
  const gridRounds = gridRoundResponse.items.map((round, index, items) => mapGridRound(round, items.length - index))
  const preferredRoundId = controlState.current_round_id
    ?? gridRounds.find((round) => round.activeSessionCount > 0)?.id
    ?? gridRounds[0]?.id
    ?? null
  const selectedGridRoundId = gridRounds.some((round) => round.id === gridRoundId)
    ? gridRoundId ?? null
    : preferredRoundId
  const sessionList: ApiList<ApiSession> = selectedGridRoundId
    ? await fetchJson<ApiList<ApiSession>>(
        accountUrl(
          `/api/sessions/active?include_recent=true&limit=200&window_id=${selectedGridRoundId}`,
          accountId,
        ),
      )
    : { items: [] }
  const sessionDetails = await Promise.all(
    sessionList.items.map(async (session) => {
      try {
        return await fetchJson<ApiSessionDetail>(
          accountUrl(`/api/sessions/${session.id}`, accountId),
        )
      } catch {
        return {
          session,
          orders: [],
          trades: [],
          performance: {} as ApiGridPerformance,
          position: {},
        }
      }
    }),
  )
  const displayedCandidates: ApiList<ApiLiquidityCandidate> = selectedGridRoundId
    ? await fetchJson<ApiList<ApiLiquidityCandidate>>(
        accountUrl(`/api/grid-rounds/${selectedGridRoundId}/candidates`, accountId),
      ).catch(() => liquidityCandidates)
    : liquidityCandidates

  return {
    summary: mapSummary(summary),
    controlState: mapControlState(controlState),
    strategyConfig: mapStrategyConfig(strategyConfig),
    gridRounds,
    selectedGridRoundId,
    sessions: sessionDetails.map(mapSessionDetail),
    liquidityCandidates: displayedCandidates.items.map(mapLiquidityCandidate),
    traderProcessState: mapTraderProcessState(traderProcessState),
    verificationRows: verificationRows.items.map(mapVerificationRow),
    auditLogs: auditLogs.items.map(mapAuditLog),
  }
}

export async function saveStrategyConfigDraft(draft: StrategySettings, accountId?: string): Promise<{ message: string; config: StrategyConfigData }> {
  const response = await fetch(accountUrl('/api/strategy-config/draft', accountId), {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      volatility_method: draft.volatilityMethod,
      leverage: draft.leverage,
      capital_per_symbol: draft.capitalPerSymbol,
      max_concurrent: draft.maxConcurrent,
      scan_candidate_count: draft.scanCandidateCount,
      observe_hours: draft.observeHours,
      observe_kline_interval: draft.observeKlineInterval,
      min_step_pct: draft.minStepPct,
      min_tradable_range_pct: draft.minTradableRangePct,
      max_grid_num: draft.maxGridNum,
      stop_buffer_pct: draft.stopBufferPct,
      safety_multiplier: draft.safetyMultiplier,
      take_profit_usdt: draft.takeProfitUsdt,
      total_capital_limit: draft.totalCapitalLimit,
      max_maker_fee_rate: draft.maxMakerFeeRate,
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
  const response = await fetch(accountUrl(actionUrl(action, payload), payload.accountId), {
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
  if (action === 'session-manual-close') {
    if (typeof payload.sessionId !== 'number') {
      throw new Error('缺少会话编号，无法手动平仓')
    }
    return `/api/actions/sessions/${payload.sessionId}/manual-close`
  }
  if (action === 'session-pause' || action === 'session-resume') {
    if (typeof payload.sessionId !== 'number') {
      throw new Error('缺少会话编号，无法暂停或恢复网格')
    }
    return `/api/actions/sessions/${payload.sessionId}/${action === 'session-pause' ? 'pause' : 'resume'}`
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
  if (action === 'symbol-start-grid') {
    if (!payload.symbol) {
      throw new Error('缺少标的，无法启动网格')
    }
    return `/api/actions/symbols/${encodeURIComponent(payload.symbol)}/start-grid`
  }
  if (action === 'grid-round-start') {
    return '/api/actions/grid-rounds/start'
  }
  if (action === 'environment-verify-readonly') {
    return '/api/actions/environment/verify-readonly'
  }
  if (action === 'trader-loop-stop') {
    return '/api/actions/trader-loop/stop'
  }
  if (action === 'trader-loop-restart') {
    return '/api/actions/trader-loop/restart'
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

function accountUrl(path: string, accountId?: string): string {
  const normalized = String(accountId || '').trim()
  if (!normalized) {
    return path
  }
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}account_id=${encodeURIComponent(normalized)}`
}

function mapAccountOption(value: ApiAccountOption): AccountOption {
  return {
    id: value.id || 'default',
    label: value.label || value.id || '默认账户',
    mode: value.mode || (value.binance_testnet ? '测试网' : '真实盘'),
    binanceTestnet: Boolean(value.binance_testnet),
    database: value.database || '',
    selected: Boolean(value.selected),
    hasApiKey: Boolean(value.has_api_key),
  }
}

function mapControlState(value: ApiControlState): ControlState {
  return {
    newEntriesPaused: Boolean(value.new_entries_paused),
    newEntriesPausedUpdatedAt: compactTime(value.new_entries_paused_updated_at),
    disabledSymbols: Array.isArray(value.disabled_symbols) ? value.disabled_symbols : [],
    disabledSymbolsUpdatedAt: compactTime(value.disabled_symbols_updated_at),
    startableSymbols: Array.isArray(value.startable_symbols) ? value.startable_symbols : [],
    sessionStopRequests: Array.isArray(value.session_stop_requests) ? value.session_stop_requests : [],
    sessionControlRequests: Array.isArray(value.session_control_requests) ? value.session_control_requests : [],
    roundStartRequest: value.round_start_request && typeof value.round_start_request === 'object' ? value.round_start_request : null,
    runtimeId: value.runtime_id || '',
    runtimeStartedAt: compactTime(value.runtime_started_at),
    roundStartAvailable: Boolean(value.round_start_available),
    currentRoundId: typeof value.current_round_id === 'number' ? value.current_round_id : null,
    roundState: value.round_state || 'IDLE',
    roundStartedAt: compactTime(value.round_started_at),
    lastScanAt: compactTime(value.last_scan_at),
    nextScanAt: compactTime(value.next_scan_at),
  }
}

function mapTraderProcessState(value: ApiTraderProcessState): TraderProcessState {
  return {
    available: Boolean(value.available),
    mode: value.mode || 'unavailable',
    service: value.service || 'quietgrid-trader',
    state: value.state || 'unknown',
    detail: value.detail || '',
  }
}

function mapSummary(value: ApiSummary): ConsoleSummary {
  return {
    mode: value.mode,
    accountId: value.account_id || 'default',
    accountLabel: value.account_label || value.account_id || '默认账户',
    loopState: value.loop_state,
    heartbeat: compactTime(value.heartbeat),
    activeSessions: toNumber(value.active_sessions),
    openOrders: toNumber(value.open_orders),
    realizedPnl: toNumber(value.realized_pnl),
    balance: typeof value.balance === 'number' ? value.balance : null,
    availableBalance: nullableNumber(value.available_balance),
    marginBalance: nullableNumber(value.margin_balance),
    initialMargin: nullableNumber(value.initial_margin),
    maintenanceMargin: nullableNumber(value.maintenance_margin),
    unrealizedPnl: nullableNumber(value.unrealized_pnl),
    currentExposure: nullableNumber(value.current_exposure),
    accountSummary: mapAccountSummary(value.account_summary),
    riskLevel: value.risk_level,
    latestSystemMessage: value.latest_system_message,
  }
}

function mapAccountSummary(value: ApiAccountSummary | undefined): AccountSummary {
  return {
    status: value?.status || 'unknown',
    error: value?.error || '',
    asset: value?.asset || 'USDT',
    balance: nullableNumber(value?.balance),
    availableBalance: nullableNumber(value?.available_balance),
    marginBalance: nullableNumber(value?.margin_balance),
    initialMargin: nullableNumber(value?.initial_margin),
    maintenanceMargin: nullableNumber(value?.maintenance_margin),
    unrealizedPnl: nullableNumber(value?.unrealized_pnl),
    currentExposure: nullableNumber(value?.current_exposure),
  }
}

export function mapLiquidityCandidate(value: ApiLiquidityCandidate): LiquidityCandidate {
  return {
    rank: Math.trunc(toNumber(value.rank)),
    symbol: value.symbol,
    score: nullableNumber(value.score),
    volumeScore: nullableNumber(value.volume_score),
    depthScore: nullableNumber(value.depth_score),
    volume24h: nullableNumber(value.volume_24h),
    depthUsdt: nullableNumber(value.depth_usdt),
    bidPrice: nullableNumber(value.bid_price),
    askPrice: nullableNumber(value.ask_price),
    spreadPct: nullableNumber(value.spread_pct),
    selected: Boolean(value.selected),
    disabled: Boolean(value.disabled),
    status: value.status || 'unknown',
    error: value.error || '',
    volatilityMethod: value.volatility_method || '',
    volatilityMethodLabel: value.volatility_method_label || value.volatility_method || '-',
    volatilityValue: nullableNumber(value.volatility_value),
    currentVolatility: nullableNumber(value.current_volatility),
    volatilityWindow: nullableNumber(value.volatility_window),
    currentVolatilityWindow: nullableNumber(value.current_volatility_window),
    stage: value.stage || '等待波动计算',
    snapshotAt: compactTime(value.snapshot_at || ''),
    price: nullableNumber(value.price),
    rangeLower: nullableNumber(value.range_lower),
    rangeUpper: nullableNumber(value.range_upper),
    rangeWidthPct: nullableNumber(value.range_width_pct),
    thresholdMet: Boolean(value.threshold_met),
    sessionId: nullableNumber(value.session_id),
    marketUpdatedAt: compactTime(value.market_updated_at || ''),
    lastKlineCloseAt: compactTime(value.last_kline_close_at || ''),
    dataStale: Boolean(value.data_stale),
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
    leverage: Math.trunc(toNumber(value.leverage)),
    capitalPerSymbol: toNumber(value.capital_per_symbol),
    maxConcurrent: Math.trunc(toNumber(value.max_concurrent)),
    scanCandidateCount: Math.trunc(toNumber(value.scan_candidate_count)),
    observeHours: toNumber(value.observe_hours),
    observeKlineInterval: value.observe_kline_interval || '1m',
    minStepPct: toNumber(value.min_step_pct),
    minTradableRangePct: toNumber(value.min_tradable_range_pct),
    maxGridNum: Math.trunc(toNumber(value.max_grid_num)),
    stopBufferPct: toNumber(value.stop_buffer_pct),
    safetyMultiplier: toNumber(value.safety_multiplier),
    takeProfitUsdt: toNumber(value.take_profit_usdt),
    totalCapitalLimit: toNumber(value.total_capital_limit),
    maxMakerFeeRate: toNumber(value.max_maker_fee_rate),
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
    windowId: Math.trunc(toNumber(value.window_id)),
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
    volatilityValue: toNumber(value.volatility_value),
    volatilityWindow: toNumber(value.volatility_window),
    currentVolatility: toNumber(value.current_volatility),
    currentVolatilityWindow: toNumber(value.current_volatility_window),
    currentVolatilityAt: compactTime(value.current_volatility_at),
    baselineAtr: toNumber(value.baseline_atr),
    stopLossPrice: toNumber(value.stop_loss_price),
    capital: toNumber(value.capital),
    leverage: toNumber(value.leverage),
    openTime: compactTime(value.open_time),
    closeTime: compactTime(value.close_time),
    closeReason: value.close_reason || '',
    volatilityStage: value.volatility_stage || 'pending',
    volatilityStageLabel: value.volatility_stage_label || '加载历史行情',
    volatilityProgressPct: nullableNumber(value.volatility_progress_pct),
    volatilityRemainingSeconds: nullableNumber(value.volatility_remaining_seconds),
    openOrderCount: toNumber(value.open_order_count),
    tradeCount: toNumber(value.trade_count),
    orders: [],
    trades: [],
    performance: emptyPerformance(),
    nextEntryDisabled: Boolean(value.next_entry_disabled),
    stopRequested: Boolean(value.stop_requested),
    stopRequestStatus: value.stop_request_status || '',
    stopRequestType: value.stop_request_type || '',
    controlRequested: Boolean(value.control_requested),
    controlRequestStatus: value.control_request_status || '',
    controlRequestAction: value.control_request_action || '',
    position: emptyPosition(),
  }
}

function mapGridRound(value: ApiGridRound, roundNumber: number): GridRound {
  return {
    id: Math.trunc(toNumber(value.window_id)),
    roundNumber,
    startTime: compactTime(value.window_start),
    endTime: compactTime(value.window_end),
    status: value.status,
    statusLabel: value.status_label || value.status,
    totalPnl: toNumber(value.total_pnl),
    sessionCount: Math.trunc(toNumber(value.session_count)),
    activeSessionCount: Math.trunc(toNumber(value.active_session_count)),
  }
}

function mapSessionDetail(value: ApiSessionDetail): GridSession {
  const session = mapSession(value.session)
  return {
    ...session,
    orders: Array.isArray(value.orders) ? value.orders.map(mapOrder) : [],
    trades: Array.isArray(value.trades) ? value.trades.map(mapTrade) : [],
    performance: mapPerformance(value.performance),
    position: mapPosition(value.position),
  }
}

function mapPosition(value: Record<string, unknown> | undefined) {
  const source = value || {}
  return {
    status: String(source.status || 'historical'),
    error: String(source.error || ''),
    symbol: String(source.symbol || ''),
    qty: nullableUnknownNumber(source.qty),
    longQty: nullableUnknownNumber(source.long_qty),
    shortQty: nullableUnknownNumber(source.short_qty),
    entryPrice: nullableUnknownNumber(source.entry_price),
    markPrice: nullableUnknownNumber(source.mark_price),
    unrealizedPnl: nullableUnknownNumber(source.unrealized_pnl),
    notional: nullableUnknownNumber(source.notional),
  }
}

function emptyPosition() {
  return mapPosition(undefined)
}

function mapOrder(value: ApiOrder): GridOrder {
  return {
    id: value.id,
    sessionId: value.session_id,
    symbol: value.symbol,
    orderId: value.order_id,
    gridIndex: Math.trunc(toNumber(value.grid_index)),
    side: value.side,
    sideLabel: value.side_label || value.side,
    price: toNumber(value.price),
    qty: toNumber(value.qty),
    status: value.status,
    statusLabel: value.status_label || value.status,
    createdAt: compactTime(value.created_at),
    filledAt: compactTime(value.filled_at),
    fillPrice: toNumber(value.fill_price),
  }
}

function mapTrade(value: ApiTrade): GridTrade {
  return {
    id: value.id,
    sessionId: value.session_id,
    symbol: value.symbol,
    orderId: value.order_id,
    gridIndex: Math.trunc(toNumber(value.grid_index)),
    side: value.side,
    sideLabel: value.side_label || value.side,
    price: toNumber(value.price),
    qty: toNumber(value.qty),
    quoteQty: toNumber(value.quote_qty),
    gridPnl: toNumber(value.grid_pnl),
    fee: toNumber(value.fee),
    fundingFee: toNumber(value.funding_fee),
    tradeTime: compactTime(value.trade_time),
  }
}

function mapPerformance(value: ApiGridPerformance | undefined): GridPerformance {
  if (!value) {
    return emptyPerformance()
  }
  return {
    grossGridPnl: toNumber(value.gross_grid_pnl),
    tradingFees: toNumber(value.trading_fees),
    fundingFee: toNumber(value.funding_fee),
    realizedPnl: toNumber(value.realized_pnl),
    unpairedPnl: toNumber(value.unpaired_pnl),
    initialMargin: toNumber(value.initial_margin),
    currentMargin: nullableNumber(value.current_margin),
    marginChange: nullableNumber(value.margin_change),
    roi: nullableNumber(value.roi),
    annualizedRoi: nullableNumber(value.annualized_roi),
    durationHours: nullableNumber(value.duration_hours),
    tradeCount: Math.trunc(toNumber(value.trade_count)),
    unpairedTradeCount: Math.trunc(toNumber(value.unpaired_trade_count)),
    pnlCurve: Array.isArray(value.pnl_curve)
      ? value.pnl_curve.map((point) => ({ time: compactTime(point.time), value: toNumber(point.value) }))
      : [],
  }
}

function emptyPerformance(): GridPerformance {
  return {
    grossGridPnl: 0,
    tradingFees: 0,
    fundingFee: 0,
    realizedPnl: 0,
    unpairedPnl: 0,
    initialMargin: 0,
    currentMargin: null,
    marginChange: null,
    roi: null,
    annualizedRoi: null,
    durationHours: null,
    tradeCount: 0,
    unpairedTradeCount: 0,
    pnlCurve: [],
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

function nullableNumber(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function nullableUnknownNumber(value: unknown): number | null {
  const number = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : Number.NaN
  return Number.isFinite(number) ? number : null
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
