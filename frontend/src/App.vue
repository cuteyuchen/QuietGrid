<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  Clapperboard,
  ClipboardList,
  FlaskConical,
  Grid3X3,
  Layers3,
  LayoutDashboard,
  Menu,
  Milestone,
  Radar,
  RefreshCw,
  ServerCog,
  Settings2,
  ShieldAlert,
  Wifi,
  WifiOff,
  X,
} from '@lucide/vue'
import {
  consoleEventsUrl,
  executeConsoleAction,
  executeV2Command,
  getCurrentRound,
  loadAccounts,
  loadConsoleData,
  loadV2Dashboard,
  saveStrategyConfigDraft,
  type ConsoleAction,
  type AutoTradingUiState,
  type CurrentRoundSnapshot,
  type V2CommandType,
  type V2DashboardData,
} from './api'
import {
  accounts as fallbackAccounts,
  auditLogs as fallbackAuditLogs,
  controlState as fallbackControlState,
  liquidityCandidates as fallbackLiquidityCandidates,
  sessions as fallbackSessions,
  strategyConfig as fallbackStrategyConfig,
  summary as fallbackSummary,
  traderProcessState as fallbackTraderProcessState,
  verificationRows as fallbackVerificationRows,
  type AccountOption,
  type GridSession,
  type StrategySettings,
} from './mock'
import ConfirmDialog from './components/ConfirmDialog.vue'
import StatusBadge from './components/StatusBadge.vue'
import BacktestsPage from './pages/BacktestsPage.vue'
import CurrentRoundPage from './pages/CurrentRoundPage.vue'
import DashboardPage from './pages/DashboardPage.vue'
import MarketPage from './pages/MarketPage.vue'
import OperationsPage from './pages/OperationsPage.vue'
import RecordsPage from './pages/RecordsPage.vue'
import ReplayPage from './pages/ReplayPage.vue'
import RiskPage from './pages/RiskPage.vue'
import SessionsPage from './pages/SessionsPage.vue'
import SettingsPage from './pages/SettingsPage.vue'

type PageKey =
  | 'current-round'
  | 'dashboard'
  | 'market'
  | 'sessions'
  | 'risk'
  | 'backtests'
  | 'replay'
  | 'records'
  | 'settings'
  | 'operations'

type PendingAction = {
  key: string
  title: string
  description: string
  confirmationText: string
  danger: boolean
  session?: GridSession
}

const navigation = [
  {
    label: '运行',
    items: [
      { key: 'current-round', label: '本轮运行', description: '生命周期主线', icon: Milestone },
      { key: 'dashboard', label: '总览', description: '安全状态与下一步', icon: LayoutDashboard },
      { key: 'market', label: '市场状态', description: '评分与候选标的', icon: Radar },
      { key: 'sessions', label: '会话与库存', description: '网格、订单与持仓', icon: Layers3 },
      { key: 'risk', label: '风险中心', description: '预算、熔断与恢复', icon: ShieldAlert },
    ],
  },
  {
    label: '研究',
    items: [
      { key: 'backtests', label: '回测中心', description: '实验、报告与验证', icon: FlaskConical },
      { key: 'replay', label: '策略重放', description: '逐事件检查决策', icon: Clapperboard },
    ],
  },
  {
    label: '系统',
    items: [
      { key: 'records', label: '订单与日志', description: '成交与审计记录', icon: ClipboardList },
      { key: 'settings', label: '策略设置', description: '草稿与版本差异', icon: Settings2 },
      { key: 'operations', label: '运维工具', description: '环境、进程与清扫', icon: ServerCog },
    ],
  },
] as const

const pageDescriptions: Record<PageKey, { title: string; subtitle: string }> = {
  'current-round': { title: '本轮运行', subtitle: '一眼看清系统当前处于哪个阶段' },
  dashboard: { title: '运行总览', subtitle: '先看安全，再决定下一步' },
  market: { title: '市场状态', subtitle: '用可解释评分判断是否适合网格' },
  sessions: { title: '会话与库存', subtitle: '查看网格、真实订单、成交和库存风险' },
  risk: { title: '风险中心', subtitle: '预算、熔断、冷却和恢复条件' },
  backtests: { title: '回测中心', subtitle: '验证策略，不美化历史收益' },
  replay: { title: '策略重放', subtitle: '沿时间线复盘系统每个决定' },
  records: { title: '订单与日志', subtitle: '交易记录、差异和审计证据' },
  settings: { title: '策略设置', subtitle: '参数草稿、版本差异与生效边界' },
  operations: { title: '运维工具', subtitle: '连接检查、交易进程和安全清扫' },
}

const activePage = ref<PageKey>(pageFromHash())
const sidebarOpen = ref(false)
const initialLoading = ref(true)
const refreshing = ref(false)
const dataError = ref('')
const actionMessage = ref('')
const actionError = ref('')
const actionBusy = ref(false)
const autoTradingTransition = ref<'STARTING' | 'STOPPING' | ''>('')
const strategyBusy = ref(false)
const strategyError = ref('')
const pendingAction = ref<PendingAction | null>(null)
const realtimeConnected = ref(false)
const realtimeError = ref('')
const selectedAccountId = ref(window.localStorage.getItem('quietgrid.accountId') || '')
const accountOptions = ref<AccountOption[]>(fallbackAccounts)
const selectedGridRoundId = ref<number | null>(null)

const summary = ref(fallbackSummary)
const controlState = ref(fallbackControlState)
const traderProcessState = ref(fallbackTraderProcessState)
const strategyConfig = ref(fallbackStrategyConfig)
const sessions = ref(fallbackSessions)
const liquidityCandidates = ref(fallbackLiquidityCandidates)
const verificationRows = ref(fallbackVerificationRows)
const auditLogs = ref(fallbackAuditLogs)
const v2Dashboard = ref<V2DashboardData>({
  environment: 'testnet',
  traderStatus: 'IDLE',
  accountId: 'default',
  equity: 0,
  availableBalance: null,
  currentExposure: null,
  windowId: null,
  windowPnl: 0,
  windowLossBudget: 0,
  windowLossBudgetRemaining: 0,
  windowStopCount: 0,
  activeSessions: 0,
  openOrders: 0,
  globalRiskLevel: 'LOW',
  dataHealth: 'WAITING',
  latestRegime: null,
  latestInventory: null,
  latestRisk: null,
  riskPolicy: {
    effective_leverage_cap: 1,
    max_session_loss_pct: 0,
    max_weekend_loss_pct: 0,
    max_symbol_inventory_pct: 0,
    max_group_notional_pct: 0,
    max_consecutive_session_losses: 0,
    max_window_stop_count: 0,
    block_risk_increase_hot_reload: true,
  },
})
const currentRound = ref<CurrentRoundSnapshot | null>(null)
const autoTradingState = computed<AutoTradingUiState>(() => {
  const current = currentRound.value?.autoTrading
  const enabled = Boolean(current?.enabled)
  const transitioning = Boolean(autoTradingTransition.value)
  return {
    enabled,
    transitioning,
    transitionState: autoTradingTransition.value || current?.transitionState || (enabled ? 'ENABLED' : 'DISABLED'),
    canStart: !transitioning && Boolean(current?.canStart ?? !enabled),
    canStop: !transitioning && Boolean(current?.canStop ?? enabled),
    blockedReason: current?.blockedReason || '',
    mode: current?.mode,
    requestId: current?.requestId,
  }
})

const activePageMeta = computed(() => pageDescriptions[activePage.value])
const currentAccount = computed(() => accountOptions.value.find((item) => item.id === selectedAccountId.value))
const isLiveEnvironment = computed(() => {
  const environment = `${v2Dashboard.value.environment} ${summary.value.mode}`.toLowerCase()
  return !environment.includes('test') && !environment.includes('测试')
})
const riskTone = computed(() => {
  const level = v2Dashboard.value.globalRiskLevel.toUpperCase()
  if (['CRITICAL', 'EMERGENCY'].includes(level)) return 'danger'
  if (['HIGH', 'MEDIUM', 'CAUTION'].includes(level)) return 'warning'
  return 'good'
})
const dataTone = computed(() => {
  const value = v2Dashboard.value.dataHealth.toUpperCase()
  return ['STALE', 'ERROR', 'UNHEALTHY', 'DISCONNECTED'].includes(value) ? 'danger' : value === 'WAITING' ? 'warning' : 'good'
})
const activeComponent = computed(() => ({
  'current-round': CurrentRoundPage,
  dashboard: DashboardPage,
  market: MarketPage,
  sessions: SessionsPage,
  risk: RiskPage,
  backtests: BacktestsPage,
  replay: ReplayPage,
  records: RecordsPage,
  settings: SettingsPage,
  operations: OperationsPage,
}[activePage.value]))

let refreshTimer: number | undefined
let eventSource: EventSource | undefined
let eventRefreshTimer: number | undefined
let hasLoadedData = false

function pageFromHash(): PageKey {
  const value = window.location.hash.replace(/^#\/?/, '') as PageKey
  return pageDescriptions[value] ? value : 'dashboard'
}

function navigate(page: string) {
  const normalized = page as PageKey
  if (!pageDescriptions[normalized]) return
  activePage.value = normalized
  window.location.hash = `/${normalized}`
  sidebarOpen.value = false
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function refreshData(showInitial = false) {
  if (refreshing.value) return
  refreshing.value = true
  if (showInitial) initialLoading.value = true
  try {
    const accountsData = await loadAccounts()
    accountOptions.value = accountsData.accounts.length ? accountsData.accounts : fallbackAccounts
    if (!selectedAccountId.value || !accountOptions.value.some((item) => item.id === selectedAccountId.value)) {
      selectedAccountId.value = accountsData.currentAccountId
    }
    const [legacy, dashboard, roundSnapshot] = await Promise.all([
      loadConsoleData(selectedAccountId.value, selectedGridRoundId.value ?? undefined),
      loadV2Dashboard(selectedAccountId.value),
      getCurrentRound(selectedAccountId.value).catch(() => null),
    ])
    summary.value = legacy.summary
    controlState.value = legacy.controlState
    traderProcessState.value = legacy.traderProcessState
    strategyConfig.value = legacy.strategyConfig
    sessions.value = legacy.sessions
    liquidityCandidates.value = legacy.liquidityCandidates
    verificationRows.value = legacy.verificationRows
    auditLogs.value = legacy.auditLogs
    selectedGridRoundId.value = legacy.selectedGridRoundId
    v2Dashboard.value = dashboard
    if (roundSnapshot) {
      currentRound.value = roundSnapshot
      traderProcessState.value = roundSnapshot.trader
      if (roundSnapshot.round.state) {
        controlState.value = {
          ...controlState.value,
          roundState: roundSnapshot.round.state,
          nextScanAt: roundSnapshot.round.nextScanAt || controlState.value.nextScanAt,
          lastScanAt: roundSnapshot.round.lastScanAt || controlState.value.lastScanAt,
          currentRoundId: roundSnapshot.round.roundId ?? controlState.value.currentRoundId,
          roundStartRequest: roundSnapshot.round.startRequest || controlState.value.roundStartRequest,
        }
      }
      if (roundSnapshot.candidates.length) {
        liquidityCandidates.value = roundSnapshot.candidates
      }
    }
    selectedAccountId.value = legacy.summary.accountId
    window.localStorage.setItem('quietgrid.accountId', selectedAccountId.value)
    hasLoadedData = true
    dataError.value = ''
    if (realtimeConnected.value) {
      realtimeError.value = ''
    }
    scheduleStartingPoll()
  } catch (reason) {
    const message = reason instanceof Error ? reason.message : '无法连接 QuietGrid API'
    if (hasLoadedData) {
      realtimeError.value = `刷新失败，继续显示最近数据：${message}`
    } else {
      dataError.value = message
    }
  } finally {
    refreshing.value = false
    initialLoading.value = false
  }
}

let startingPollTimer: number | undefined

function scheduleStartingPoll() {
  const state = (traderProcessState.value.processState || traderProcessState.value.state || '').toUpperCase()
  const starting = state === 'STARTING' || state === 'START'
  if (!starting) {
    if (startingPollTimer != null) {
      window.clearTimeout(startingPollTimer)
      startingPollTimer = undefined
    }
    return
  }
  if (startingPollTimer != null) return
  startingPollTimer = window.setTimeout(() => {
    startingPollTimer = undefined
    void refreshData()
  }, 1500)
}

function handleAccountChange() {
  window.localStorage.setItem('quietgrid.accountId', selectedAccountId.value)
  selectedGridRoundId.value = null
  closeEventStream()
  startEventStream()
  void refreshData(true)
}

function startEventStream() {
  closeEventStream()
  const source = new EventSource(consoleEventsUrl(selectedAccountId.value))
  eventSource = source
  source.onopen = () => {
    realtimeConnected.value = true
    realtimeError.value = ''
  }
  const scheduleRefresh = () => {
    if (eventRefreshTimer != null) return
    eventRefreshTimer = window.setTimeout(() => {
      eventRefreshTimer = undefined
      void refreshData()
    }, 350)
  }
  for (const eventName of ['runtime', 'market', 'session', 'risk', 'inventory', 'command']) {
    source.addEventListener(eventName, scheduleRefresh)
  }
  source.onerror = () => {
    realtimeConnected.value = false
    realtimeError.value = '实时事件流正在重连'
  }
}

function closeEventStream() {
  eventSource?.close()
  eventSource = undefined
  realtimeConnected.value = false
  if (eventRefreshTimer != null) {
    window.clearTimeout(eventRefreshTimer)
    eventRefreshTimer = undefined
  }
}

function requestAction(key: string, session?: GridSession) {
  const symbol = session?.symbol || ''
  const actions: Record<string, PendingAction> = {
    pause: {
      key,
      title: '暂停所有新开仓',
      description: '现有会话仍会继续风控与退出；恢复时交易进程会重新检查全部条件。',
      confirmationText: 'PAUSE',
      danger: false,
    },
    resume: {
      key,
      title: '请求恢复新开仓',
      description: '提交不代表一定恢复；风险预算、数据、Regime 与库存检查任一失败都会拒绝命令。',
      confirmationText: 'RESUME',
      danger: false,
    },
    'start-round': {
      key,
      title: '启动下一轮扫描',
      description: '系统将先扫描候选标的，再经过市场状态和风险检查。网页不会直接下单。',
      confirmationText: 'START-ROUND',
      danger: false,
    },
    'stop-all': {
      key,
      title: '请求关闭全部会话',
      description: '交易进程将撤单、平仓并核对残留。这可能产生滑点和已实现亏损。',
      confirmationText: 'STOP-ALL',
      danger: true,
    },
    'safety-sweep': {
      key,
      title: '执行安全清扫',
      description: '系统将检查并清理残留订单与持仓。请仅在明确需要收尾时执行。',
      confirmationText: 'SAFETY-SWEEP',
      danger: true,
    },
    'close-session': {
      key,
      title: `请求关闭 ${symbol}`,
      description: `会话 #${session?.id || ''} 将由交易进程执行撤单、平仓和对账。`,
      confirmationText: `CLOSE-${symbol}`,
      danger: true,
      session,
    },
    verify: {
      key,
      title: '运行只读环境验证',
      description: '只检查连接、账户、费率和余额，不会发单。',
      confirmationText: 'VERIFY',
      danger: false,
    },
    'trader-start': {
      key,
      title: '启动交易进程',
      description: '将在本地启动 trader.py --binance-loop，并等待首个心跳。不会直接下单。',
      confirmationText: 'START-TRADER',
      danger: false,
    },
    'trader-restart': {
      key,
      title: '请求重启交易循环',
      description: '服务恢复后会先执行重启恢复和交易所对账。',
      confirmationText: 'RESTART-TRADER',
      danger: false,
    },
    'trader-stop': {
      key,
      title: '停止交易循环',
      description: '停止进程循环不等于自动平仓。若需要清仓，请使用关闭全部会话或安全清扫。',
      confirmationText: 'STOP-TRADER',
      danger: true,
    },
    'auto-trading-start': {
      key,
      title: '启动自动交易',
      description: '开启自动交易总开关；Trader 离线时会尝试启动进程，并在有效周末/节假日窗口自动评估。',
      confirmationText: 'START-AUTO',
      danger: false,
    },
    'auto-trading-stop': {
      key,
      title: '停止自动交易',
      description: '关闭未来新轮次。不会自动平仓。停止自动交易 ≠ 立即平仓。',
      confirmationText: 'STOP-AUTO',
      danger: false,
    },
  }
  pendingAction.value = actions[key] || null
  actionError.value = ''
  actionMessage.value = ''
}

async function confirmAction(reason: string) {
  const action = pendingAction.value
  if (!action || actionBusy.value) return
  actionBusy.value = true
  if (action.key === 'auto-trading-start') autoTradingTransition.value = 'STARTING'
  if (action.key === 'auto-trading-stop') autoTradingTransition.value = 'STOPPING'
  actionError.value = ''
  actionMessage.value = ''
  try {
    const v2Map: Partial<Record<string, V2CommandType>> = {
      pause: 'pause',
      resume: 'resume',
      'stop-all': 'stop-all',
      'safety-sweep': 'safety-sweep',
      'close-session': 'close-session',
    }
    const v2Command = v2Map[action.key]
    if (v2Command) {
      const result = await executeV2Command(v2Command, {
        accountId: selectedAccountId.value,
        reason,
        confirmation: action.confirmationText,
        sessionId: action.session?.id,
      })
      actionMessage.value = `命令 ${result.command_id} 已进入队列，当前状态：${result.status}`
    } else {
      const legacyMap: Record<string, ConsoleAction> = {
        'start-round': 'grid-round-start',
        verify: 'environment-verify-readonly',
        'trader-start': 'trader-loop-start',
        'trader-restart': 'trader-loop-restart',
        'trader-stop': 'trader-loop-stop',
        'auto-trading-start': 'auto-trading-start',
        'auto-trading-stop': 'auto-trading-stop',
      }
      const legacyAction = legacyMap[action.key]
      if (!legacyAction) throw new Error('不支持的控制操作')
      const result = await executeConsoleAction(legacyAction, {
        accountId: selectedAccountId.value,
        reason,
      })
      actionMessage.value = result.message
    }
    pendingAction.value = null
    await refreshData()
  } catch (reasonValue) {
    actionError.value = reasonValue instanceof Error ? reasonValue.message : '操作失败'
  } finally {
    actionBusy.value = false
    autoTradingTransition.value = ''
  }
}

async function saveStrategy(draft: StrategySettings) {
  if (strategyBusy.value) return
  strategyBusy.value = true
  strategyError.value = ''
  actionMessage.value = ''
  try {
    const result = await saveStrategyConfigDraft(draft, selectedAccountId.value)
    strategyConfig.value = result.config
    actionMessage.value = result.message
    await refreshData()
  } catch (reason) {
    strategyError.value = reason instanceof Error ? reason.message : '保存策略草稿失败'
  } finally {
    strategyBusy.value = false
  }
}

function componentProps() {
  switch (activePage.value) {
    case 'current-round':
      return {
        summary: summary.value,
        dashboard: v2Dashboard.value,
        control: controlState.value,
        sessions: sessions.value,
        candidates: liquidityCandidates.value,
        currentRound: currentRound.value,
        autoTrading: autoTradingState.value,
        actionBusy: actionBusy.value,
        traderProcess: traderProcessState.value,
        loading: initialLoading.value,
        dataError: dataError.value,
      }
    case 'dashboard':
      return {
        summary: summary.value,
        dashboard: v2Dashboard.value,
        control: controlState.value,
        sessions: sessions.value,
        autoTrading: autoTradingState.value,
        actionBusy: actionBusy.value,
        loading: refreshing.value,
        dataError: dataError.value,
      }
    case 'market':
      return {
        accountId: selectedAccountId.value,
        dashboard: v2Dashboard.value,
        candidates: liquidityCandidates.value,
      }
    case 'sessions':
      return {
        accountId: selectedAccountId.value,
        sessions: sessions.value,
        dashboard: v2Dashboard.value,
      }
    case 'risk':
      return { dashboard: v2Dashboard.value, control: controlState.value }
    case 'backtests':
      return { accountId: selectedAccountId.value }
    case 'replay':
      return { accountId: selectedAccountId.value, sessions: sessions.value }
    case 'records':
      return {
        accountId: selectedAccountId.value,
        sessions: sessions.value,
        logs: auditLogs.value,
      }
    case 'settings':
      return {
        accountId: selectedAccountId.value,
        config: strategyConfig.value,
        busy: strategyBusy.value,
        error: strategyError.value,
      }
    case 'operations':
      return {
        summary: summary.value,
        process: traderProcessState.value,
        verificationRows: verificationRows.value,
      }
  }
}

function componentListeners() {
  return {
    navigate,
    action: requestAction,
    close: (session: GridSession) => requestAction('close-session', session),
    save: saveStrategy,
  }
}

function dismissToast() {
  actionMessage.value = ''
  actionError.value = ''
}

function handleHashChange() {
  activePage.value = pageFromHash()
}

onMounted(async () => {
  window.addEventListener('hashchange', handleHashChange)
  await refreshData(true)
  startEventStream()
  refreshTimer = window.setInterval(() => void refreshData(), 12_000)
})

onUnmounted(() => {
  window.removeEventListener('hashchange', handleHashChange)
  if (startingPollTimer != null) {
    window.clearTimeout(startingPollTimer)
    startingPollTimer = undefined
  }
  closeEventStream()
  if (refreshTimer != null) window.clearInterval(refreshTimer)
})
</script>

<template>
  <div class="app-shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>

    <aside class="sidebar" :class="{ 'sidebar--open': sidebarOpen }">
      <div class="brand">
        <span class="brand__mark"><Grid3X3 :size="22" /></span>
        <div><strong>QuietGrid</strong><small>Adaptive Trading</small></div>
        <button class="icon-button sidebar__close" type="button" aria-label="关闭导航" @click="sidebarOpen = false">
          <X :size="20" />
        </button>
      </div>

      <nav class="main-navigation" aria-label="主导航">
        <section v-for="group in navigation" :key="group.label">
          <h2>{{ group.label }}</h2>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            :class="{ active: activePage === item.key }"
            :aria-current="activePage === item.key ? 'page' : undefined"
            @click="navigate(item.key)"
          >
            <component :is="item.icon" :size="19" aria-hidden="true" />
            <span><strong>{{ item.label }}</strong><small>{{ item.description }}</small></span>
          </button>
        </section>
      </nav>

      <div class="sidebar__footer">
        <div class="connection-state">
          <Wifi v-if="realtimeConnected" :size="17" />
          <WifiOff v-else :size="17" />
          <span><strong>{{ realtimeConnected ? '实时连接' : '轮询模式' }}</strong><small>{{ realtimeError || summary.heartbeat }}</small></span>
        </div>
      </div>
    </aside>

    <div v-if="sidebarOpen" class="sidebar-scrim" @click="sidebarOpen = false" />

    <div class="app-main">
      <header class="topbar">
        <div class="topbar__title">
          <button class="icon-button menu-button" type="button" aria-label="打开导航" @click="sidebarOpen = true">
            <Menu :size="21" />
          </button>
          <div>
            <h1>{{ activePageMeta.title }}</h1>
            <p>{{ activePageMeta.subtitle }}</p>
          </div>
          <StatusBadge
            class="mobile-environment"
            :tone="isLiveEnvironment ? 'danger' : 'info'"
            :label="isLiveEnvironment ? 'LIVE' : 'TESTNET'"
          />
        </div>

        <div class="topbar__status">
          <StatusBadge
            :tone="isLiveEnvironment ? 'danger' : 'info'"
            :label="isLiveEnvironment ? '真实盘 LIVE' : '测试网 TESTNET'"
          />
          <StatusBadge
            :tone="(traderProcessState.processState || traderProcessState.state) === 'ONLINE' || traderProcessState.state === 'running' ? 'good' : 'warning'"
            :label="`Trader ${traderProcessState.processState || traderProcessState.state}`"
          />
          <StatusBadge
            :tone="controlState.roundState === 'RUNNING' ? 'good' : 'info'"
            :label="`本轮 ${controlState.roundState || 'IDLE'}`"
          />
          <StatusBadge :tone="dataTone" :label="`数据 ${v2Dashboard.dataHealth}`" />
          <StatusBadge :tone="riskTone" :label="`风险 ${v2Dashboard.globalRiskLevel}`" />
        </div>

        <div class="topbar__actions">
          <label class="account-picker">
            <span class="sr-only">当前账户</span>
            <select v-model="selectedAccountId" @change="handleAccountChange">
              <option v-for="account in accountOptions" :key="account.id" :value="account.id">
                {{ account.label }} · {{ account.mode }}
              </option>
            </select>
          </label>
          <button
            class="icon-button"
            type="button"
            aria-label="刷新数据"
            :disabled="refreshing"
            @click="refreshData()"
          >
            <RefreshCw :size="19" :class="{ spin: refreshing }" />
          </button>
        </div>
      </header>

      <div v-if="isLiveEnvironment" class="live-warning" role="status">
        当前为真实盘环境。所有危险操作均需输入确认词，且会写入审计日志。
      </div>

      <main id="main-content" class="page-content">
        <div v-if="initialLoading" class="page-skeleton" aria-label="正在加载控制台">
          <span v-for="index in 6" :key="index" />
        </div>
        <component
          :is="activeComponent"
          v-else
          v-bind="componentProps()"
          v-on="componentListeners()"
        />
      </main>
    </div>

    <div v-if="actionMessage || actionError" class="toast" :class="{ 'toast--error': actionError }" role="status">
      <span>
        <strong>{{ actionError ? '操作未完成' : '操作已提交' }}</strong>
        {{ actionError || actionMessage }}
      </span>
      <button class="icon-button" type="button" aria-label="关闭消息" @click="dismissToast"><X :size="18" /></button>
    </div>

    <ConfirmDialog
      :open="Boolean(pendingAction)"
      :title="pendingAction?.title || ''"
      :description="pendingAction?.description || ''"
      :confirmation-text="pendingAction?.confirmationText || ''"
      :danger="pendingAction?.danger"
      :busy="actionBusy"
      @cancel="pendingAction = null"
      @confirm="confirmAction"
    />
  </div>
</template>
