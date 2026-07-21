export type SessionState = 'RUNNING' | 'DEFENSIVE' | 'PAUSED' | 'OBSERVING' | 'COOLDOWN' | 'CLOSING' | 'STOPPED'

export type ConsoleSummary = {
  mode: string
  accountId: string
  accountLabel: string
  loopState: string
  heartbeat: string
  activeSessions: number
  openOrders: number
  realizedPnl: number
  balance: number | null
  availableBalance: number | null
  marginBalance: number | null
  initialMargin: number | null
  maintenanceMargin: number | null
  unrealizedPnl: number | null
  currentExposure: number | null
  accountSummary: AccountSummary
  riskLevel: string
  latestSystemMessage: string
}

export type AccountSummary = {
  status: string
  error: string
  asset: string
  balance: number | null
  availableBalance: number | null
  marginBalance: number | null
  initialMargin: number | null
  maintenanceMargin: number | null
  unrealizedPnl: number | null
  currentExposure: number | null
}

export type AccountOption = {
  id: string
  label: string
  mode: string
  binanceTestnet: boolean
  database: string
  selected: boolean
  hasApiKey: boolean
}

export type LiquidityCandidate = {
  rank: number
  symbol: string
  score: number | null
  volumeScore: number | null
  depthScore: number | null
  volume24h: number | null
  depthUsdt: number | null
  bidPrice: number | null
  askPrice: number | null
  spreadPct: number | null
  selected: boolean
  disabled: boolean
  status: string
  error: string
  volatilityMethod: string
  volatilityMethodLabel: string
  volatilityValue: number | null
  currentVolatility: number | null
  volatilityWindow: number | null
  currentVolatilityWindow: number | null
  stage: string
  snapshotAt: string
  price: number | null
  rangeLower: number | null
  rangeUpper: number | null
  rangeWidthPct: number | null
  thresholdMet: boolean
  sessionId: number | null
  marketUpdatedAt: string
  lastKlineCloseAt: string
  dataStale: boolean
  klineRequiredCount?: number | null
  klineActualCount?: number | null
  klineAgeSeconds?: number | null
  klineMissingCount?: number | null
  klineQualityStatus?: string
  regimeScore?: number | null
  regimeAllowed?: boolean | null
  blockCode?: string
  marketState?: string
  verdict?: string
  softBreachCount?: number
  gridPreview?: {
    lower?: number | null
    upper?: number | null
    gridCount?: number | null
    levelCount?: number | null
  }
  economics?: {
    directionMode?: GridDirectionMode
    makerFeeRate?: number | null
    makerFeeSource?: string
    makerFeeCheckedAt?: string
    takerFeeRate?: number | null
    makerRoundTripPct?: number | null
    projectedFundingPct?: number | null
    grossStepPct?: number | null
    hardCostPct?: number | null
    feeNetEdgePct?: number | null
    riskDiscountPct?: number | null
    seedExecutionCostPct?: number | null
    estimatedCrossingsPerHour?: number | null
    objectiveValue?: number | null
    configuredCapital?: number | null
    minimumRequiredCapital?: number | null
    plannedMinOrderNotional?: number | null
    minimumOrderNotional?: number | null
    worstCaseStopLoss?: number | null
    riskBudget?: number | null
    rejectedReason?: string
  }
}

export type GridRound = {
  id: number
  roundNumber: number
  startTime: string
  endTime: string
  status: string
  statusLabel: string
  totalPnl: number
  sessionCount: number
  activeSessionCount: number
}

export type GridSession = {
  id: number
  windowId: number
  symbol: string
  state: SessionState | string
  stateLabel: string
  softBreachCount?: number
  lastRetentionDecisionAt: string
  cooldownCurrentAtr: number | null
  cooldownAmplitudePct: number | null
  cooldownAmplitudeLimitPct: number | null
  cooldownReason: string
  cooldownEvaluatedAt: string
  directionMode: GridDirectionMode
  directionSource: string
  seedPositionSide: string
  seedQty: number
  seedEntryPrice: number | null
  seedSlippagePct: number | null
  seedFee: number
  upper: number
  lower: number
  gridNum: number
  stepPct: number
  pnl: number
  volatilityMethod: string
  volatilityMethodLabel: string
  volatilityValue: number
  volatilityWindow: number
  currentVolatility: number
  currentVolatilityWindow: number
  currentVolatilityAt: string
  baselineAtr: number
  stopLossPrice: number
  capital: number
  leverage: number
  openTime: string
  closeTime: string
  closeReason: string
  volatilityStage: string
  volatilityStageLabel: string
  volatilityProgressPct: number | null
  volatilityRemainingSeconds: number | null
  openOrderCount: number
  tradeCount: number
  orders: GridOrder[]
  trades: GridTrade[]
  performance: GridPerformance
  nextEntryDisabled: boolean
  stopRequested: boolean
  stopRequestStatus: string
  stopRequestType: string
  controlRequested: boolean
  controlRequestStatus: string
  controlRequestAction: string
  position: GridPosition
}

export type GridPosition = {
  status: string
  error: string
  symbol: string
  qty: number | null
  longQty: number | null
  shortQty: number | null
  entryPrice: number | null
  markPrice: number | null
  unrealizedPnl: number | null
  notional: number | null
}

export type GridOrder = {
  id: number
  sessionId: number
  symbol: string
  orderId: string
  gridIndex: number
  side: string
  sideLabel: string
  price: number
  qty: number
  status: string
  statusLabel: string
  createdAt: string
  filledAt: string
  fillPrice: number
  positionSide: string
  orderIntent: string
}

export type GridDirectionMode = 'LONG' | 'SHORT' | 'NEUTRAL'

export type GridTrade = {
  id: number
  sessionId: number
  symbol: string
  orderId: string
  gridIndex: number
  side: string
  sideLabel: string
  price: number
  qty: number
  quoteQty: number
  gridPnl: number
  fee: number
  fundingFee: number
  tradeTime: string
}

export type PnlPoint = {
  time: string
  value: number
}

export type GridPerformance = {
  grossGridPnl: number
  tradingFees: number
  fundingFee: number
  realizedPnl: number
  unpairedPnl: number
  initialMargin: number
  currentMargin: number | null
  marginChange: number | null
  roi: number | null
  annualizedRoi: number | null
  durationHours: number | null
  tradeCount: number
  unpairedTradeCount: number
  pnlCurve: PnlPoint[]
}

export type ControlState = {
  newEntriesPaused: boolean
  newEntriesPausedUpdatedAt: string
  disabledSymbols: string[]
  disabledSymbolsUpdatedAt: string
  startableSymbols: string[]
  sessionStopRequests: Array<Record<string, unknown>>
  sessionControlRequests: Array<Record<string, unknown>>
  roundStartRequest: Record<string, unknown> | null
  runtimeId: string
  runtimeStartedAt: string
  roundStartAvailable: boolean
  currentRoundId: number | null
  roundState: string
  roundStartedAt: string
  lastScanAt: string
  nextScanAt: string
}

export type TraderProcessState = {
  available: boolean
  mode: string
  service: string
  state: string
  detail: string
  processState?: string
  alive?: boolean
  pid?: number | null
  runtimeId?: string
  runtimeState?: string
  startedAt?: string
  heartbeatAt?: string
  heartbeatAgeSeconds?: number | null
  uptimeSeconds?: number | null
  lastStatus?: string
  lastError?: string
  processControlAvailable?: boolean
  processControlMode?: string
}

export type StrategySettings = {
  directionMode: GridDirectionMode
  directionOverrides: Record<string, GridDirectionMode>
  volatilityMethod: string
  leverage: number
  capitalPerSymbol: number
  maxConcurrent: number
  scanCandidateCount: number
  observeHours: number
  observeKlineInterval: string
  minStepPct: number
  minTradableRangePct: number
  maxGridNum: number
  stopBufferPct: number
  safetyMultiplier: number
  takeProfitUsdt: number
  totalCapitalLimit: number
  maxMakerFeeRate: number
}

export type StrategyDiff = {
  key: string
  label: string
  current: string | number
  draft: string | number
}

export type VolatilityOption = {
  value: string
  label: string
}

export type StrategyConfigData = {
  current: StrategySettings
  draft: StrategySettings
  diff: StrategyDiff[]
  draftUpdatedAt: string
  volatilityOptions: VolatilityOption[]
  directionOptions: VolatilityOption[]
}

export type VerificationRow = {
  name: string
  status: string
  statusCode: string
  detail: string
  module: string
  lastChecked: string
}

export type AuditLog = {
  level: string
  time: string
  module: string
  message: string
}

export const summary: ConsoleSummary = {
  mode: '测试网',
  accountId: 'default',
  accountLabel: '默认账户',
  loopState: '有界测试已完成',
  heartbeat: '23:21:36',
  activeSessions: 0,
  openOrders: 0,
  realizedPnl: 0,
  balance: 4829.59,
  availableBalance: 4829.59,
  marginBalance: 5012.24,
  initialMargin: 180.41,
  maintenanceMargin: 21.88,
  unrealizedPnl: 12.24,
  currentExposure: 640.5,
  accountSummary: {
    status: 'ok',
    error: '',
    asset: 'USDT',
    balance: 5000,
    availableBalance: 4829.59,
    marginBalance: 5012.24,
    initialMargin: 180.41,
    maintenanceMargin: 21.88,
    unrealizedPnl: 12.24,
    currentExposure: 640.5,
  },
  riskLevel: '正常',
  latestSystemMessage: '测试网有界运行完成',
}

export const accounts: AccountOption[] = [
  {
    id: 'default',
    label: '默认账户',
    mode: '测试网',
    binanceTestnet: true,
    database: 'data/trading.db',
    selected: true,
    hasApiKey: false,
  },
]

export const controlState: ControlState = {
  newEntriesPaused: false,
  newEntriesPausedUpdatedAt: '-',
  disabledSymbols: [],
  disabledSymbolsUpdatedAt: '-',
  startableSymbols: ['BTCUSDT', 'ETHUSDT', 'BCHUSDT'],
  sessionStopRequests: [],
  sessionControlRequests: [],
  roundStartRequest: null,
  runtimeId: '',
  runtimeStartedAt: '-',
  roundStartAvailable: false,
  currentRoundId: null,
  roundState: 'IDLE',
  roundStartedAt: '-',
  lastScanAt: '-',
  nextScanAt: '-',
}

export const traderProcessState: TraderProcessState = {
  available: true,
  mode: 'local',
  service: 'quietgrid-trader',
  state: 'stopped',
  detail: 'Trader 离线',
  processState: 'OFFLINE',
  alive: false,
  pid: null,
  runtimeId: '',
  runtimeState: '',
  startedAt: '',
  heartbeatAt: '',
  heartbeatAgeSeconds: null,
  uptimeSeconds: null,
  lastStatus: '',
  lastError: '',
  processControlAvailable: true,
  processControlMode: 'local',
}

export const strategyConfig: StrategyConfigData = {
  current: {
    directionMode: 'NEUTRAL',
    directionOverrides: {},
    volatilityMethod: 'std',
    leverage: 10,
    capitalPerSymbol: 500,
    maxConcurrent: 3,
    scanCandidateCount: 10,
    observeHours: 3,
    observeKlineInterval: '1m',
    minStepPct: 0.0015,
    minTradableRangePct: 0.0015,
    maxGridNum: 20,
    stopBufferPct: 0.015,
    safetyMultiplier: 3.5,
    takeProfitUsdt: 10,
    totalCapitalLimit: 2000,
    maxMakerFeeRate: 0.0002,
  },
  draft: {
    directionMode: 'NEUTRAL',
    directionOverrides: {},
    volatilityMethod: 'std',
    leverage: 10,
    capitalPerSymbol: 500,
    maxConcurrent: 3,
    scanCandidateCount: 10,
    observeHours: 3,
    observeKlineInterval: '1m',
    minStepPct: 0.0015,
    minTradableRangePct: 0.0015,
    maxGridNum: 20,
    stopBufferPct: 0.015,
    safetyMultiplier: 3.5,
    takeProfitUsdt: 10,
    totalCapitalLimit: 2000,
    maxMakerFeeRate: 0.0002,
  },
  diff: [],
  draftUpdatedAt: '-',
  volatilityOptions: [
    { value: 'std', label: '标准差' },
    { value: 'parkinson', label: 'Parkinson 高低价' },
    { value: 'garman_klass', label: 'Garman-Klass' },
    { value: 'rogers_satchell', label: 'Rogers-Satchell' },
    { value: 'yang_zhang', label: 'Yang-Zhang' },
    { value: 'quantile', label: '分位数' },
  ],
  directionOptions: [
    { value: 'NEUTRAL', label: '中性网格' },
    { value: 'LONG', label: '做多网格' },
    { value: 'SHORT', label: '做空网格' },
  ],
}

export const gridRounds: GridRound[] = [
  {
    id: 91,
    roundNumber: 2,
    startTime: '07/08 23:18:12',
    endTime: '07/08 23:21:36',
    status: 'closed',
    statusLabel: '已关闭',
    totalPnl: 0,
    sessionCount: 1,
    activeSessionCount: 0,
  },
  {
    id: 90,
    roundNumber: 1,
    startTime: '07/08 22:42:08',
    endTime: '07/08 22:58:41',
    status: 'closed',
    statusLabel: '已关闭',
    totalPnl: 0,
    sessionCount: 1,
    activeSessionCount: 0,
  },
]

export const sessions: GridSession[] = [
  {
    id: 488,
    windowId: 91,
    symbol: 'BCHUSDT',
    state: 'STOPPED',
    stateLabel: '已停止',
    lastRetentionDecisionAt: '23:21:10',
    cooldownCurrentAtr: null,
    cooldownAmplitudePct: null,
    cooldownAmplitudeLimitPct: null,
    cooldownReason: '',
    cooldownEvaluatedAt: '',
    directionMode: 'NEUTRAL',
    directionSource: 'global',
    seedPositionSide: '',
    seedQty: 0,
    seedEntryPrice: null,
    seedSlippagePct: null,
    seedFee: 0,
    upper: 234.1378,
    lower: 230.6886,
    gridNum: 9,
    stepPct: 0.001661,
    pnl: 0,
    volatilityMethod: 'std',
    volatilityMethodLabel: '标准差',
    volatilityValue: 0.004001,
    volatilityWindow: 60,
    currentVolatility: 0.004122,
    currentVolatilityWindow: 30,
    currentVolatilityAt: '23:21:10',
    baselineAtr: 0.82,
    stopLossPrice: 228.4,
    capital: 200,
    leverage: 10,
    openTime: '23:18:12',
    closeTime: '23:21:36',
    closeReason: 'binance_safety_sweep',
    volatilityStage: 'stopped',
    volatilityStageLabel: '已停止',
    volatilityProgressPct: 1,
    volatilityRemainingSeconds: 0,
    openOrderCount: 0,
    tradeCount: 2,
    orders: [],
    trades: [
      {
        id: 1,
        sessionId: 488,
        symbol: 'BCHUSDT',
        orderId: 'demo-trade-1',
        gridIndex: 4,
        side: 'BUY',
        sideLabel: '买入',
        price: 231.2,
        qty: 0.1,
        quoteQty: 23.12,
        gridPnl: 0.18,
        fee: 0.004,
        fundingFee: 0,
        tradeTime: '23:20:32',
      },
    ],
    performance: {
      grossGridPnl: 0.18,
      tradingFees: 0.004,
      fundingFee: 0,
      realizedPnl: 0,
      unpairedPnl: -0.176,
      initialMargin: 200,
      currentMargin: 200,
      marginChange: 0,
      roi: 0,
      annualizedRoi: 0,
      durationHours: 0.06,
      tradeCount: 1,
      unpairedTradeCount: 0,
      pnlCurve: [{ time: '23:20:32', value: 0.176 }],
    },
    nextEntryDisabled: false,
    stopRequested: false,
    stopRequestStatus: '',
    stopRequestType: '',
    controlRequested: false,
    controlRequestStatus: '',
    controlRequestAction: '',
    position: {
      status: 'historical', error: '', symbol: 'BCHUSDT', qty: 0, longQty: 0, shortQty: 0,
      entryPrice: null, markPrice: null, unrealizedPnl: null, notional: null,
    },
  },
  {
    id: 487,
    windowId: 90,
    symbol: 'BTCUSDT',
    state: 'STOPPED',
    stateLabel: '已停止',
    lastRetentionDecisionAt: '23:20:50',
    cooldownCurrentAtr: null,
    cooldownAmplitudePct: null,
    cooldownAmplitudeLimitPct: null,
    cooldownReason: '',
    cooldownEvaluatedAt: '',
    directionMode: 'NEUTRAL',
    directionSource: 'global',
    seedPositionSide: '',
    seedQty: 0,
    seedEntryPrice: null,
    seedSlippagePct: null,
    seedFee: 0,
    upper: 62275.5695,
    lower: 61718.4172,
    gridNum: 6,
    stepPct: 0.001505,
    pnl: 0,
    volatilityMethod: 'std',
    volatilityMethodLabel: '标准差',
    volatilityValue: 0.00241,
    volatilityWindow: 60,
    currentVolatility: 0.002496,
    currentVolatilityWindow: 30,
    currentVolatilityAt: '23:20:50',
    baselineAtr: 38.4,
    stopLossPrice: 61500,
    capital: 200,
    leverage: 10,
    openTime: '23:17:58',
    closeTime: '23:21:36',
    closeReason: 'binance_safety_sweep',
    volatilityStage: 'stopped',
    volatilityStageLabel: '已停止',
    volatilityProgressPct: 1,
    volatilityRemainingSeconds: 0,
    openOrderCount: 0,
    tradeCount: 0,
    orders: [],
    trades: [],
    performance: {
      grossGridPnl: 0,
      tradingFees: 0,
      fundingFee: 0,
      realizedPnl: 0,
      unpairedPnl: 0,
      initialMargin: 200,
      currentMargin: 200,
      marginChange: 0,
      roi: 0,
      annualizedRoi: 0,
      durationHours: 0.06,
      tradeCount: 0,
      unpairedTradeCount: 0,
      pnlCurve: [],
    },
    nextEntryDisabled: false,
    stopRequested: false,
    stopRequestStatus: '',
    stopRequestType: '',
    controlRequested: false,
    controlRequestStatus: '',
    controlRequestAction: '',
    position: {
      status: 'historical', error: '', symbol: 'AAPLUSDT', qty: 0, longQty: 0, shortQty: 0,
      entryPrice: null, markPrice: null, unrealizedPnl: null, notional: null,
    },
  },
]

export const verificationRows: VerificationRow[] = [
  { name: '连接与账户检查', status: '通过', statusCode: 'passed', detail: '余额、标的、手续费健康', module: 'binance_check', lastChecked: '23:21:36' },
  { name: '签名写接口', status: '通过', statusCode: 'passed', detail: '杠杆和逐仓设置可写', module: 'binance_signed_write_health', lastChecked: '23:21:36' },
  { name: '安全清扫', status: '通过', statusCode: 'passed', detail: '残留仓位 0，挂单 0', module: 'binance_safety_sweep', lastChecked: '23:21:36' },
  { name: '一键有界测试', status: '通过', statusCode: 'passed', detail: '前置检查 -> 循环 -> 清扫 -> 后置检查', module: 'binance_test_run', lastChecked: '23:21:36' },
]

export const auditLogs: AuditLog[] = [
  { level: '信息', time: '23:21:36', module: '一键测试流程', message: '测试网有界运行完成' },
  { level: '信息', time: '23:21:27', module: '安全清扫', message: '安全清扫完成，残留为 0' },
  { level: '信息', time: '23:20:49', module: '标的选择', message: '完成候选标的选择' },
]

export const liquidityCandidates: LiquidityCandidate[] = [
  {
    rank: 1,
    symbol: 'BCHUSDT',
    score: 1,
    volumeScore: 0.94,
    depthScore: 1,
    volume24h: 182000000,
    depthUsdt: 820000,
    bidPrice: 232.1,
    askPrice: 232.18,
    spreadPct: 0.00034,
    selected: true,
    disabled: false,
    status: 'ok',
    error: '',
    volatilityMethod: 'std',
    volatilityMethodLabel: '标准差',
    volatilityValue: 0.004001,
    currentVolatility: 0.004122,
    volatilityWindow: 60,
    currentVolatilityWindow: 30,
    stage: '已停止',
    snapshotAt: '23:21:36',
    price: 232.14,
    rangeLower: 230.68,
    rangeUpper: 234.14,
    rangeWidthPct: 0.015,
    thresholdMet: true,
    sessionId: 488,
    marketUpdatedAt: '23:21:36',
    lastKlineCloseAt: '23:21:00',
    dataStale: false,
  },
  {
    rank: 2,
    symbol: 'BTCUSDT',
    score: 0.82,
    volumeScore: 1,
    depthScore: 0.4,
    volume24h: 9800000000,
    depthUsdt: 326000,
    bidPrice: 62010.2,
    askPrice: 62011.4,
    spreadPct: 0.000019,
    selected: true,
    disabled: false,
    status: 'ok',
    error: '',
    volatilityMethod: 'std',
    volatilityMethodLabel: '标准差',
    volatilityValue: 0.00241,
    currentVolatility: 0.002496,
    volatilityWindow: 60,
    currentVolatilityWindow: 30,
    stage: '已停止',
    snapshotAt: '23:21:36',
    price: 62010.8,
    rangeLower: 61718.42,
    rangeUpper: 62275.57,
    rangeWidthPct: 0.009,
    thresholdMet: true,
    sessionId: 487,
    marketUpdatedAt: '23:21:36',
    lastKlineCloseAt: '23:21:00',
    dataStale: false,
  },
]

export const volatilityOptions = [
  'std',
  'parkinson',
  'garman_klass',
  'rogers_satchell',
  'yang_zhang',
]
