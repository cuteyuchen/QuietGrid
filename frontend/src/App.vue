<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  Activity,
  AlertTriangle,
  Ban,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  Database,
  Gauge,
  History,
  LayoutDashboard,
  Play,
  Power,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  Trash2,
} from '@lucide/vue'
import { consoleEventsUrl, executeConsoleAction, loadAccounts, loadConsoleData, mapLiquidityCandidate, saveStrategyConfigDraft, type ConsoleAction } from './api'
import {
  accounts as fallbackAccounts,
  auditLogs as fallbackAuditLogs,
  controlState as fallbackControlState,
  gridRounds as fallbackGridRounds,
  liquidityCandidates as fallbackLiquidityCandidates,
  sessions as fallbackSessions,
  strategyConfig as fallbackStrategyConfig,
  summary as fallbackSummary,
  traderProcessState as fallbackTraderProcessState,
  verificationRows as fallbackVerificationRows,
  type AccountOption,
  type StrategySettings,
} from './mock'

const tabs = [
  { key: 'overview', label: '总览', icon: LayoutDashboard },
  { key: 'grids', label: '网格控制', icon: Activity },
  { key: 'strategy', label: '策略参数', icon: SlidersHorizontal },
  { key: 'environment', label: '环境验证', icon: ShieldCheck },
  { key: 'logs', label: '日志审计', icon: History },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('overview')
const testRunSeconds = ref(60)
const selectedStartSymbol = ref('')
const selectedGridRoundId = ref<number | null>(null)
const selectedGridKey = ref('')
const loading = ref(false)
const actionBusy = ref<ConsoleAction | ''>('')
const strategyBusy = ref(false)
const dataError = ref('')
const actionMessage = ref('')
const actionError = ref('')
const strategyError = ref('')
const summary = ref(fallbackSummary)
const controlState = ref(fallbackControlState)
const traderProcessState = ref(fallbackTraderProcessState)
const strategyConfig = ref(fallbackStrategyConfig)
const strategyForm = ref<StrategySettings>({ ...fallbackStrategyConfig.draft })
const gridRounds = ref(fallbackGridRounds)
const sessions = ref(fallbackSessions)
const liquidityCandidates = ref(fallbackLiquidityCandidates)
const verificationRows = ref(fallbackVerificationRows)
const auditLogs = ref(fallbackAuditLogs)
const pendingAction = ref<ActionConfig | null>(null)
const actionReason = ref('控制台手动操作')
const autoRefresh = ref(true)
const realtimeConnected = ref(false)
const realtimeError = ref('')
const accountOptions = ref<AccountOption[]>(fallbackAccounts)
const selectedAccountId = ref(window.localStorage.getItem('quietgrid.accountId') || '')
const refreshIntervalMs = 10000
let refreshTimer: number | undefined
let eventSource: EventSource | undefined
const lastEventVersions: Record<string, string> = {}

const activeTabMeta = computed(() => tabs.find((tab) => tab.key === activeTab.value) ?? tabs[0])

const stateLabels: Record<string, string> = {
  RUNNING: '运行中',
  PAUSED: '已暂停',
  OBSERVING: '观察中',
  COOLDOWN: '冷却中',
  STOPPED: '已停止',
}

const volatilityLabels: Record<string, string> = {
  std: '标准差',
  parkinson: 'Parkinson 高低价',
  garman_klass: 'Garman-Klass',
  rogers_satchell: 'Rogers-Satchell',
  yang_zhang: 'Yang-Zhang',
}

const auditModuleLabels: Record<string, string> = {
  binance_test_run: '一键测试流程',
  binance_safety_sweep: '安全清扫',
  selector: '标的选择',
}

const statusCards = computed(() => [
  { label: '活动会话', value: summary.value.activeSessions, detail: '数据库实时统计', tone: 'good' },
  { label: '开放订单', value: summary.value.openOrders, detail: '活动会话未成交', tone: 'good' },
  { label: '已实现盈亏', value: summary.value.realizedPnl.toFixed(4), detail: '全量会话累计', tone: 'neutral' },
  { label: '账户余额', value: formatNullableMoney(summary.value.balance), detail: accountSummaryStatus.value, tone: 'accent' },
])

const accountSummaryStatus = computed(() => {
  const account = summary.value.accountSummary
  if (account.status === 'ok') {
    return `${account.asset} 实时账户摘要`
  }
  if (account.status === 'unconfigured') {
    return '账户密钥未配置'
  }
  return account.error || '账户摘要不可用'
})

const accountCards = computed(() => [
  { label: '可用余额', value: formatNullableMoney(summary.value.availableBalance), detail: '可用于新开仓', tone: 'good' },
  { label: '保证金余额', value: formatNullableMoney(summary.value.marginBalance), detail: '含未实现盈亏', tone: 'accent' },
  { label: '占用保证金', value: formatNullableMoney(summary.value.initialMargin), detail: '当前初始保证金', tone: 'neutral' },
  { label: '当前暴露', value: formatNullableMoney(summary.value.currentExposure), detail: '持仓 notional 绝对值', tone: 'neutral' },
])

const dataSourceLabel = computed(() => (dataError.value ? '离线示例' : '实时数据'))
const refreshModeLabel = computed(() => {
  if (!autoRefresh.value) {
    return '自动刷新关'
  }
  return realtimeConnected.value ? 'SSE 实时刷新' : '轮询刷新 10s'
})
const refreshStatusText = computed(() => {
  if (realtimeConnected.value) {
    return '事件流已连接'
  }
  return realtimeError.value || '事件流未连接，使用轮询兜底'
})
const selectedAccount = computed(() => {
  const selected = accountOptions.value.find((account) => account.id === selectedAccountId.value)
  if (selected) {
    return selected
  }
  return accountOptions.value[0] ?? {
    id: summary.value.accountId,
    label: summary.value.accountLabel,
    mode: summary.value.mode,
    binanceTestnet: summary.value.mode === '测试网',
    database: '',
    selected: true,
    hasApiKey: summary.value.accountSummary.status === 'ok',
  }
})
const selectedAccountDetail = computed(() => {
  const account = selectedAccount.value
  const keyState = account.hasApiKey ? '密钥已配置' : '未配置密钥'
  const parts = [account.mode, keyState]
  if (account.database) {
    parts.push(account.database)
  }
  return parts.filter(Boolean).join(' · ')
})
const paused = computed(() => controlState.value.newEntriesPaused)
const startableSymbols = computed(() => controlState.value.startableSymbols)
const rankedStartableSymbols = computed(() => {
  const configured = new Set(startableSymbols.value)
  const ranked = liquidityCandidates.value
    .filter((candidate) => configured.has(candidate.symbol))
    .sort((left, right) => left.rank - right.rank)
    .map((candidate) => candidate.symbol)
  const seen = new Set<string>()
  return [...ranked, ...startableSymbols.value].filter((symbol) => {
    if (seen.has(symbol)) {
      return false
    }
    seen.add(symbol)
    return true
  })
})
const startGridSymbol = computed(() => selectedStartSymbol.value || rankedStartableSymbols.value[0] || '')
const selectedGridRound = computed(() => gridRounds.value.find((round) => round.id === selectedGridRoundId.value) ?? gridRounds.value[0] ?? null)
const isCurrentRoundSelected = computed(() => selectedGridRoundId.value === controlState.value.currentRoundId)
const roundCanStop = computed(() => Boolean(
  controlState.value.currentRoundId
  && !['IDLE', 'STOPPED'].includes(controlState.value.roundState),
))
const gridTabs = computed(() => {
  const tabs: Array<{ key: string; symbol: string; sessionId?: number; stateLabel: string; disabled: boolean }> = []
  const roundId = selectedGridRoundId.value ?? selectedGridRound.value?.id
  const sessionSymbols = new Set<string>()
  for (const session of sessions.value.filter((item) => item.windowId === roundId)) {
    sessionSymbols.add(session.symbol)
    const key = `session:${session.id}`
    tabs.push({
      key,
      symbol: session.symbol,
      sessionId: session.id,
      stateLabel: session.stateLabel || formatState(session.state),
      disabled: session.nextEntryDisabled,
    })
  }
  for (const candidate of liquidityCandidates.value) {
    if (sessionSymbols.has(candidate.symbol)) {
      continue
    }
    tabs.push({
      key: `candidate:${candidate.symbol}`,
      symbol: candidate.symbol,
      stateLabel: formatCandidateStage(candidate.stage),
      disabled: candidate.disabled,
    })
  }
  return tabs
})
const selectedGridTab = computed(() => gridTabs.value.find((tab) => tab.key === selectedGridKey.value) ?? gridTabs.value[0])
const selectedGridSession = computed(() => {
  const tab = selectedGridTab.value
  if (!tab?.sessionId) {
    return null
  }
  return sessions.value.find((session) => session.id === tab.sessionId) ?? null
})
const selectedGridCandidate = computed(() => {
  const symbol = selectedGridTab.value?.symbol
  return symbol ? liquidityCandidates.value.find((candidate) => candidate.symbol === symbol) ?? null : null
})
const selectedGridSymbol = computed(() => selectedGridTab.value?.symbol || startGridSymbol.value)
const selectedOpenOrders = computed(() => {
  const session = selectedGridSession.value
  if (!session) {
    return []
  }
  return session.orders.filter((order) => order.status === 'open').slice(0, 20)
})
const selectedHistoryOrders = computed(() => selectedGridSession.value?.orders.filter((order) => order.status !== 'open').slice(0, 20) ?? [])
const selectedRecentTrades = computed(() => selectedGridSession.value?.trades.slice(0, 8) ?? [])
const selectedPerformance = computed(() => selectedGridSession.value?.performance ?? null)
const selectedStageSteps = computed(() => {
  const stage = selectedGridSession.value?.volatilityStage || 'pending'
  const stepIndexByStage: Record<string, number> = {
    pending: 0,
    observing: 0,
    calculating: 1,
    trading: 3,
    stopped: 3,
  }
  const activeIndex = stepIndexByStage[stage] ?? 0
  return [
    { key: 'observing', label: '加载历史行情', detail: '立即读取历史 1 分钟 K 线；仅在样本不足时等待补齐', done: activeIndex > 0, active: activeIndex === 0 },
    { key: 'calculating', label: '波动计算中', detail: '计算区间和网格', done: activeIndex > 1, active: activeIndex === 1 },
    { key: 'ready', label: '计算结束', detail: '等待自动交易', done: activeIndex > 2, active: activeIndex === 2 },
    { key: 'trading', label: selectedGridSession.value?.volatilityStage === 'stopped' ? '已停止' : '自动交易已启动', detail: '网格交易中', done: stage === 'trading' || stage === 'stopped', active: activeIndex === 3 || stage === 'stopped' },
  ]
})
const selectedPnlPolyline = computed(() => {
  const points = selectedPerformance.value?.pnlCurve ?? []
  if (points.length === 0) {
    return ''
  }
  const values = points.map((point) => point.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  return points
    .map((point, index) => {
      const x = points.length === 1 ? 100 : (index / (points.length - 1)) * 100
      const y = 40 - ((point.value - min) / range) * 32 - 4
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
})
const selectedGridChart = computed(() => {
  const session = selectedGridSession.value
  if (!session || !Number.isFinite(session.lower) || !Number.isFinite(session.upper) || session.upper <= session.lower) {
    return null
  }
  const currentPrice = selectedGridCandidate.value?.price ?? null
  const prices = [
    session.lower,
    session.upper,
    ...(currentPrice && Number.isFinite(currentPrice) ? [currentPrice] : []),
    ...(session.stopLossPrice > 0 ? [session.stopLossPrice] : []),
    ...session.orders.filter((order) => order.status === 'open').map((order) => order.price),
    ...session.trades.map((trade) => trade.price),
  ].filter((value) => Number.isFinite(value) && value > 0)
  const min = Math.min(...prices, session.lower)
  const max = Math.max(...prices, session.upper)
  const padding = Math.max((max - min) * 0.08, Math.abs(session.upper - session.lower) * 0.08, 0.0001)
  const minPrice = Math.max(0, min - padding)
  const maxPrice = max + padding
  const yForPrice = (price: number) => {
    const value = 94 - ((price - minPrice) / (maxPrice - minPrice)) * 84
    return Math.max(6, Math.min(94, value))
  }
  const gridCount = Math.max(1, Math.trunc(session.gridNum || 1))
  const visualGridCount = Math.min(gridCount, 24)
  const gridIndexes = [...new Set(
    Array.from({ length: visualGridCount + 1 }, (_, index) => Math.round((index / visualGridCount) * gridCount)),
  )]
  const labelEvery = Math.max(1, Math.ceil(gridIndexes.length / 5))
  const lines = gridIndexes.map((gridIndex, index) => {
    const compounded = session.stepPct > 0
      ? session.lower * ((1 + session.stepPct) ** gridIndex)
      : session.lower + ((session.upper - session.lower) / gridCount) * gridIndex
    const price = gridIndex === gridCount ? session.upper : Math.min(session.upper, compounded)
    return {
      key: gridIndex,
      price,
      y: yForPrice(price),
      boundary: gridIndex === 0 || gridIndex === gridCount,
      showLabel: gridIndex === 0 || gridIndex === gridCount || index % labelEvery === 0,
    }
  })
  const xForIndex = (index: number) => 8 + ((index % 12) / 11) * 80
  const orderPoints = session.orders
    .filter((order) => order.status === 'open' && Number.isFinite(order.price) && order.price > 0)
    .slice(0, 48)
    .map((order, index) => ({
      x: xForIndex(index),
      y: yForPrice(order.price),
      side: order.side,
      title: `${order.sideLabel || order.side}挂单 · ${formatNumber(order.price, 4)}`,
    }))
  const tradePoints = session.trades
    .filter((trade) => Number.isFinite(trade.price) && trade.price > 0)
    .slice(0, 48)
    .map((trade, index) => ({
      x: xForIndex(index + 4),
      y: yForPrice(trade.price),
      side: trade.side,
      title: `${trade.sideLabel || trade.side}成交 · ${formatNumber(trade.price, 4)}`,
    }))
  return {
    minPrice,
    maxPrice,
    stopPrice: session.stopLossPrice,
    stopY: session.stopLossPrice ? yForPrice(session.stopLossPrice) : null,
    currentPrice,
    currentY: currentPrice && Number.isFinite(currentPrice) ? yForPrice(currentPrice) : null,
    lines,
    orderPoints,
    tradePoints,
  }
})
const overviewVolatilityRows = computed(() => {
  const candidateRows = liquidityCandidates.value.slice(0, 8).map((candidate) => ({
    rank: candidate.rank,
    symbol: candidate.symbol,
    price: candidate.price,
    method: candidate.volatilityMethodLabel || formatVolatilityMethod(candidate.volatilityMethod),
    current: candidate.currentVolatility,
    window: candidate.currentVolatilityWindow || candidate.volatilityWindow,
    source: candidate.stage,
    score: candidate.score,
    volume24h: candidate.volume24h,
    depthUsdt: candidate.depthUsdt,
    spreadPct: candidate.spreadPct,
    selected: candidate.selected,
    status: candidate.status,
    error: candidate.error,
    snapshotAt: candidate.snapshotAt,
  }))
  if (candidateRows.length) {
    return candidateRows
  }
  return startableSymbols.value.slice(0, 6).map((symbol) => ({
    rank: 0,
    symbol,
    price: null,
    method: formatVolatilityMethod(strategyConfig.value.current.volatilityMethod),
    current: null,
    window: null,
    source: '等待波动计算',
    score: null,
    volume24h: null,
    depthUsdt: null,
    spreadPct: null,
    selected: false,
    status: 'unconfigured',
    error: '',
    snapshotAt: '',
  }))
})

type ActionConfig = {
  action: ConsoleAction
  title: string
  description: string
  buttonLabel: string
  tone: 'primary' | 'danger' | 'secondary'
  sessionId?: number
  symbol?: string
}

function formatPct(value: number) {
  return `${(value * 100).toFixed(3)}%`
}

function formatBalance(value: number | null) {
  return typeof value === 'number' ? value.toFixed(2) : '-'
}

function formatNullableMoney(value: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)} USDT` : '-'
}

function formatNullablePct(value: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? formatPct(value) : '-'
}

function formatNullableRoi(value: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? formatPct(value) : '-'
}

function formatCompactNumber(value: number | null) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '-'
  }
  return new Intl.NumberFormat('zh-CN', {
    notation: 'compact',
    maximumFractionDigits: 2,
  }).format(value)
}

function formatNumber(value: number, digits = 4) {
  return Number.isFinite(value) ? value.toFixed(digits) : '-'
}

function formatSeconds(value: number | null) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '-'
  }
  if (value <= 0) {
    return '0 秒'
  }
  const minutes = Math.floor(value / 60)
  const seconds = Math.floor(value % 60)
  if (minutes <= 0) {
    return `${seconds} 秒`
  }
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  if (hours <= 0) {
    return `${minutes} 分 ${seconds} 秒`
  }
  return `${hours} 小时 ${restMinutes} 分`
}

function formatMoney(value: number) {
  return Number.isFinite(value) ? `${value.toFixed(4)} USDT` : '-'
}

function formatSignedMoney(value: number) {
  if (!Number.isFinite(value)) {
    return '-'
  }
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(4)} USDT`
}

function formatNullableSignedMoney(value: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? formatSignedMoney(value) : '-'
}

function formatState(value: string) {
  return stateLabels[value] ?? value
}

function formatVolatilityMethod(value: string) {
  return volatilityLabels[value] ?? value
}

function formatStopRequestStatus(value: string, requestType = '') {
  const prefix = requestType === 'manual_close' ? '平仓' : '停止'
  const labels: Record<string, string> = {
    requested: `${prefix}请求已提交`,
    closing: `${prefix}清理中`,
    completed: `${prefix}已完成`,
  }
  return labels[value] ?? value
}

function formatAuditModule(value: string) {
  return auditModuleLabels[value] ?? value
}

function formatRoundOptionTime(value: string) {
  return value.replace(/:\d{2}$/, '')
}

function formatCandidateStatus(row: (typeof overviewVolatilityRows.value)[number]) {
  if (row.source) {
    return formatCandidateStage(row.source)
  }
  if (row.status === 'ok') {
    return row.selected ? '已入选' : '候选'
  }
  if (row.status === 'cached' || row.status === 'stale') {
    return row.snapshotAt && row.snapshotAt !== '-' ? `缓存 ${row.snapshotAt}` : '缓存'
  }
  return row.error || '未配置'
}

function formatCandidateStage(value: string) {
  const labels: Record<string, string> = {
    scanning: '扫描中',
    requested: '启动请求已提交',
    running: '运行中',
    stopping: '停止中',
    eligible: '达到阈值',
    trading: '交易中',
    cooldown: '冷静期',
    paused: '已暂停',
    stopped: '已停止',
    below_threshold: '未达到阈值',
    not_selected: '未入选',
    error: '计算异常',
  }
  return labels[value] ?? (value || '扫描中')
}

async function refreshData() {
  loading.value = true
  try {
    const accountsData = await loadAccounts()
    accountOptions.value = accountsData.accounts.length > 0 ? accountsData.accounts : fallbackAccounts
    if (!accountOptions.value.some((account) => account.id === selectedAccountId.value)) {
      selectedAccountId.value = accountsData.currentAccountId
    }
    const data = await loadConsoleData(selectedAccountId.value, selectedGridRoundId.value ?? undefined)
    summary.value = data.summary
    controlState.value = data.controlState
    traderProcessState.value = data.traderProcessState
    strategyConfig.value = data.strategyConfig
    strategyForm.value = { ...data.strategyConfig.draft }
    gridRounds.value = data.gridRounds
    selectedGridRoundId.value = data.selectedGridRoundId
    sessions.value = data.sessions
    liquidityCandidates.value = data.liquidityCandidates
    verificationRows.value = data.verificationRows
    auditLogs.value = data.auditLogs
    if (!selectedStartSymbol.value || !data.controlState.startableSymbols.includes(selectedStartSymbol.value)) {
      selectedStartSymbol.value = rankedStartableSymbols.value[0] || ''
    }
    if (!gridTabs.value.some((tab) => tab.key === selectedGridKey.value)) {
      selectedGridKey.value = gridTabs.value[0]?.key || ''
    }
    selectedAccountId.value = data.summary.accountId
    window.localStorage.setItem('quietgrid.accountId', selectedAccountId.value)
    dataError.value = ''
  } catch (error) {
    dataError.value = error instanceof Error ? error.message : '无法连接控制台 API'
  } finally {
    loading.value = false
  }
}

function handleAccountChange() {
  window.localStorage.setItem('quietgrid.accountId', selectedAccountId.value)
  selectedStartSymbol.value = ''
  selectedGridRoundId.value = null
  selectedGridKey.value = ''
  startEventStream()
  void refreshData()
}

function handleGridRoundChange() {
  selectedGridKey.value = ''
  void refreshData()
}

function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    startEventStream()
    void refreshData()
  } else {
    closeEventStream()
  }
}

function startAutoRefresh() {
  refreshTimer = window.setInterval(() => {
    if (!autoRefresh.value || realtimeConnected.value || loading.value || actionBusy.value || pendingAction.value || activeTab.value === 'strategy') {
      return
    }
    void refreshData()
  }, refreshIntervalMs)
}

function startEventStream() {
  closeEventStream()
  if (!autoRefresh.value) {
    return
  }
  realtimeError.value = ''
  for (const key of Object.keys(lastEventVersions)) {
    delete lastEventVersions[key]
  }
  const source = new EventSource(consoleEventsUrl(selectedAccountId.value))
  eventSource = source
  source.onopen = () => {
    realtimeConnected.value = true
    realtimeError.value = ''
  }
  source.addEventListener('runtime', (event) => {
    const payload = parseEventPayload(event)
    if (!payload || payload.account_id !== selectedAccountId.value || payload.version === lastEventVersions.runtime) {
      return
    }
    lastEventVersions.runtime = payload.version
    if (!loading.value && !actionBusy.value && !pendingAction.value && activeTab.value !== 'strategy') {
      void refreshData()
    }
  })
  source.addEventListener('market', (event) => {
    const payload = parseEventPayload(event)
    if (!payload || payload.account_id !== selectedAccountId.value || payload.version === lastEventVersions.market) {
      return
    }
    lastEventVersions.market = payload.version
    const items = Array.isArray(payload.items) ? payload.items : []
    if (typeof payload.round_id === 'number' && payload.round_id === selectedGridRoundId.value) {
      liquidityCandidates.value = items.map((item) => mapLiquidityCandidate(item as never))
    }
  })
  source.addEventListener('session', (event) => {
    const payload = parseEventPayload(event)
    if (!payload || payload.account_id !== selectedAccountId.value || payload.version === lastEventVersions.session) {
      return
    }
    lastEventVersions.session = payload.version
    if (!loading.value && !actionBusy.value && !pendingAction.value && activeTab.value !== 'strategy') {
      void refreshData()
    }
  })
  source.onerror = () => {
    realtimeConnected.value = false
    realtimeError.value = '事件流重连中'
  }
}

function closeEventStream() {
  if (eventSource) {
    eventSource.close()
    eventSource = undefined
  }
  realtimeConnected.value = false
}

function parseEventPayload(event: Event): Record<string, unknown> & { account_id: string; version: string } | null {
  const data = event instanceof MessageEvent ? event.data : ''
  if (!data) {
    return null
  }
  try {
    const parsed = JSON.parse(String(data)) as Record<string, unknown>
    if (typeof parsed.account_id !== 'string' || typeof parsed.version !== 'string') {
      return null
    }
    return parsed as Record<string, unknown> & { account_id: string; version: string }
  } catch {
    return null
  }
}

async function saveStrategyDraft() {
  if (strategyBusy.value) {
    return
  }
  strategyBusy.value = true
  actionMessage.value = ''
  strategyError.value = ''
  try {
    const result = await saveStrategyConfigDraft(strategyForm.value, selectedAccountId.value)
    strategyConfig.value = result.config
    strategyForm.value = { ...result.config.draft }
    actionMessage.value = result.message
    await refreshData()
  } catch (error) {
    strategyError.value = error instanceof Error ? error.message : '策略参数保存失败'
  } finally {
    strategyBusy.value = false
  }
}

function resetStrategyDraft() {
  strategyForm.value = { ...strategyConfig.value.current }
}

function formatStrategyValue(key: string, value: string | number) {
  if (key === 'volatility_method') {
    return formatVolatilityMethod(String(value))
  }
  if (key === 'min_step_pct' || key === 'min_tradable_range_pct') {
    return formatPct(Number(value))
  }
  if (key === 'stop_buffer_pct') {
    return formatPct(Number(value))
  }
  if (key === 'observe_hours') {
    return `${Number(value).toFixed(2)} 小时`
  }
  if (key === 'capital_per_symbol' || key === 'take_profit_usdt' || key === 'total_capital_limit') {
    return `${Number(value).toFixed(2)} USDT`
  }
  if (key === 'max_maker_fee_rate') {
    return formatPct(Number(value))
  }
  return String(value)
}

function openAction(config: ActionConfig) {
  actionMessage.value = ''
  actionError.value = ''
  actionReason.value = config.title
  pendingAction.value = config
}

function closeAction() {
  if (actionBusy.value) {
    return
  }
  pendingAction.value = null
}

async function confirmAction() {
  if (!pendingAction.value || actionBusy.value) {
    return
  }
  const config = pendingAction.value
  actionBusy.value = config.action
  actionMessage.value = ''
  actionError.value = ''
  try {
    const result = await executeConsoleAction(config.action, {
      accountId: selectedAccountId.value,
      reason: actionReason.value.trim() || config.title,
      loopSeconds: config.action === 'bounded-run' || config.action === 'symbol-start-grid' ? testRunSeconds.value : undefined,
      sessionId: config.sessionId,
      symbol: config.symbol,
    })
    actionMessage.value = result.message
    pendingAction.value = null
    await refreshData()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '控制动作执行失败'
  } finally {
    actionBusy.value = ''
  }
}

function canStopSession(session: (typeof sessions.value)[number]) {
  return session.state !== 'STOPPED' && !session.stopRequested
}

function canManualCloseSession(session: (typeof sessions.value)[number]) {
  return session.state !== 'STOPPED' && !session.stopRequested
}

function stopSessionAction(session: (typeof sessions.value)[number]): ActionConfig {
  return {
    action: 'session-stop',
    sessionId: session.id,
    symbol: session.symbol,
    title: `停止 ${session.symbol} 网格`,
    description: `将提交停止请求。交易循环下一轮会撤销 ${session.symbol} 挂单并尝试同步平仓，完成后会写入审计日志。`,
    buttonLabel: '确认停止',
    tone: 'danger',
  }
}

function manualCloseSessionAction(session: (typeof sessions.value)[number]): ActionConfig {
  return {
    action: 'session-manual-close',
    sessionId: session.id,
    symbol: session.symbol,
    title: `手动平仓 ${session.symbol}`,
    description: `将提交手动平仓请求。交易循环下一轮会撤销 ${session.symbol} 挂单并同步平仓，完成后会写入手动平仓审计日志。`,
    buttonLabel: '确认平仓',
    tone: 'danger',
  }
}

function stopAllSessionsAction(): ActionConfig {
  return {
    action: 'all-sessions-stop',
    title: '停止当前整轮网格',
    description: '将停止当前轮次的后续扫描，并逐个撤销活动标的挂单、同步平仓、关闭会话与轮次。停止后必须重启 trader 才能再次启动新一轮。',
    buttonLabel: '确认停止整轮',
    tone: 'danger',
  }
}

function symbolToggleAction(session: (typeof sessions.value)[number]): ActionConfig {
  const willEnable = session.nextEntryDisabled
  return {
    action: willEnable ? 'symbol-enable-next-entry' : 'symbol-disable-next-entry',
    symbol: session.symbol,
    title: willEnable ? `启用 ${session.symbol} 下一轮开仓` : `禁用 ${session.symbol} 下一轮开仓`,
    description: willEnable
      ? `${session.symbol} 将重新允许在后续交易循环中被选择并创建新网格。`
      : `${session.symbol} 后续不会再新建网格；已存在会话仍会继续对账、风控和手动停止处理。`,
    buttonLabel: willEnable ? '确认启用' : '确认禁用',
    tone: willEnable ? 'secondary' : 'danger',
  }
}

function boundedRunAction(): ActionConfig {
  return {
    action: 'bounded-run',
    title: '启动网格有界运行',
    description: `将在当前连接的 ${summary.value.mode} 账户运行 ${testRunSeconds.value} 秒，并自动执行前置持仓检查、安全清扫和后置检查。`,
    buttonLabel: '确认执行',
    tone: 'primary',
  }
}

function startSymbolGridAction(): ActionConfig {
  const symbol = selectedGridSymbol.value || startGridSymbol.value
  return {
    action: 'symbol-start-grid',
    symbol,
    title: `启动 ${symbol || '指定标的'} 网格`,
    description: `将只针对 ${symbol || '所选标的'} 在当前连接的 ${summary.value.mode} 账户运行 ${testRunSeconds.value} 秒，并沿用前置持仓检查、安全清扫和后置检查。`,
    buttonLabel: '确认启动',
    tone: 'primary',
  }
}

function startGridRoundAction(): ActionConfig {
  return {
    action: 'grid-round-start',
    title: '启动一轮网格',
    description: `交易服务将在当前连接的 ${summary.value.mode} 账户扫描流动性前 ${strategyConfig.value.draft.scanCandidateCount} 个标的，按最小可交易波动阈值筛选，并最多启动 ${strategyConfig.value.draft.maxConcurrent} 个网格。`,
    buttonLabel: '确认启动本轮',
    tone: 'primary',
  }
}

function sessionPauseToggleAction(session: (typeof sessions.value)[number]): ActionConfig {
  const willResume = session.state === 'PAUSED'
  return {
    action: willResume ? 'session-resume' : 'session-pause',
    sessionId: session.id,
    symbol: session.symbol,
    title: willResume ? `恢复 ${session.symbol} 网格` : `暂停 ${session.symbol} 网格`,
    description: willResume
      ? '将重新计算当前波动区间并恢复普通网格挂单，现有持仓和保护性风控保持有效。'
      : '将撤销普通网格挂单，但保留现有持仓和保护性止损；暂停期间仍会进行持仓对账与风险检查。',
    buttonLabel: willResume ? '确认恢复' : '确认暂停',
    tone: willResume ? 'primary' : 'secondary',
  }
}

function verifyEnvironmentAction(): ActionConfig {
  return {
    action: 'environment-verify-readonly',
    title: '验证当前连接环境',
    description: `只读取 ${summary.value.mode} 的接口连通性、账户摘要、可用资金和当前暴露，不会下单、撤单、改杠杆或平仓。`,
    buttonLabel: '开始只读验证',
    tone: 'primary',
  }
}

function safetySweepAction(): ActionConfig {
  return {
    action: 'safety-sweep',
    title: '执行安全清扫',
    description: '将撤销 allowlist 标的挂单、尝试关闭残留仓位，并同步关闭数据库中的未结束会话。',
    buttonLabel: '确认清扫',
    tone: 'danger',
  }
}

function pauseToggleAction(): ActionConfig {
  const willResume = paused.value
  return {
    action: willResume ? 'resume-new-entries' : 'pause-new-entries',
    title: willResume ? '恢复新开仓' : '暂停新开仓',
    description: willResume
      ? '当前活动轮次会继续扫描候选，并在完整 1 分钟 K 线计算达到阈值后补充新标的；不会重启已停止轮次或已停止标的。'
      : '当前活动轮次仍会更新实时价格和每分钟波动率，但不会为新标的创建网格；已有会话继续对账和风控。',
    buttonLabel: willResume ? '确认恢复' : '确认暂停',
    tone: willResume ? 'secondary' : 'danger',
  }
}

function traderLoopAction(operation: 'stop' | 'restart'): ActionConfig {
  const isStop = operation === 'stop'
  return {
    action: isStop ? 'trader-loop-stop' : 'trader-loop-restart',
    title: isStop ? '停止交易 loop 进程' : '重启交易 loop 进程',
    description: `${isStop ? '停止' : '重启'}后台交易进程 ${traderProcessState.value.service}。这会作用于运维层交易服务，不等同于暂停新开仓或停止单个网格。`,
    buttonLabel: isStop ? '确认停止进程' : '确认重启进程',
    tone: 'danger',
  }
}

onMounted(() => {
  void refreshData().then(() => startEventStream())
  startAutoRefresh()
})

onUnmounted(() => {
  closeEventStream()
  if (refreshTimer) {
    window.clearInterval(refreshTimer)
  }
})
</script>

<template>
  <main class="shell">
    <aside class="sidebar" aria-label="主导航">
      <div class="brand">
        <div class="brand-mark">
          <Gauge :size="22" />
        </div>
        <div>
          <p class="eyebrow">QuietGrid</p>
          <h1>网格控制台</h1>
        </div>
      </div>

      <nav class="nav-list">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="nav-item"
          :class="{ active: activeTab === tab.key }"
          type="button"
          @click="activeTab = tab.key"
        >
          <component :is="tab.icon" :size="18" />
          <span>{{ tab.label }}</span>
          <ChevronRight :size="16" class="nav-arrow" />
        </button>
      </nav>

      <section class="runtime-card">
        <div class="runtime-row">
          <span>运行模式</span>
          <strong>{{ summary.mode }}</strong>
        </div>
          <div class="runtime-row">
            <span>最近心跳</span>
            <strong>{{ summary.heartbeat }}</strong>
          </div>
          <label class="account-switcher">
            <span>连接账户</span>
            <select v-model="selectedAccountId" :disabled="loading || Boolean(actionBusy)" @change="handleAccountChange">
              <option v-for="account in accountOptions" :key="account.id" :value="account.id">
                {{ account.label }} · {{ account.mode }}
              </option>
            </select>
            <small>{{ selectedAccountDetail }}</small>
          </label>
          <div class="health-pill">
            <CheckCircle2 :size="16" />
            {{ summary.riskLevel }}
          </div>
      </section>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">下一阶段 Vue 控制台</p>
          <h2>{{ activeTabMeta.label }}</h2>
          <div class="data-source">
            <span class="data-pill" :class="{ warning: dataError }">{{ dataSourceLabel }}</span>
            <small v-if="dataError">{{ dataError }}，正在显示兜底数据</small>
            <small v-else>{{ summary.latestSystemMessage || '已接入控制台 API' }}</small>
          </div>
        </div>
        <div class="top-actions">
          <button class="secondary-button" type="button" :class="{ active: autoRefresh }" :title="refreshStatusText" @click="toggleAutoRefresh">
            <RefreshCw :size="18" />
            {{ refreshModeLabel }}
          </button>
          <button class="icon-button" type="button" aria-label="刷新数据" :disabled="loading" @click="refreshData">
            <RefreshCw :size="18" :class="{ spinning: loading }" />
          </button>
          <button class="danger-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(safetySweepAction())">
            <Trash2 :size="18" />
            安全清扫
          </button>
        </div>
      </header>

      <section v-if="activeTab === 'overview'" class="panel-stack">
        <div class="hero-panel">
          <div>
            <p class="eyebrow">当前状态</p>
            <h3>{{ summary.loopState }}</h3>
            <p class="muted">{{ summary.latestSystemMessage || '等待系统日志写入后展示最近运行状态。' }}</p>
          </div>
          <div class="hero-actions">
            <button v-if="controlState.roundStartAvailable" class="primary-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(startGridRoundAction())">
              <Play :size="18" />
              启动一轮网格
            </button>
            <div v-else class="round-runtime-status">
              <strong>{{ formatCandidateStage(controlState.roundState.toLowerCase()) }}</strong>
              <small v-if="controlState.roundState === 'STOPPED'">本轮已结束，可以直接启动新一轮。</small>
              <small v-else>启动 {{ controlState.roundStartedAt || '-' }} · 最后扫描 {{ controlState.lastScanAt || '-' }} · 下次 {{ controlState.nextScanAt || '-' }}</small>
            </div>
            <button v-if="roundCanStop" class="secondary-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(pauseToggleAction())">
              <CirclePause :size="18" />
              {{ paused ? '恢复新开仓' : '暂停新开仓' }}
            </button>
          </div>
        </div>

        <div class="metric-grid">
          <article v-for="item in statusCards" :key="item.label" class="metric-card" :class="item.tone">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
            <small>{{ item.detail }}</small>
          </article>
        </div>

        <div class="account-grid">
          <article v-for="item in accountCards" :key="item.label" class="account-card" :class="item.tone">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
            <small>{{ item.detail }}</small>
          </article>
        </div>

        <div class="split-grid">
          <section class="surface">
            <div class="section-title">
              <BarChart3 :size="18" />
              <h3>流动性候选波动率</h3>
            </div>
            <div class="candidate-table" role="table" aria-label="流动性候选榜">
              <div class="candidate-row candidate-head" role="row">
                <span>排名</span>
                <span>标的</span>
                <span>实时价</span>
                <span>综合分</span>
                <span>24h成交额</span>
                <span>盘口深度</span>
                <span>价差</span>
                <span>当前波动率</span>
                <span>入选</span>
              </div>
              <div v-for="row in overviewVolatilityRows" :key="row.symbol" class="candidate-row" role="row">
                <span>{{ row.rank || '-' }}</span>
                <strong>{{ row.symbol }}</strong>
                <span>{{ row.price === null ? '-' : formatNumber(row.price, 4) }}</span>
                <span>{{ row.score === null ? '-' : row.score.toFixed(3) }}</span>
                <span>{{ formatCompactNumber(row.volume24h) }}</span>
                <span>{{ formatNullableMoney(row.depthUsdt) }}</span>
                <span>{{ formatNullablePct(row.spreadPct) }}</span>
                <span>
                  {{ formatNullablePct(row.current) }}
                  <small>{{ row.method }} · {{ row.window ? `${row.window} 根K线` : row.source }}</small>
                </span>
                <span class="status-badge" :class="{ selected: row.selected, warning: row.status !== 'ok' }">
                  {{ formatCandidateStatus(row) }}
                </span>
              </div>
            </div>
          </section>
          <section class="surface">
            <div class="section-title">
              <ShieldCheck :size="18" />
              <h3>当前连接环境验证</h3>
            </div>
            <div class="environment-summary">
              <div>
                <span>环境</span>
                <strong>{{ summary.mode }}</strong>
              </div>
              <div>
                <span>账户</span>
                <strong>{{ summary.accountLabel }}</strong>
              </div>
            </div>
            <div class="verification-list">
              <div v-for="row in verificationRows" :key="row.name" class="verification-row">
                <CheckCircle2 :size="18" />
                <div>
                  <strong>{{ row.name }}</strong>
                  <span>{{ row.detail }}</span>
                  <small>{{ row.status }} · {{ row.lastChecked }}</small>
                </div>
              </div>
            </div>
          </section>
        </div>
      </section>

      <section v-if="activeTab === 'grids'" class="panel-stack">
        <div class="control-bar">
          <button
            v-if="controlState.roundStartAvailable"
            class="primary-button"
            type="button"
            :disabled="Boolean(actionBusy)"
            @click="openAction(startGridRoundAction())"
          >
            <Play :size="18" />
            启动新一轮
          </button>
          <span v-else class="control-note">
            第 {{ controlState.currentRoundId || '-' }} 轮 · {{ formatCandidateStage(controlState.roundState.toLowerCase()) }} ·
            最后扫描 {{ controlState.lastScanAt || '-' }} · 下次扫描 {{ controlState.nextScanAt || '-' }}
          </span>
          <button v-if="roundCanStop" class="secondary-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(pauseToggleAction())">
            <CirclePause :size="18" />
            {{ paused ? '恢复新开仓' : '暂停新开仓' }}
          </button>
          <span class="control-note">
            每轮扫描 {{ strategyConfig.draft.scanCandidateCount }} 个候选，最多交易 {{ strategyConfig.draft.maxConcurrent }} 个；
            当前：{{ roundCanStop ? (paused ? '已暂停新开仓，仍持续计算波动率' : '允许达到阈值的候选自动交易') : '本轮已停止，恢复新开仓不会重启本轮' }}
          </span>
          <button
            class="danger-button"
            type="button"
            :disabled="Boolean(actionBusy) || !roundCanStop"
            @click="openAction(stopAllSessionsAction())"
          >
            <Square :size="18" />
            停止整轮网格
          </button>
        </div>
        <section class="surface grid-console">
          <div class="section-title">
            <Database :size="18" />
            <h3>启动轮次与标的网格</h3>
          </div>
          <div v-if="gridRounds.length" class="grid-round-bar">
            <label class="grid-round-select">
              <span>启动轮次</span>
              <select
                v-model.number="selectedGridRoundId"
                :disabled="loading || Boolean(actionBusy)"
                aria-label="选择启动轮次"
                @change="handleGridRoundChange"
              >
                <option v-for="round in gridRounds" :key="round.id" :value="round.id">
                  第{{ round.roundNumber }}轮 · {{ formatRoundOptionTime(round.startTime) }} · {{ round.sessionCount }} 标的
                </option>
              </select>
            </label>
            <div v-if="selectedGridRound" class="grid-round-summary" aria-live="polite">
              <div>
                <span>当前查看</span>
                <strong>第 {{ selectedGridRound.roundNumber }} 轮</strong>
              </div>
              <div>
                <span>启动时间</span>
                <strong>{{ selectedGridRound.startTime }}</strong>
              </div>
              <div>
                <span>轮次状态</span>
                <strong>{{ selectedGridRound.statusLabel }}</strong>
              </div>
              <div>
                <span>标的数量</span>
                <strong>{{ selectedGridRound.sessionCount }}</strong>
              </div>
            </div>
          </div>
          <div v-else class="empty-state">暂无已启动的网格轮次。</div>
          <div v-if="selectedGridRound && gridTabs.length === 0" class="empty-state">这一轮正在扫描，暂无候选标的数据。</div>
          <div v-else-if="gridTabs.length" class="grid-tabs" role="tablist" :aria-label="`第 ${selectedGridRound?.roundNumber ?? '-'} 轮网格标的`">
            <button
              v-for="tab in gridTabs"
              :key="tab.key"
              class="grid-tab"
              :class="{ active: selectedGridTab?.key === tab.key }"
              type="button"
              role="tab"
              :aria-selected="selectedGridTab?.key === tab.key"
              @click="selectedGridKey = tab.key"
            >
              <strong>{{ tab.symbol }}</strong>
              <span>{{ tab.stateLabel }}</span>
            </button>
          </div>

          <div v-if="selectedGridTab" class="grid-detail-panel">
            <div class="grid-title-row">
              <div>
                <p class="eyebrow">第 {{ selectedGridRound?.roundNumber ?? '-' }} 轮网格会话</p>
                <h3>{{ selectedGridTab.symbol }}</h3>
              </div>
              <div class="row-actions">
                <button
                  v-if="isCurrentRoundSelected && selectedGridSession && selectedGridSession.state !== 'STOPPED'"
                  class="compact-secondary"
                  type="button"
                  :disabled="Boolean(actionBusy) || selectedGridSession.controlRequested"
                  @click="openAction(sessionPauseToggleAction(selectedGridSession))"
                >
                  <CirclePause :size="16" />
                  {{ selectedGridSession.state === 'PAUSED' ? '恢复' : '暂停' }}
                </button>
                <button
                  v-if="isCurrentRoundSelected && selectedGridSession"
                  class="compact-danger"
                  type="button"
                  :disabled="Boolean(actionBusy) || !canStopSession(selectedGridSession)"
                  @click="openAction(stopSessionAction(selectedGridSession))"
                >
                  <Power :size="16" />
                  {{ selectedGridSession.stopRequested ? formatStopRequestStatus(selectedGridSession.stopRequestStatus, selectedGridSession.stopRequestType) : '结束' }}
                </button>
                <button
                  v-if="isCurrentRoundSelected && selectedGridSession"
                  class="compact-secondary"
                  :class="{ warning: !selectedGridSession.nextEntryDisabled }"
                  type="button"
                  :disabled="Boolean(actionBusy)"
                  @click="openAction(symbolToggleAction(selectedGridSession))"
                >
                  <Ban :size="16" />
                  {{ selectedGridSession.nextEntryDisabled ? '启用开仓' : '禁用开仓' }}
                </button>
              </div>
            </div>

            <div class="grid-stat-strip">
              <article class="market-price-card" :class="{ stale: selectedGridCandidate?.dataStale }">
                <span>实时价格</span>
                <strong>{{ selectedGridCandidate?.price ? formatNumber(selectedGridCandidate.price, 4) : '-' }}</strong>
                <small>
                  买 {{ selectedGridCandidate?.bidPrice ? formatNumber(selectedGridCandidate.bidPrice, 4) : '-' }} ·
                  卖 {{ selectedGridCandidate?.askPrice ? formatNumber(selectedGridCandidate.askPrice, 4) : '-' }}
                </small>
                <small>{{ selectedGridCandidate?.dataStale ? '行情已过期' : '行情实时' }} · {{ selectedGridCandidate?.marketUpdatedAt || '等待更新' }}</small>
              </article>
              <article>
                <span>总收益(USDT)</span>
                <strong>{{ selectedGridSession ? formatMoney(selectedGridSession.pnl) : '-' }}</strong>
              </article>
              <article>
                <span>当前挂单</span>
                <strong>{{ selectedGridSession?.openOrderCount ?? 0 }}</strong>
              </article>
              <article>
                <span>标的已交易次数</span>
                <strong>{{ selectedGridSession?.tradeCount ?? 0 }}</strong>
              </article>
              <article>
                <span>运行状态</span>
                <strong>{{ selectedGridSession ? (selectedGridSession.stateLabel || formatState(selectedGridSession.state)) : formatCandidateStage(selectedGridCandidate?.stage || 'scanning') }}</strong>
              </article>
            </div>

            <div class="stage-progress">
              <div>
                <span>波动计算阶段</span>
                <strong>{{ selectedGridSession?.volatilityStageLabel || formatCandidateStage(selectedGridCandidate?.stage || 'scanning') }}</strong>
              </div>
              <div class="progress-track" aria-label="波动计算进度">
                <span :style="{ width: `${Math.round((selectedGridSession?.volatilityProgressPct ?? 0) * 100)}%` }"></span>
              </div>
              <div class="stage-steps" aria-label="阶段状态">
                <div
                  v-for="step in selectedStageSteps"
                  :key="step.key"
                  class="stage-step"
                  :class="{ active: step.active, done: step.done }"
                >
                  <strong>{{ step.label }}</strong>
                  <small>{{ step.detail }}</small>
                </div>
              </div>
              <small>
                进度 {{ Math.round((selectedGridSession?.volatilityProgressPct ?? 0) * 100) }}% ·
                剩余 {{ formatSeconds(selectedGridSession?.volatilityRemainingSeconds ?? null) }}
              </small>
            </div>

            <div class="performance-grid">
              <article>
                <span>网格毛收益</span>
                <strong>{{ selectedPerformance ? formatSignedMoney(selectedPerformance.grossGridPnl) : '-' }}</strong>
              </article>
              <article>
                <span>手续费</span>
                <strong>{{ selectedPerformance ? formatMoney(selectedPerformance.tradingFees) : '-' }}</strong>
              </article>
              <article>
                <span>资金费用</span>
                <strong>{{ selectedPerformance ? formatSignedMoney(selectedPerformance.fundingFee) : '-' }}</strong>
              </article>
              <article>
                <span>未配对盈亏估算</span>
                <strong>{{ selectedPerformance ? formatSignedMoney(selectedPerformance.unpairedPnl) : '-' }}</strong>
              </article>
              <article>
                <span>收益率</span>
                <strong>{{ selectedPerformance ? formatNullableRoi(selectedPerformance.roi) : '-' }}</strong>
              </article>
              <article>
                <span>年化收益估算</span>
                <strong>{{ selectedPerformance ? formatNullableRoi(selectedPerformance.annualizedRoi) : '-' }}</strong>
              </article>
              <article>
                <span>初始保证金</span>
                <strong>{{ selectedPerformance ? formatMoney(selectedPerformance.initialMargin) : '-' }}</strong>
              </article>
              <article>
                <span>当前保证金估算</span>
                <strong>{{ selectedPerformance ? formatNullableMoney(selectedPerformance.currentMargin) : '-' }}</strong>
              </article>
              <article>
                <span>保证金变化</span>
                <strong>{{ selectedPerformance ? formatNullableSignedMoney(selectedPerformance.marginChange) : '-' }}</strong>
              </article>
            </div>

            <section class="subsurface wide">
              <div class="grid-title-row">
                <h4>PnL 曲线</h4>
                <span class="control-note">
                  {{ selectedPerformance ? `${selectedPerformance.tradeCount} 笔成交 · ${selectedPerformance.unpairedTradeCount} 笔未配对` : '暂无成交' }}
                </span>
              </div>
              <div v-if="selectedPnlPolyline" class="pnl-chart" role="img" aria-label="累计 PnL 曲线">
                <svg viewBox="0 0 100 44" preserveAspectRatio="none">
                  <line x1="0" y1="22" x2="100" y2="22" />
                  <polyline :points="selectedPnlPolyline" />
                </svg>
              </div>
              <div v-else class="empty-state">暂无可绘制的成交盈亏曲线。</div>
            </section>

            <section class="subsurface wide">
              <div class="grid-title-row">
                <h4>价格 / 网格图</h4>
                <span class="control-note">
                  {{ selectedGridChart ? `${formatNumber(selectedGridChart.minPrice, 4)} - ${formatNumber(selectedGridChart.maxPrice, 4)}` : '暂无网格区间' }}
                </span>
              </div>
              <div v-if="selectedGridChart" class="price-grid-chart" role="img" :aria-label="`${selectedGridTab.symbol} 价格和网格分布图`">
                <div class="price-grid-plot">
                  <span
                    v-for="line in selectedGridChart.lines"
                    :key="line.key"
                    class="price-level"
                    :class="{ boundary: line.boundary }"
                    :style="{ top: `${line.y}%` }"
                  >
                    <em v-if="line.showLabel">{{ formatNumber(line.price, 4) }}</em>
                  </span>
                  <span
                    v-if="selectedGridChart.stopY !== null"
                    class="price-stop-level"
                    :style="{ top: `${selectedGridChart.stopY}%` }"
                  >
                    <em>止损 {{ formatNumber(selectedGridChart.stopPrice, 4) }}</em>
                  </span>
                  <span
                    v-if="selectedGridChart.currentY !== null"
                    class="price-current-level"
                    :class="{ stale: selectedGridCandidate?.dataStale }"
                    :style="{ top: `${selectedGridChart.currentY}%` }"
                  >
                    <em>现价 {{ formatNumber(selectedGridChart.currentPrice ?? 0, 4) }}</em>
                  </span>
                  <span
                    v-for="(point, index) in selectedGridChart.orderPoints"
                    :key="`order-${index}`"
                    class="price-marker order-marker"
                    :class="point.side === 'BUY' ? 'buy-point' : 'sell-point'"
                    :style="{ left: `${point.x}%`, top: `${point.y}%` }"
                    :title="point.title"
                  ></span>
                  <span
                    v-for="(point, index) in selectedGridChart.tradePoints"
                    :key="`trade-${index}`"
                    class="price-marker trade-marker"
                    :class="point.side === 'BUY' ? 'buy-point' : 'sell-point'"
                    :style="{ left: `${point.x}%`, top: `${point.y}%` }"
                    :title="point.title"
                  ></span>
                </div>
                <div class="chart-legend">
                  <span><i class="legend-grid"></i>网格</span>
                  <span><i class="legend-stop"></i>止损</span>
                  <span><i class="legend-current"></i>实时价格</span>
                  <span><i class="legend-buy"></i>买入（绿）</span>
                  <span><i class="legend-sell"></i>卖出（红）</span>
                  <span><i class="legend-order"></i>挂单圆点</span>
                  <span><i class="legend-trade"></i>成交方块</span>
                </div>
              </div>
              <div v-else class="empty-state">暂无可绘制的网格区间。</div>
            </section>

            <div class="grid-info-layout">
              <section class="subsurface">
                <h4>网格参数</h4>
                <div class="info-list">
                  <div><span>价格区间</span><strong>{{ selectedGridSession ? `${formatNumber(selectedGridSession.lower, 4)} - ${formatNumber(selectedGridSession.upper, 4)}` : (selectedGridCandidate?.rangeLower && selectedGridCandidate?.rangeUpper ? `${formatNumber(selectedGridCandidate.rangeLower, 4)} - ${formatNumber(selectedGridCandidate.rangeUpper, 4)}` : '-') }}</strong></div>
                  <div><span>网格数量</span><strong>{{ selectedGridSession?.gridNum || '-' }}</strong></div>
                  <div><span>网格间距</span><strong>{{ selectedGridSession ? formatPct(selectedGridSession.stepPct) : '-' }}</strong></div>
                  <div><span>计算方法</span><strong>{{ selectedGridSession ? (selectedGridSession.volatilityMethodLabel || formatVolatilityMethod(selectedGridSession.volatilityMethod)) : (selectedGridCandidate?.volatilityMethodLabel || formatVolatilityMethod(strategyConfig.current.volatilityMethod)) }}</strong></div>
                  <div><span>初始波动率</span><strong>{{ selectedGridSession?.volatilityValue ? formatPct(selectedGridSession.volatilityValue) : formatNullablePct(selectedGridCandidate?.volatilityValue ?? null) }}</strong></div>
                  <div><span>当前波动率</span><strong>{{ selectedGridSession?.currentVolatility ? formatPct(selectedGridSession.currentVolatility) : formatNullablePct(selectedGridCandidate?.currentVolatility ?? null) }}</strong></div>
                  <div><span>历史回看窗口</span><strong>{{ selectedGridSession?.volatilityWindow ? `${selectedGridSession.volatilityWindow} 根K线` : (selectedGridCandidate?.volatilityWindow ? `${selectedGridCandidate.volatilityWindow} 根K线` : '-') }}</strong></div>
                  <div><span>资金 / 杠杆</span><strong>{{ selectedGridSession ? `${formatNumber(selectedGridSession.capital, 2)} USDT / ${selectedGridSession.leverage}x` : '-' }}</strong></div>
                  <div><span>止损价格</span><strong>{{ selectedGridSession?.stopLossPrice ? formatNumber(selectedGridSession.stopLossPrice, 4) : '-' }}</strong></div>
                  <div><span>开仓时间</span><strong>{{ selectedGridSession?.openTime || '-' }}</strong></div>
                  <div v-if="!selectedGridSession"><span>行情 / K线</span><strong>{{ selectedGridCandidate?.marketUpdatedAt || '-' }} / {{ selectedGridCandidate?.lastKlineCloseAt || '-' }}</strong></div>
                </div>
              </section>

              <section class="subsurface">
                <h4>当前挂单</h4>
                <div class="mini-table" role="table" aria-label="当前挂单">
                  <div class="mini-row mini-head" role="row">
                    <span>档位</span>
                    <span>方向</span>
                    <span>价格</span>
                    <span>数量</span>
                    <span>状态</span>
                  </div>
                  <div v-for="order in selectedOpenOrders" :key="order.id" class="mini-row" role="row">
                    <span>{{ order.gridIndex || '-' }}</span>
                    <strong>{{ order.sideLabel }}</strong>
                    <span>{{ formatNumber(order.price, 4) }}</span>
                    <span>{{ formatNumber(order.qty, 6) }}</span>
                    <span>{{ order.statusLabel }}</span>
                  </div>
                  <div v-if="selectedOpenOrders.length === 0" class="empty-state">暂无挂单。</div>
                </div>
              </section>

              <section class="subsurface">
                <h4>历史订单</h4>
                <div class="mini-table" role="table" aria-label="历史订单">
                  <div class="mini-row mini-head" role="row">
                    <span>档位</span>
                    <span>方向</span>
                    <span>价格</span>
                    <span>数量</span>
                    <span>状态</span>
                  </div>
                  <div v-for="order in selectedHistoryOrders" :key="order.id" class="mini-row" role="row">
                    <span>{{ order.gridIndex || '-' }}</span>
                    <strong>{{ order.sideLabel }}</strong>
                    <span>{{ formatNumber(order.price, 4) }}</span>
                    <span>{{ formatNumber(order.qty, 6) }}</span>
                    <span>{{ order.statusLabel }}</span>
                  </div>
                  <div v-if="selectedHistoryOrders.length === 0" class="empty-state">暂无历史订单。</div>
                </div>
              </section>

              <section class="subsurface">
                <h4>{{ selectedGridSession?.position.status === 'historical' ? '历史轮次持仓' : '实时持仓' }}</h4>
                <div class="info-list">
                  <div><span>读取状态</span><strong>{{ selectedGridSession?.position.status || '-' }}</strong></div>
                  <div><span>净持仓数量</span><strong>{{ formatNumber(selectedGridSession?.position.qty ?? 0, 6) }}</strong></div>
                  <div><span>多仓 / 空仓</span><strong>{{ formatNumber(selectedGridSession?.position.longQty ?? 0, 6) }} / {{ formatNumber(selectedGridSession?.position.shortQty ?? 0, 6) }}</strong></div>
                  <div><span>入场价 / 标记价</span><strong>{{ formatNumber(selectedGridSession?.position.entryPrice ?? 0, 4) }} / {{ formatNumber(selectedGridSession?.position.markPrice ?? 0, 4) }}</strong></div>
                  <div><span>未实现盈亏</span><strong>{{ formatNullableSignedMoney(selectedGridSession?.position.unrealizedPnl ?? null) }}</strong></div>
                  <div><span>名义价值</span><strong>{{ formatNullableMoney(selectedGridSession?.position.notional ?? null) }}</strong></div>
                </div>
                <p v-if="selectedGridSession?.position.error" class="form-error" role="alert">{{ selectedGridSession.position.error }}</p>
              </section>

              <section class="subsurface wide">
                <h4>最近成交</h4>
                <div class="mini-table trades" role="table" aria-label="最近成交">
                  <div class="mini-row mini-head" role="row">
                    <span>时间</span>
                    <span>方向</span>
                    <span>价格</span>
                    <span>数量</span>
                    <span>单格盈亏</span>
                    <span>手续费</span>
                    <span>资金费</span>
                  </div>
                  <div v-for="trade in selectedRecentTrades" :key="trade.id" class="mini-row" role="row">
                    <span>{{ trade.tradeTime }}</span>
                    <strong>{{ trade.sideLabel }}</strong>
                    <span>{{ formatNumber(trade.price, 4) }}</span>
                    <span>{{ formatNumber(trade.qty, 6) }}</span>
                    <span>{{ formatMoney(trade.gridPnl) }}</span>
                    <span>{{ formatMoney(trade.fee) }}</span>
                    <span>{{ formatSignedMoney(trade.fundingFee) }}</span>
                  </div>
                  <div v-if="selectedRecentTrades.length === 0" class="empty-state">暂无成交记录。</div>
                </div>
              </section>
            </div>

            <div v-if="selectedGridSession" class="grid-secondary-actions">
              <button
                class="danger-button"
                type="button"
                :disabled="Boolean(actionBusy) || !canManualCloseSession(selectedGridSession)"
                @click="openAction(manualCloseSessionAction(selectedGridSession))"
              >
                <Trash2 :size="18" />
                手动平仓
              </button>
              <span class="control-note">
                {{ selectedGridSession.closeReason ? `最近关闭原因：${selectedGridSession.closeReason}` : '停止或平仓请求会由交易循环下一轮执行。' }}
              </span>
            </div>
          </div>
        </section>
      </section>

      <section v-if="activeTab === 'strategy'" class="panel-stack">
        <section class="surface form-grid">
          <div class="section-title wide">
            <SlidersHorizontal :size="18" />
            <h3>策略与风控参数</h3>
          </div>
          <div class="config-summary wide">
            <div>
              <span>启动配置基线</span>
              <strong>{{ formatVolatilityMethod(strategyConfig.current.volatilityMethod) }}</strong>
              <small>
                杠杆 {{ strategyConfig.current.leverage }}x · 本金 {{ strategyConfig.current.capitalPerSymbol.toFixed(2) }} USDT ·
                并发 {{ strategyConfig.current.maxConcurrent }} · 历史回看 {{ strategyConfig.current.observeHours }} 小时 ·
                止盈 {{ strategyConfig.current.takeProfitUsdt.toFixed(2) }} USDT
              </small>
            </div>
            <div>
              <span>草稿更新时间</span>
              <strong>{{ strategyConfig.draftUpdatedAt }}</strong>
              <small>{{ strategyConfig.diff.length ? `有 ${strategyConfig.diff.length} 项参数变更` : '草稿与当前配置一致' }}</small>
            </div>
          </div>
          <label>
            <span>波动率算法</span>
            <select v-model="strategyForm.volatilityMethod">
              <option v-for="option in strategyConfig.volatilityOptions" :key="option.value" :value="option.value">
                {{ option.label || formatVolatilityMethod(option.value) }}
              </option>
            </select>
          </label>
          <label>
            <span>杠杆倍数</span>
            <input v-model.number="strategyForm.leverage" type="number" min="1" max="125" step="1" />
          </label>
          <label>
            <span>单标的本金 USDT</span>
            <input v-model.number="strategyForm.capitalPerSymbol" type="number" min="1" max="10000000" step="1" />
          </label>
          <label>
            <span>最大并发标的</span>
            <input v-model.number="strategyForm.maxConcurrent" type="number" min="1" max="10" />
          </label>
          <label>
            <span>每轮流动性扫描数</span>
            <input v-model.number="strategyForm.scanCandidateCount" type="number" min="1" max="100" />
          </label>
          <label>
            <span>历史回看窗口（小时）</span>
            <input v-model.number="strategyForm.observeHours" type="number" min="0.1" max="24" step="0.1" />
          </label>
          <label>
            <span>K线周期</span>
            <select v-model="strategyForm.observeKlineInterval">
              <option value="1m">1m</option>
              <option value="3m">3m</option>
              <option value="5m">5m</option>
              <option value="15m">15m</option>
              <option value="30m">30m</option>
              <option value="1h">1h</option>
            </select>
          </label>
          <label>
            <span>最小网格步长</span>
            <input v-model.number="strategyForm.minStepPct" type="number" min="0.0001" max="0.05" step="0.0001" />
          </label>
          <label>
            <span>最小可交易波动区间</span>
            <input v-model.number="strategyForm.minTradableRangePct" type="number" min="0.0001" max="0.05" step="0.0001" />
          </label>
          <label>
            <span>最大网格数量</span>
            <input v-model.number="strategyForm.maxGridNum" type="number" min="1" max="200" />
          </label>
          <label>
            <span>止损缓冲</span>
            <input v-model.number="strategyForm.stopBufferPct" type="number" min="0" max="0.99" step="0.001" />
          </label>
          <label>
            <span>资金费安全倍数</span>
            <input v-model.number="strategyForm.safetyMultiplier" type="number" min="0" max="100" step="0.1" />
          </label>
          <label>
            <span>单标的止盈 USDT</span>
            <input v-model.number="strategyForm.takeProfitUsdt" type="number" min="0.01" max="100000" step="0.01" />
          </label>
          <label>
            <span>总资金上限 USDT</span>
            <input v-model.number="strategyForm.totalCapitalLimit" type="number" min="1" max="10000000" step="1" />
          </label>
          <label>
            <span>Maker 费率上限</span>
            <input v-model.number="strategyForm.maxMakerFeeRate" type="number" min="0" max="0.01" step="0.0001" />
          </label>
          <div class="empty-state wide">
            本金、杠杆、止盈、并发、总资金上限和 Maker 费率上限会在交易循环下一次轮询时热加载；波动率算法、K线周期、历史回看窗口、止损缓冲和网格参数用于下一轮新建网格。历史回看窗口用于启动时立即拉取既有 K 线，不代表需要等待对应时长。
          </div>
          <div class="draft-diff wide" aria-live="polite">
            <div v-if="strategyConfig.diff.length === 0" class="empty-state">当前草稿与运行配置一致。</div>
            <div v-for="item in strategyConfig.diff" :key="item.key" class="diff-row">
              <span>{{ item.label }}</span>
              <strong>{{ formatStrategyValue(item.key, item.current) }} -> {{ formatStrategyValue(item.key, item.draft) }}</strong>
            </div>
          </div>
          <p v-if="strategyError" class="form-error wide" role="alert">{{ strategyError }}</p>
          <button class="secondary-button" type="button" :disabled="strategyBusy" @click="resetStrategyDraft">
            恢复当前配置
          </button>
          <button class="primary-button" type="button" :disabled="strategyBusy" @click="saveStrategyDraft">
            <CheckCircle2 :size="18" />
            {{ strategyBusy ? '保存中' : '保存参数草稿' }}
          </button>
        </section>
      </section>

      <section v-if="activeTab === 'environment'" class="panel-stack">
        <section class="surface">
          <div class="section-title">
            <Power :size="18" />
            <h3>交易 loop 进程</h3>
          </div>
          <div class="environment-summary process-summary">
            <div>
              <span>控制模式</span>
              <strong>{{ traderProcessState.mode }}</strong>
            </div>
            <div>
              <span>服务名</span>
              <strong>{{ traderProcessState.service }}</strong>
            </div>
            <div>
              <span>进程状态</span>
              <strong>{{ traderProcessState.state }}</strong>
            </div>
            <div>
              <span>可用性</span>
              <strong>{{ traderProcessState.available ? '可控制' : '不可用' }}</strong>
            </div>
          </div>
          <p class="control-note">{{ traderProcessState.detail || '运维层控制后台交易服务。' }}</p>
          <div class="row-actions">
            <button
              class="danger-button"
              type="button"
              :disabled="Boolean(actionBusy) || !traderProcessState.available"
              @click="openAction(traderLoopAction('stop'))"
            >
              <Square :size="18" />
              停止交易进程
            </button>
            <button
              class="secondary-button"
              type="button"
              :disabled="Boolean(actionBusy) || !traderProcessState.available"
              @click="openAction(traderLoopAction('restart'))"
            >
              <RefreshCw :size="18" />
              重启交易进程
            </button>
          </div>
        </section>

        <section class="surface">
          <div class="section-title">
            <ShieldCheck :size="18" />
            <h3>当前连接环境只读验证</h3>
          </div>
          <div class="environment-summary">
            <div><span>连接环境</span><strong>{{ summary.mode }}</strong></div>
            <div><span>账户</span><strong>{{ summary.accountLabel }}</strong></div>
            <div><span>可用资金</span><strong>{{ formatNullableMoney(summary.availableBalance) }}</strong></div>
            <div><span>当前暴露</span><strong>{{ formatNullableMoney(summary.currentExposure) }}</strong></div>
          </div>
          <p class="control-note">本验证只调用连接、交易所元数据、账户摘要和资金等只读接口，不会产生任何交易写操作。</p>
          <div class="verification-list">
            <div v-for="row in verificationRows" :key="row.module" class="verification-row">
              <CheckCircle2 :size="18" />
              <div>
                <strong>{{ row.name }}</strong>
                <span>{{ row.detail }}</span>
                <small>{{ row.status }} · {{ row.lastChecked }}</small>
              </div>
            </div>
          </div>
          <div class="row-actions">
            <button class="primary-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(verifyEnvironmentAction())">
              <ShieldCheck :size="18" />
              验证当前环境
            </button>
          </div>
        </section>
      </section>

      <section v-if="activeTab === 'logs'" class="panel-stack">
        <section class="surface">
          <div class="section-title">
            <AlertTriangle :size="18" />
            <h3>最近审计日志</h3>
          </div>
          <div class="audit-list">
            <div v-for="log in auditLogs" :key="`${log.time}-${log.module}`" class="audit-row">
              <span>{{ log.time }}</span>
              <strong>{{ formatAuditModule(log.module) }}</strong>
              <p>{{ log.message }}</p>
            </div>
            <div v-if="auditLogs.length === 0" class="empty-state">暂无系统日志。</div>
          </div>
        </section>
      </section>
    </section>

    <div v-if="actionMessage || actionError" class="toast" :class="{ error: actionError }" role="status">
      {{ actionError || actionMessage }}
    </div>

    <div v-if="pendingAction" class="modal-backdrop" role="presentation" @click.self="closeAction">
      <section class="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <div>
          <p class="eyebrow">控制动作确认</p>
          <h3 id="confirm-title">{{ pendingAction.title }}</h3>
          <p class="muted">{{ pendingAction.description }}</p>
        </div>
        <label>
          <span>操作原因</span>
          <textarea v-model="actionReason" rows="3" maxlength="200" />
        </label>
        <div class="modal-actions">
          <button class="secondary-button" type="button" :disabled="Boolean(actionBusy)" @click="closeAction">取消</button>
          <button
            :class="pendingAction.tone === 'danger' ? 'danger-button' : pendingAction.tone === 'primary' ? 'primary-button' : 'secondary-button'"
            type="button"
            :disabled="Boolean(actionBusy)"
            @click="confirmAction"
          >
            <RefreshCw v-if="actionBusy === pendingAction.action" :size="18" class="spinning" />
            <CheckCircle2 v-else :size="18" />
            {{ actionBusy === pendingAction.action ? '执行中' : pendingAction.buttonLabel }}
          </button>
        </div>
      </section>
    </div>
  </main>
</template>
