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
  verdict?: string
  grid_score: number
  threshold_used?: number | null
  allowed: number | boolean
  reasons?: string[]
  hard_blocks?: string[]
  component_scores?: Record<string, number | null>
  cost_breakdown?: Record<string, number>
  effective_weights?: Record<string, number>
  score_contributions?: Record<string, number>
  event_source_available?: number | boolean
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
  window_stop_count: number
  active_sessions: number
  open_orders: number
  global_risk_level: string
  data_health: string
  latest_regime: ApiV2RegimeDecision | null
  latest_inventory: ApiV2InventorySnapshot | null
  latest_risk: ApiV2RiskSnapshot | null
  risk_policy?: V2RiskPolicy
}

export type V2RiskPolicy = {
  effective_leverage_cap: number
  max_session_loss_pct: number
  max_weekend_loss_pct: number
  max_symbol_inventory_pct: number
  max_group_notional_pct: number
  max_consecutive_session_losses: number
  max_window_stop_count: number
  block_risk_increase_hot_reload: boolean
}

export type V2RegimeDecision = {
  symbol: string
  state: string
  verdict: string
  gridScore: number
  thresholdUsed: number | null
  allowed: boolean
  reasons: string[]
  hardBlocks: string[]
  componentScores: Record<string, number | null>
  costBreakdown: Record<string, number>
  effectiveWeights: Record<string, number>
  scoreContributions: Record<string, number>
  eventSourceAvailable: boolean
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
  windowStopCount: number
  activeSessions: number
  openOrders: number
  globalRiskLevel: string
  dataHealth: string
  latestRegime: V2RegimeDecision | null
  latestInventory: V2InventorySnapshot | null
  latestRisk: V2RiskSnapshot | null
  riskPolicy: V2RiskPolicy
}

export type V2ActiveConfig = {
  environment: string
  accountId: string
  version: string
  sections: Record<string, Record<string, unknown>>
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

export type V2InventoryLot = {
  id: number
  side: string
  entryPrice: number
  qty: number
  entryGridIndex: number | null
  targetExitPrice: number | null
  openedAt: string
  status: string
}

export type V2GridPlan = {
  symbol: string
  asOfTime: string
  center: number
  lower: number
  upper: number
  stepPct: number
  gridNum: number
  prices: number[]
  qtyWeights: number[]
  costFloorPct: number
  regimeScore: number | null
  parameterVersion: string
  directionMode: 'LONG' | 'SHORT' | 'NEUTRAL'
}

export type V2SessionWorkspace = {
  gridPlan: V2GridPlan | null
  inventory: V2InventorySnapshot | null
  inventoryLots: V2InventoryLot[]
  inventoryHistory: V2InventorySnapshot[]
  risk: V2RiskSnapshot | null
  events: V2SessionEvent[]
  orders: GridOrder[]
  trades: GridTrade[]
}

export type V2OrderDifference = {
  type: string
  severity: string
  clientId: string
  orderId: string
  message: string
  local: Record<string, unknown> | null
  exchange: Record<string, unknown> | null
}

export type V2OrderReconciliation = {
  status: string
  error: string
  checkedAt: string
  symbol: string
  localOrders: GridOrder[]
  exchangeOrders: Array<Record<string, unknown>>
  differences: V2OrderDifference[]
  consistent: boolean
}

export type V2BacktestDataset = {
  datasetId: string | null
  sourceType: 'FROZEN_DATASET' | 'LEGACY_CSV'
  name: string
  relativePath: string
  sizeBytes: number
  modifiedAt: string
  provider: string
  market: string
  symbol: string
  interval: string
  priceType: string
  requestedStart: string
  requestedEnd: string
  actualStart: string
  actualEnd: string
  rowCount: number
  checksum: string
  schemaVersion: number
  qualityStatus: string
  qualityReport: V2DatasetQualityReport
  windowMode: string
  windowCount: number | null
  status: string
  error: string
  createdAt: string
  updatedAt: string
}

export type V2DatasetQualityReport = {
  status: string
  inputRows: number
  outputRows: number
  duplicateRows: number
  conflictingDuplicates: number
  missingIntervals: number
  missingRatio: number
  maxConsecutiveMissing: number
  unclosedRows: number
  firstOpenTime: number | null
  lastOpenTime: number | null
  warnings: string[]
  errors: string[]
}

export type V2BacktestDataProvider = {
  id: string
  label: string
  market: string
  intervals: string[]
  priceTypes: string[]
}

export type V2BacktestSymbol = {
  symbol: string
  status: string
  market: string
  baseAsset: string
  quoteAsset: string
}

export type V2DatasetRequest = {
  provider: string
  symbol: string
  interval: string
  priceType: string
  startTime: string
  endTime: string
  windowMode: 'NYSE_CLOSED_ONLY' | 'RAW_RANGE'
}

export type V2DatasetPreview = {
  provider: string
  symbol: string
  interval: string
  startTime: string
  endTime: string
  estimatedRows: number
  estimatedPages: number
  estimatedSizeBytes: number
  cacheHit: boolean
  windowCount: number | null
  warnings: string[]
}

export type V2DatasetJob = {
  jobId: string
  datasetId: string | null
  provider: string
  symbol: string
  interval: string
  requestedStart: string
  requestedEnd: string
  windowMode: string
  status: string
  stage: string
  progress: number
  currentPage: number
  totalPages: number
  downloadedRows: number
  cancelRequested: boolean
  error: string
  createdAt: string
  startedAt: string
  completedAt: string
  updatedAt: string
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
  datasetId: string
  datasetChecksum: string
  dataProvider: string
  windowMode: string
  windowCount: number | null
  config: Record<string, unknown>
  metrics: Record<string, number | string | null>
}

export type V2BacktestDetail = V2BacktestRun & {
  report: {
    summary?: Record<string, number | string | null>
    grid_params?: Record<string, unknown>
    windows?: Array<{
      window_id?: string
      market_close?: string
      force_close_at?: string
      row_count?: number
      observation_rows?: number
      tradable_rows?: number
      status?: string
      skip_reason?: string | null
      warning?: string | null
      error?: string | null
      total_pnl?: number
      max_drawdown?: number
      fills?: number
      stopped_reason?: string | null
    }>
    fills?: Array<Record<string, unknown>>
    equity_curve?: Array<{
      bar_index?: number
      timestamp?: string
      equity?: number
      drawdown?: number
      realized_pnl?: number
      unrealized_pnl?: number
      close?: number
      gross_inventory_notional?: number
      inventory_utilization?: number
    }>
    validation?: {
      sample_label?: string
      parameters_frozen?: boolean
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
      cost_sensitivity?: {
        status?: string
        scenario_count?: number
        worst_total_pnl?: number
        error?: string
        scenarios?: Array<{
          key?: string
          label?: string
          total_pnl?: number
          max_drawdown?: number
          fills?: number
          max_inventory_utilization?: number
          stopped_reason?: string | null
          pnl_delta_vs_baseline?: number
        }>
      }
      window_distribution?: {
        status?: string
        source?: 'FIXED_ROWS' | 'NYSE_WINDOWS'
        window_rows?: number
        window_count?: number
        total_window_count?: number
        skipped_count?: number
        failed_count?: number
        positive_ratio?: number
        p05?: number
        p50?: number
        p95?: number
        worst?: number
        best?: number
        values?: number[]
      }
      window_analysis?: {
        status?: string
        source?: 'RAW_RANGE' | 'NYSE_WINDOWS'
        total_count?: number
        completed_count?: number
        skipped_count?: number
        failed_count?: number
        reason_counts?: Record<string, number>
        windows?: Array<{
          window_id?: string
          market_close?: string
          force_close_at?: string
          row_count?: number
          observation_rows?: number
          tradable_rows?: number
          status?: string
          skip_reason?: string | null
          reason?: string | null
          total_pnl?: number | null
          max_drawdown?: number | null
          fills?: number | null
          stopped_reason?: string | null
        }>
      }
      regime_diagnostics?: {
        status?: string
        reason?: string
      }
    }
    metadata?: {
      dataset?: string
      dataset_id?: string
      dataset_checksum?: string
      data_provider?: string
      window_mode?: string
      window_count?: number | null
      dataset_schema_version?: number | null
      sample_label?: string
      parameters_frozen?: boolean
      data_start?: string | null
      data_end?: string | null
      row_count?: number
      observe_rows?: number
      execution_rows?: number
      fill_model?: string
      code_commit?: string
      run_config?: Record<string, unknown>
    }
  } | null
}

export type V2BacktestRequest = {
  dataset?: string
  datasetId?: string
  symbol: string
  observeRows: number
  capital: number
  leverage: number
  makerFeeRate: number
  fillModel: string
  makerFillProbability: number
  maxFillsPerBar: number
  takerFeeRate: number
  stopSlippageBps: number
  fundingRatePerBar: number
  walkForwardTestRows: number
  monteCarloSimulations: number
  monteCarloMissingFillProbability: number
  monteCarloLossMultiplier: number
  distributionWindowRows: number
  sampleLabel: string
  parametersFrozen: boolean
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
  kline_required_count?: number | null
  kline_actual_count?: number | null
  kline_age_seconds?: number | null
  kline_missing_count?: number | null
  kline_quality_status?: string
  regime_score?: number | null
  regime_allowed?: number | boolean | null
  block_code?: string
  market_state?: string
  verdict?: string
  soft_breach_count?: number
  grid_preview?: Record<string, unknown>
  grid_preview_json?: Record<string, unknown> | string
  economics?: Record<string, unknown>
  economics_json?: Record<string, unknown> | string
  maker_fee_rate?: number | null
  maker_fee_source?: string
  maker_fee_checked_at?: string
}

type ApiSession = {
  id: number
  window_id: number
  symbol: string
  state: string
  state_label: string
  soft_breach_count?: number
  last_retention_decision_at?: string
  direction_mode?: 'LONG' | 'SHORT' | 'NEUTRAL'
  direction_source?: string
  seed_position_side?: string | null
  seed_qty?: number | null
  seed_entry_price?: number | null
  seed_slippage_pct?: number | null
  seed_fee?: number | null
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
  position_side?: string | null
  order_intent?: string | null
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
  process_state?: string
  alive?: boolean
  pid?: number | null
  runtime_id?: string
  runtime_state?: string
  started_at?: string
  heartbeat_at?: string
  heartbeat_age_seconds?: number | null
  uptime_seconds?: number | null
  last_status?: string
  last_error?: string
  process_control_available?: boolean
  process_control_mode?: string
}

type ApiStrategySettings = {
  direction_mode: 'LONG' | 'SHORT' | 'NEUTRAL'
  direction_overrides: Record<string, 'LONG' | 'SHORT' | 'NEUTRAL'>
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
    direction_modes: VolatilityOption[]
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
  | 'trader-loop-start'
  | 'trader-loop-stop'
  | 'trader-loop-restart'
  | 'auto-trading-start'
  | 'auto-trading-stop'

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
    windowStopCount: value.window_stop_count,
    activeSessions: value.active_sessions,
    openOrders: value.open_orders,
    globalRiskLevel: value.global_risk_level,
    dataHealth: value.data_health,
    latestRegime: value.latest_regime ? mapV2Regime(value.latest_regime) : null,
    latestInventory: value.latest_inventory ? mapV2Inventory(value.latest_inventory) : null,
    latestRisk: value.latest_risk ? mapV2Risk(value.latest_risk) : null,
    riskPolicy: value.risk_policy || {
      effective_leverage_cap: 1,
      max_session_loss_pct: 0,
      max_weekend_loss_pct: 0,
      max_symbol_inventory_pct: 0,
      max_group_notional_pct: 0,
      max_consecutive_session_losses: 0,
      max_window_stop_count: 0,
      block_risk_increase_hot_reload: true,
    },
  }
}

export async function loadV2ActiveConfig(accountId?: string): Promise<V2ActiveConfig> {
  const value = await fetchJson<{
    environment: string
    account_id: string
    version: string
    sections: Record<string, Record<string, unknown>>
  }>(accountUrl('/api/v2/config/active', accountId))
  return {
    environment: value.environment,
    accountId: value.account_id,
    version: value.version,
    sections: value.sections || {},
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

export async function loadV2RegimeHistory(
  symbol: string,
  accountId?: string,
): Promise<V2RegimeDecision[]> {
  const value = await fetchJson<{ items: ApiV2RegimeDecision[] }>(
    accountUrl(`/api/v2/regime/${encodeURIComponent(symbol)}/history?limit=1440`, accountId),
  )
  return value.items.map(mapV2Regime)
}

export async function loadV2SessionWorkspace(
  sessionId: number,
  accountId?: string,
): Promise<V2SessionWorkspace> {
  const value = await fetchJson<{
    grid_plan: Record<string, unknown> | null
    inventory: ApiV2InventorySnapshot | null
    inventory_lots: Array<Record<string, unknown>>
    inventory_history: ApiV2InventorySnapshot[]
    risk: ApiV2RiskSnapshot | null
    events: Array<{
      id: number
      event_type: string
      aggregate_type: string
      aggregate_id: string
      event_time: string
      available_time: string
      payload: Record<string, unknown>
    }>
    orders: ApiOrder[]
    trades: ApiTrade[]
  }>(accountUrl(`/api/v2/sessions/${sessionId}/workspace`, accountId))
  return {
    gridPlan: value.grid_plan ? mapV2GridPlan(value.grid_plan) : null,
    inventory: value.inventory ? mapV2Inventory(value.inventory) : null,
    inventoryLots: value.inventory_lots.map(mapV2InventoryLot),
    inventoryHistory: value.inventory_history.map(mapV2Inventory),
    risk: value.risk ? mapV2Risk(value.risk) : null,
    events: value.events.map((item) => ({
      eventId: item.id,
      eventType: item.event_type,
      aggregateType: item.aggregate_type,
      aggregateId: item.aggregate_id,
      eventTime: item.event_time,
      availableTime: item.available_time,
      payload: item.payload || {},
    })),
    orders: value.orders.map(mapOrder),
    trades: value.trades.map(mapTrade),
  }
}

export async function loadV2OrderReconciliation(
  sessionId: number,
  accountId?: string,
): Promise<V2OrderReconciliation> {
  const value = await fetchJson<{
    status: string
    error: string
    checked_at: string
    symbol: string
    local_orders: ApiOrder[]
    exchange_orders: Array<Record<string, unknown>>
    differences: Array<{
      type: string
      severity: string
      client_id: string
      order_id: string
      message: string
      local: Record<string, unknown> | null
      exchange: Record<string, unknown> | null
    }>
    consistent: boolean
  }>(accountUrl(`/api/v2/sessions/${sessionId}/order-reconciliation`, accountId))
  return {
    status: value.status,
    error: value.error,
    checkedAt: value.checked_at,
    symbol: value.symbol,
    localOrders: value.local_orders.map(mapOrder),
    exchangeOrders: value.exchange_orders,
    differences: value.differences.map((item) => ({
      type: item.type,
      severity: item.severity,
      clientId: item.client_id,
      orderId: item.order_id,
      message: item.message,
      local: item.local,
      exchange: item.exchange,
    })),
    consistent: value.consistent,
  }
}

export async function loadV2BacktestDatasets(accountId?: string): Promise<V2BacktestDataset[]> {
  const value = await fetchJson<{ items: Array<Record<string, unknown>> }>(
    accountUrl('/api/v2/backtests/datasets', accountId),
  )
  return value.items.map(mapV2BacktestDataset)
}

export async function loadV2BacktestDataProviders(
  accountId?: string,
): Promise<V2BacktestDataProvider[]> {
  const value = await fetchJson<{ items: Array<Record<string, unknown>> }>(
    accountUrl('/api/v2/backtest-data/providers', accountId),
  )
  return value.items.map((item) => ({
    id: String(item.id || ''),
    label: String(item.label || ''),
    market: String(item.market || ''),
    intervals: Array.isArray(item.intervals) ? item.intervals.map(String) : [],
    priceTypes: Array.isArray(item.price_types) ? item.price_types.map(String) : [],
  }))
}

export async function searchV2BacktestSymbols(
  query = '',
  accountId?: string,
): Promise<V2BacktestSymbol[]> {
  const path = `/api/v2/backtest-data/providers/binance/symbols?query=${encodeURIComponent(query)}&market=usds_m`
  const value = await fetchJson<{ items: Array<Record<string, unknown>> }>(
    accountUrl(path, accountId),
  )
  return value.items.map((item) => ({
    symbol: String(item.symbol || ''),
    status: String(item.status || ''),
    market: String(item.market || ''),
    baseAsset: String(item.base_asset || ''),
    quoteAsset: String(item.quote_asset || ''),
  }))
}

export async function previewV2BacktestDataset(
  request: V2DatasetRequest,
  accountId?: string,
): Promise<V2DatasetPreview> {
  const value = await requestJson<Record<string, unknown>>(
    accountUrl('/api/v2/backtest-data/preview', accountId),
    { method: 'POST', body: JSON.stringify(datasetRequestBody(request)) },
  )
  return mapV2DatasetPreview(value)
}

export async function createV2BacktestDatasetJob(
  request: V2DatasetRequest,
  accountId?: string,
): Promise<V2DatasetJob> {
  const value = await requestJson<Record<string, unknown>>(
    accountUrl('/api/v2/backtest-data/jobs', accountId),
    { method: 'POST', body: JSON.stringify(datasetRequestBody(request)) },
  )
  return mapV2DatasetJob(value)
}

export async function uploadV2BacktestDataset(
  file: File,
  options: {
    symbol: string
    interval: string
    windowMode: 'NYSE_CLOSED_ONLY' | 'RAW_RANGE'
  },
  accountId?: string,
): Promise<V2BacktestDataset> {
  const query = new URLSearchParams({
    file_name: file.name,
    symbol: options.symbol,
    interval: options.interval,
    window_mode: options.windowMode,
  })
  const response = await fetch(
    accountUrl(`/api/v2/backtest-data/upload?${query.toString()}`, accountId),
    {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'text/csv' },
      body: file,
    },
  )
  const body = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) {
    throw new Error(body.detail ? String(body.detail) : `请求失败：${response.status}`)
  }
  return mapV2BacktestDataset(body)
}

export async function loadV2BacktestDatasetJob(
  jobId: string,
  accountId?: string,
): Promise<V2DatasetJob> {
  const value = await fetchJson<Record<string, unknown>>(
    accountUrl(`/api/v2/backtest-data/jobs/${encodeURIComponent(jobId)}`, accountId),
  )
  return mapV2DatasetJob(value)
}

export async function cancelV2BacktestDatasetJob(
  jobId: string,
  accountId?: string,
): Promise<void> {
  await requestJson(
    accountUrl(`/api/v2/backtest-data/jobs/${encodeURIComponent(jobId)}/cancel`, accountId),
    { method: 'POST' },
  )
}

export async function loadV2BacktestDatasetDetail(
  datasetId: string,
  accountId?: string,
): Promise<V2BacktestDataset> {
  const value = await fetchJson<Record<string, unknown>>(
    accountUrl(`/api/v2/backtests/datasets/${encodeURIComponent(datasetId)}`, accountId),
  )
  return mapV2BacktestDataset(value)
}

export async function deleteV2BacktestDataset(
  datasetId: string,
  accountId?: string,
): Promise<void> {
  await requestJson(
    accountUrl(`/api/v2/backtests/datasets/${encodeURIComponent(datasetId)}`, accountId),
    { method: 'DELETE' },
  )
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
      ...(request.datasetId ? { dataset_id: request.datasetId } : { dataset: request.dataset }),
      symbol: request.symbol,
      observe_rows: request.observeRows,
      capital: request.capital,
      leverage: request.leverage,
      maker_fee_rate: request.makerFeeRate,
      fill_model: request.fillModel,
      maker_fill_probability: request.makerFillProbability,
      max_fills_per_bar: request.maxFillsPerBar,
      taker_fee_rate: request.takerFeeRate,
      stop_slippage_bps: request.stopSlippageBps,
      funding_rate_per_bar: request.fundingRatePerBar,
      walk_forward_test_rows: request.walkForwardTestRows,
      monte_carlo_simulations: request.monteCarloSimulations,
      monte_carlo_missing_fill_probability: request.monteCarloMissingFillProbability,
      monte_carlo_loss_multiplier: request.monteCarloLossMultiplier,
      distribution_window_rows: request.distributionWindowRows,
      sample_label: request.sampleLabel,
      parameters_frozen: request.parametersFrozen,
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
    verdict: value.verdict || (Boolean(value.allowed) ? 'ALLOWED' : 'BLOCKED_SCORE'),
    gridScore: Number(value.grid_score || 0),
    thresholdUsed: value.threshold_used == null ? null : Number(value.threshold_used),
    allowed: Boolean(value.allowed),
    reasons: Array.isArray(value.reasons) ? value.reasons : [],
    hardBlocks: Array.isArray(value.hard_blocks) ? value.hard_blocks : [],
    componentScores: value.component_scores || {},
    costBreakdown: value.cost_breakdown || {},
    effectiveWeights: value.effective_weights || {},
    scoreContributions: value.score_contributions || {},
    eventSourceAvailable: Boolean(value.event_source_available),
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

function mapV2GridPlan(value: Record<string, unknown>): V2GridPlan {
  return {
    symbol: String(value.symbol || ''),
    asOfTime: String(value.as_of_time || ''),
    center: unknownNumber(value.center),
    lower: unknownNumber(value.lower_price),
    upper: unknownNumber(value.upper_price),
    stepPct: unknownNumber(value.step_pct),
    gridNum: Math.trunc(unknownNumber(value.grid_num)),
    prices: Array.isArray(value.prices)
      ? value.prices.map((item) => unknownNumber(item))
      : [],
    qtyWeights: Array.isArray(value.qty_weights)
      ? value.qty_weights.map((item) => unknownNumber(item))
      : [],
    costFloorPct: unknownNumber(value.cost_floor_pct),
    regimeScore: nullableUnknownNumber(value.regime_score),
    parameterVersion: String(value.parameter_version || ''),
    directionMode: (String(value.direction_mode || 'NEUTRAL').toUpperCase() as 'LONG' | 'SHORT' | 'NEUTRAL'),
  }
}

function mapV2InventoryLot(value: Record<string, unknown>): V2InventoryLot {
  return {
    id: Math.trunc(unknownNumber(value.id)),
    side: String(value.side || ''),
    entryPrice: unknownNumber(value.entry_price),
    qty: unknownNumber(value.qty),
    entryGridIndex: value.entry_grid_index == null
      ? null
      : Math.trunc(unknownNumber(value.entry_grid_index)),
    targetExitPrice: nullableUnknownNumber(value.target_exit_price),
    openedAt: String(value.opened_at || ''),
    status: String(value.status || ''),
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
    datasetId: String(value.dataset_id || ''),
    datasetChecksum: String(value.dataset_checksum || ''),
    dataProvider: String(value.data_provider || ''),
    windowMode: String(value.window_mode || ''),
    windowCount: value.window_count == null ? null : Math.trunc(unknownNumber(value.window_count)),
    config,
    metrics,
  }
}

function mapV2BacktestDataset(value: Record<string, unknown>): V2BacktestDataset {
  const quality = value.quality_report && typeof value.quality_report === 'object'
    ? value.quality_report as Record<string, unknown>
    : {}
  const sourceType = String(value.source_type || 'LEGACY_CSV') === 'FROZEN_DATASET'
    ? 'FROZEN_DATASET'
    : 'LEGACY_CSV'
  const relativePath = String(value.relative_path || value.file_path || '')
  const symbol = String(value.symbol || '')
  const interval = String(value.interval || '')
  return {
    datasetId: value.dataset_id ? String(value.dataset_id) : null,
    sourceType,
    name: String(value.name || (symbol && interval ? `${symbol} · ${interval}` : relativePath)),
    relativePath,
    sizeBytes: unknownNumber(value.size_bytes),
    modifiedAt: String(value.modified_at || value.updated_at || value.created_at || ''),
    provider: String(value.provider || (sourceType === 'LEGACY_CSV' ? 'local' : '')),
    market: String(value.market || ''),
    symbol,
    interval,
    priceType: String(value.price_type || ''),
    requestedStart: String(value.requested_start || ''),
    requestedEnd: String(value.requested_end || ''),
    actualStart: String(value.actual_start || ''),
    actualEnd: String(value.actual_end || ''),
    rowCount: Math.trunc(unknownNumber(value.row_count)),
    checksum: String(value.checksum || ''),
    schemaVersion: Math.trunc(unknownNumber(value.schema_version)),
    qualityStatus: String(value.quality_status || (sourceType === 'LEGACY_CSV' ? 'LEGACY' : '')),
    qualityReport: {
      status: String(quality.status || value.quality_status || ''),
      inputRows: Math.trunc(unknownNumber(quality.input_rows)),
      outputRows: Math.trunc(unknownNumber(quality.output_rows)),
      duplicateRows: Math.trunc(unknownNumber(quality.duplicate_rows)),
      conflictingDuplicates: Math.trunc(unknownNumber(quality.conflicting_duplicates)),
      missingIntervals: Math.trunc(unknownNumber(quality.missing_intervals)),
      missingRatio: unknownNumber(quality.missing_ratio),
      maxConsecutiveMissing: Math.trunc(unknownNumber(quality.max_consecutive_missing)),
      unclosedRows: Math.trunc(unknownNumber(quality.unclosed_rows)),
      firstOpenTime: quality.first_open_time == null ? null : unknownNumber(quality.first_open_time),
      lastOpenTime: quality.last_open_time == null ? null : unknownNumber(quality.last_open_time),
      warnings: Array.isArray(quality.warnings) ? quality.warnings.map(String) : [],
      errors: Array.isArray(quality.errors) ? quality.errors.map(String) : [],
    },
    windowMode: String(value.window_mode || ''),
    windowCount: value.window_count == null ? null : Math.trunc(unknownNumber(value.window_count)),
    status: String(value.status || (sourceType === 'LEGACY_CSV' ? 'READY' : '')),
    error: String(value.error || ''),
    createdAt: String(value.created_at || ''),
    updatedAt: String(value.updated_at || ''),
  }
}

function datasetRequestBody(request: V2DatasetRequest): Record<string, unknown> {
  return {
    provider: request.provider,
    symbol: request.symbol,
    interval: request.interval,
    price_type: request.priceType,
    start_time: request.startTime,
    end_time: request.endTime,
    window_mode: request.windowMode,
  }
}

function mapV2DatasetPreview(value: Record<string, unknown>): V2DatasetPreview {
  return {
    provider: String(value.provider || ''),
    symbol: String(value.symbol || ''),
    interval: String(value.interval || ''),
    startTime: String(value.start_time || ''),
    endTime: String(value.end_time || ''),
    estimatedRows: Math.trunc(unknownNumber(value.estimated_rows)),
    estimatedPages: Math.trunc(unknownNumber(value.estimated_pages)),
    estimatedSizeBytes: unknownNumber(value.estimated_size_bytes),
    cacheHit: Boolean(value.cache_hit),
    windowCount: value.window_count == null ? null : Math.trunc(unknownNumber(value.window_count)),
    warnings: Array.isArray(value.warnings) ? value.warnings.map(String) : [],
  }
}

function mapV2DatasetJob(value: Record<string, unknown>): V2DatasetJob {
  return {
    jobId: String(value.job_id || ''),
    datasetId: value.dataset_id ? String(value.dataset_id) : null,
    provider: String(value.provider || ''),
    symbol: String(value.symbol || ''),
    interval: String(value.interval || ''),
    requestedStart: String(value.requested_start || ''),
    requestedEnd: String(value.requested_end || ''),
    windowMode: String(value.window_mode || ''),
    status: String(value.status || 'UNKNOWN'),
    stage: String(value.stage || ''),
    progress: unknownNumber(value.progress),
    currentPage: Math.trunc(unknownNumber(value.current_page)),
    totalPages: Math.trunc(unknownNumber(value.total_pages)),
    downloadedRows: Math.trunc(unknownNumber(value.downloaded_rows)),
    cancelRequested: Boolean(value.cancel_requested),
    error: String(value.error || ''),
    createdAt: String(value.created_at || ''),
    startedAt: String(value.started_at || ''),
    completedAt: String(value.completed_at || ''),
    updatedAt: String(value.updated_at || ''),
  }
}

export function consoleEventsUrl(accountId?: string): string {
  return accountUrl('/api/events', accountId)
}

export type AutoTradingUiState = {
  enabled: boolean
  transitioning: boolean
  transitionState: string
  canStart: boolean
  canStop: boolean
  blockedReason: string
  mode?: string
  requestId?: string
}

export type CurrentRoundSnapshot = {
  trader: TraderProcessState
  autoTrading: AutoTradingUiState
  window: {
    kind: string
    allowed: boolean
    windowKey: string
    forceCloseAt: string
    minutesToForceClose: number | null
    testnetForceWindow: boolean
    reason?: string
  }
  round: {
    state: string
    roundId: number | null
    lastScanAt: string
    nextScanAt: string
    runtimeId: string
    startRequest: Record<string, unknown> | null
  }
  candidates: LiquidityCandidate[]
  streamHealth: {
    updatedAt: string
    streams: Record<string, {
      state: string
      reconnectCount: number
      lastMessageAt: string
      lastError: string
      updatedAt: string
    }>
  }
  recentEvents: Array<{ time: string; level: string; module: string; message: string }>
  risk: { newEntriesPaused: boolean }
}

export async function getCurrentRound(accountId?: string): Promise<CurrentRoundSnapshot> {
  const value = await fetchJson<{
    trader: ApiTraderProcessState
    auto_trading?: Record<string, unknown>
    window?: Record<string, unknown>
    round?: Record<string, unknown>
    candidates?: ApiLiquidityCandidate[]
    recent_events?: Array<Record<string, unknown>>
    risk?: Record<string, unknown>
    stream_health?: Record<string, unknown>
  }>(accountUrl('/api/v2/current-round', accountId))
  return {
    trader: mapTraderProcessState(value.trader || {
      available: false,
      mode: 'unavailable',
      service: 'quietgrid-trader',
      state: 'unknown',
      detail: '',
    }),
    autoTrading: {
      enabled: Boolean(value.auto_trading?.enabled),
      transitioning: false,
      transitionState: String(
        value.auto_trading?.transition_state
        || (value.auto_trading?.enabled ? 'ENABLED' : 'DISABLED'),
      ),
      canStart: Boolean(value.auto_trading?.can_start ?? !value.auto_trading?.enabled),
      canStop: Boolean(value.auto_trading?.can_stop ?? value.auto_trading?.enabled),
      blockedReason: String(value.auto_trading?.blocked_reason || ''),
      mode: String(value.auto_trading?.mode || ''),
      requestId: String(value.auto_trading?.request_id || ''),
    },
    window: {
      kind: String(value.window?.kind || ''),
      allowed: Boolean(value.window?.allowed),
      windowKey: String(value.window?.window_key || ''),
      forceCloseAt: String(value.window?.force_close_at || ''),
      minutesToForceClose:
        value.window?.minutes_to_force_close == null
          ? null
          : Number(value.window.minutes_to_force_close),
      testnetForceWindow: Boolean(value.window?.testnet_force_window),
      reason: String(value.window?.reason || ''),
    },
    round: {
      state: String(value.round?.state || 'IDLE'),
      roundId: value.round?.round_id == null ? null : Number(value.round.round_id),
      lastScanAt: String(value.round?.last_scan_at || ''),
      nextScanAt: String(value.round?.next_scan_at || ''),
      runtimeId: String(value.round?.runtime_id || ''),
      startRequest: (value.round?.start_request as Record<string, unknown> | null) || null,
    },
    candidates: (value.candidates || []).map(mapLiquidityCandidate),
    streamHealth: mapStreamHealth(value.stream_health),
    recentEvents: (value.recent_events || []).map((row) => ({
      time: String(row.time || ''),
      level: String(row.level || ''),
      module: String(row.module || ''),
      message: String(row.message || ''),
    })),
    risk: {
      newEntriesPaused: Boolean(value.risk?.new_entries_paused),
    },
  }
}

function mapStreamHealth(value: Record<string, unknown> | undefined): CurrentRoundSnapshot['streamHealth'] {
  const rawStreams = (
    value?.streams && typeof value.streams === 'object'
      ? value.streams
      : {}
  ) as Record<string, unknown>
  const streams: CurrentRoundSnapshot['streamHealth']['streams'] = {}
  Object.entries(rawStreams).forEach(([name, raw]) => {
    const item = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
    streams[name] = {
      state: String(item.state || 'UNKNOWN'),
      reconnectCount: Math.trunc(unknownNumber(item.reconnect_count)),
      lastMessageAt: String(item.last_message_at || ''),
      lastError: String(item.last_error || ''),
      updatedAt: String(item.updated_at || ''),
    }
  })
  return {
    updatedAt: String(value?.updated_at || ''),
    streams,
  }
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
      direction_mode: draft.directionMode,
      direction_overrides: draft.directionOverrides,
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
  const body = (await response.json()) as ConsoleActionResult | { detail?: string | Record<string, unknown> }
  if (!response.ok) {
    throw new Error(formatApiDetail((body as { detail?: string | Record<string, unknown> }).detail, response.status))
  }
  return body as ConsoleActionResult
}

function formatApiDetail(detail: string | Record<string, unknown> | undefined, status: number): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object') {
    const message = detail.message || detail.detail || detail.code
    const code = detail.code ? `[${String(detail.code)}] ` : ''
    if (message) return `${code}${String(message)}`
    try {
      return JSON.stringify(detail)
    } catch {
      return `请求失败：${status}`
    }
  }
  return `请求失败：${status}`
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
  if (action === 'trader-loop-start') {
    return '/api/actions/trader-loop/start'
  }
  if (action === 'trader-loop-stop') {
    return '/api/actions/trader-loop/stop'
  }
  if (action === 'trader-loop-restart') {
    return '/api/actions/trader-loop/restart'
  }
  if (action === 'auto-trading-start') {
    return '/api/actions/auto-trading/start'
  }
  if (action === 'auto-trading-stop') {
    return '/api/actions/auto-trading/stop'
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

async function requestJson<T = Record<string, unknown>>(
  url: string,
  init: RequestInit,
): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers || {}),
    },
  })
  const body = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) {
    throw new Error(body.detail ? String(body.detail) : `请求失败：${response.status}`)
  }
  return body as T
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
    processState: value.process_state || 'OFFLINE',
    alive: Boolean(value.alive),
    pid: value.pid ?? null,
    runtimeId: value.runtime_id || '',
    runtimeState: value.runtime_state || '',
    startedAt: value.started_at || '',
    heartbeatAt: value.heartbeat_at || '',
    heartbeatAgeSeconds: value.heartbeat_age_seconds ?? null,
    uptimeSeconds: value.uptime_seconds ?? null,
    lastStatus: value.last_status || '',
    lastError: value.last_error || '',
    processControlAvailable: value.process_control_available ?? Boolean(value.available),
    processControlMode: value.process_control_mode || value.mode || 'unavailable',
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
  const economics = (
    value.economics && typeof value.economics === 'object'
      ? value.economics
      : value.economics_json && typeof value.economics_json === 'object'
        ? value.economics_json
        : {}
  ) as Record<string, unknown>
  const gridPreview = (
    value.grid_preview && typeof value.grid_preview === 'object'
      ? value.grid_preview
      : value.grid_preview_json && typeof value.grid_preview_json === 'object'
        ? value.grid_preview_json
        : {}
  ) as Record<string, unknown>
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
    klineRequiredCount: nullableNumber(value.kline_required_count),
    klineActualCount: nullableNumber(value.kline_actual_count),
    klineAgeSeconds: nullableNumber(value.kline_age_seconds),
    klineMissingCount: nullableNumber(value.kline_missing_count),
    klineQualityStatus: value.kline_quality_status || '',
    regimeScore: nullableNumber(value.regime_score),
    regimeAllowed:
      value.regime_allowed == null ? null : Boolean(value.regime_allowed),
    blockCode: value.block_code || '',
    marketState: value.market_state || '',
    verdict: value.verdict || value.block_code || '',
    softBreachCount: Math.trunc(toNumber(value.soft_breach_count)),
    gridPreview: {
      lower: nullableUnknownNumber(gridPreview.lower),
      upper: nullableUnknownNumber(gridPreview.upper),
      gridCount: nullableUnknownNumber(gridPreview.grid_count),
      levelCount: nullableUnknownNumber(gridPreview.level_count),
    },
    economics: {
      makerFeeRate: nullableUnknownNumber(economics.maker_fee_rate ?? value.maker_fee_rate),
      makerFeeSource: String(economics.maker_fee_source || value.maker_fee_source || ''),
      makerFeeCheckedAt: String(
        economics.maker_fee_checked_at || value.maker_fee_checked_at || '',
      ),
      makerRoundTripPct: nullableUnknownNumber(economics.maker_round_trip_pct),
      projectedFundingPct: nullableUnknownNumber(economics.projected_funding_pct),
      grossStepPct: nullableUnknownNumber(economics.gross_step_pct),
      hardCostPct: nullableUnknownNumber(economics.hard_cost_pct),
      feeNetEdgePct: nullableUnknownNumber(economics.fee_net_edge_pct),
      riskDiscountPct: nullableUnknownNumber(economics.risk_discount_pct),
      estimatedCrossingsPerHour: nullableUnknownNumber(economics.estimated_crossings_per_hour),
      objectiveValue: nullableUnknownNumber(economics.objective_value),
      plannedMinOrderNotional: nullableUnknownNumber(economics.planned_min_order_notional),
      minimumOrderNotional: nullableUnknownNumber(economics.minimum_order_notional),
      rejectedReason: String(economics.rejected_reason || ''),
    },
  }
}

function mapStrategyConfig(value: ApiStrategyConfig): StrategyConfigData {
  return {
    current: mapStrategySettings(value.current),
    draft: mapStrategySettings(value.draft),
    diff: Array.isArray(value.diff) ? value.diff.map(mapStrategyDiff) : [],
    draftUpdatedAt: compactTime(value.draft_updated_at),
    volatilityOptions: value.options?.volatility_methods || [],
    directionOptions: value.options?.direction_modes || [],
  }
}

function mapStrategySettings(value: ApiStrategySettings): StrategySettings {
  return {
    directionMode: value.direction_mode || 'NEUTRAL',
    directionOverrides: value.direction_overrides || {},
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
    softBreachCount: Math.trunc(toNumber(value.soft_breach_count)),
    lastRetentionDecisionAt: compactTime(value.last_retention_decision_at || ''),
    directionMode: value.direction_mode || 'NEUTRAL',
    directionSource: value.direction_source || 'global',
    seedPositionSide: value.seed_position_side || '',
    seedQty: toNumber(value.seed_qty),
    seedEntryPrice: nullableNumber(value.seed_entry_price),
    seedSlippagePct: nullableNumber(value.seed_slippage_pct),
    seedFee: toNumber(value.seed_fee),
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
    positionSide: value.position_side || '',
    orderIntent: value.order_intent || 'OPEN',
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

function unknownNumber(value: unknown): number {
  return nullableUnknownNumber(value) ?? 0
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
