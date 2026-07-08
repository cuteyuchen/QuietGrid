export type SessionState = 'RUNNING' | 'OBSERVING' | 'COOLDOWN' | 'STOPPED'

export type ConsoleSummary = {
  mode: string
  loopState: string
  heartbeat: string
  activeSessions: number
  openOrders: number
  realizedPnl: number
  balance: number | null
  riskLevel: string
  latestSystemMessage: string
}

export type GridSession = {
  id: number
  symbol: string
  state: SessionState | string
  stateLabel: string
  upper: number
  lower: number
  gridNum: number
  stepPct: number
  pnl: number
  volatilityMethod: string
  volatilityMethodLabel: string
  currentVolatility: number
  openOrderCount: number
}

export type ControlState = {
  newEntriesPaused: boolean
  newEntriesPausedUpdatedAt: string
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
  loopState: '有界测试已完成',
  heartbeat: '23:21:36',
  activeSessions: 0,
  openOrders: 0,
  realizedPnl: 0,
  balance: 4829.59,
  riskLevel: '正常',
  latestSystemMessage: '测试网有界运行完成',
}

export const controlState: ControlState = {
  newEntriesPaused: false,
  newEntriesPausedUpdatedAt: '-',
}

export const sessions: GridSession[] = [
  {
    id: 488,
    symbol: 'BCHUSDT',
    state: 'STOPPED',
    stateLabel: '已停止',
    upper: 234.1378,
    lower: 230.6886,
    gridNum: 9,
    stepPct: 0.001661,
    pnl: 0,
    volatilityMethod: 'std',
    volatilityMethodLabel: '标准差',
    currentVolatility: 0.004122,
    openOrderCount: 0,
  },
  {
    id: 487,
    symbol: 'BTCUSDT',
    state: 'STOPPED',
    stateLabel: '已停止',
    upper: 62275.5695,
    lower: 61718.4172,
    gridNum: 6,
    stepPct: 0.001505,
    pnl: 0,
    volatilityMethod: 'std',
    volatilityMethodLabel: '标准差',
    currentVolatility: 0.002496,
    openOrderCount: 0,
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

export const volatilityOptions = [
  'std',
  'parkinson',
  'garman_klass',
  'rogers_satchell',
  'yang_zhang',
]
