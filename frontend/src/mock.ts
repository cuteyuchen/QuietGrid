export type SessionState = 'RUNNING' | 'OBSERVING' | 'COOLDOWN' | 'STOPPED'

export type GridSession = {
  id: number
  symbol: string
  state: SessionState
  upper: number
  lower: number
  gridNum: number
  stepPct: number
  pnl: number
  volatilityMethod: string
  currentVolatility: number
  exposure: string
}

export const summary = {
  mode: '测试网',
  loopState: '有界测试已完成',
  heartbeat: '23:21:36',
  activeSessions: 0,
  openOrders: 0,
  realizedPnl: 0,
  balance: 4829.59,
  riskLevel: '正常',
}

export const sessions: GridSession[] = [
  {
    id: 488,
    symbol: 'BCHUSDT',
    state: 'STOPPED',
    upper: 234.1378,
    lower: 230.6886,
    gridNum: 9,
    stepPct: 0.001661,
    pnl: 0,
    volatilityMethod: 'std',
    currentVolatility: 0.004122,
    exposure: '0',
  },
  {
    id: 487,
    symbol: 'BTCUSDT',
    state: 'STOPPED',
    upper: 62275.5695,
    lower: 61718.4172,
    gridNum: 6,
    stepPct: 0.001505,
    pnl: 0,
    volatilityMethod: 'std',
    currentVolatility: 0.002496,
    exposure: '0',
  },
]

export const verificationRows = [
  { name: '连接与账户检查', status: '通过', detail: '余额、标的、手续费健康' },
  { name: '签名写接口', status: '通过', detail: '杠杆和逐仓设置可写' },
  { name: '安全清扫', status: '通过', detail: '残留仓位 0，挂单 0' },
  { name: '一键有界测试', status: '通过', detail: '前置检查 -> loop -> 清扫 -> 后置检查' },
]

export const auditLogs = [
  { level: 'INFO', time: '23:21:36', module: 'binance_test_run', message: '测试网有界运行完成' },
  { level: 'INFO', time: '23:21:27', module: 'binance_safety_sweep', message: '安全清扫完成，残留为 0' },
  { level: 'INFO', time: '23:20:49', module: 'selector', message: '完成候选标的选择' },
]

export const volatilityOptions = [
  'std',
  'parkinson',
  'garman_klass',
  'rogers_satchell',
  'yang_zhang',
]
