<script setup lang="ts">
import { computed } from 'vue'
import {
  ArrowRight,
  CheckCircle2,
  CirclePause,
  Clock,
  Layers3,
  Play,
  Radar,
  ShieldAlert,
  TriangleAlert,
} from '@lucide/vue'
import MetricCard from '../components/MetricCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StartupProgress, { type StartupStep } from '../components/runtime/StartupProgress.vue'
import CandidateEvaluationTable from '../components/runtime/CandidateEvaluationTable.vue'
import AutoTradingControl from '../components/runtime/AutoTradingControl.vue'
import type { AutoTradingUiState, CurrentRoundSnapshot, V2DashboardData } from '../api'
import type { ControlState, GridSession, LiquidityCandidate, TraderProcessState } from '../mock'

const props = defineProps<{
  dashboard: V2DashboardData
  control: ControlState
  sessions: GridSession[]
  candidates: LiquidityCandidate[]
  currentRound?: CurrentRoundSnapshot | null
  autoTrading: AutoTradingUiState
  actionBusy: boolean
  traderProcess?: TraderProcessState | null
  loading: boolean
  dataError: string
}>()

const emit = defineEmits<{
  navigate: [page: string]
  action: [action: string, session?: GridSession]
}>()

// 生命周期主线：从现有运行信号推断当前阶段，让用户一眼看到"现在在哪一步、下一步是什么"。
const LIFECYCLE = [
  { key: 'WAITING', label: '等待窗口', hint: '等待休市窗口开启' },
  { key: 'START_QUEUED', label: '排队启动', hint: '等待 Trader 消费启动请求' },
  { key: 'SCANNING', label: '扫描评估', hint: '拉取三小时 K 线并评估' },
  { key: 'PLANNING', label: '生成网格', hint: '计算自适应网格' },
  { key: 'RUNNING', label: '运行', hint: '网格挂单与成交' },
  { key: 'DEFENSIVE', label: '防御', hint: '保留减仓和保护订单，等待评分恢复' },
  { key: 'COOLDOWN', label: '冷却', hint: '止损后等待恢复' },
  { key: 'CLOSING', label: '强制离场', hint: '盘前撤单平仓' },
  { key: 'COMPLETED', label: '本轮完成', hint: '生成本轮报告' },
] as const

type LifecyclePhase = (typeof LIFECYCLE)[number]['key']

const activeSessionItems = computed(() =>
  props.sessions.filter((session) => !['STOPPED', 'CLOSED'].includes(session.state)),
)

const observingSessions = computed(() =>
  activeSessionItems.value.filter((session) => session.volatilityStage === 'OBSERVING'),
)

const runningSessions = computed(() =>
  activeSessionItems.value.filter((session) => session.state === 'RUNNING'),
)

const defensiveSessions = computed(() =>
  activeSessionItems.value.filter((session) => session.state === 'DEFENSIVE'),
)

const managedSessions = computed(() => [
  ...runningSessions.value,
  ...defensiveSessions.value,
])

const cooldownSessions = computed(() =>
  activeSessionItems.value.filter((session) => session.state === 'COOLDOWN'),
)

const selectedCandidates = computed(() => props.candidates.filter((item) => item.selected))

const blockedCandidates = computed(() =>
  props.candidates.filter((item) => {
    const stage = String(item.stage || '').toLowerCase()
    return stage.includes('block') || stage.includes('below') || stage.includes('error') || item.thresholdMet === false
  }),
)

const phase = computed<LifecyclePhase>(() => {
  const roundState = (props.control.roundState || '').toUpperCase()
  const requestStatus = String(props.control.roundStartRequest?.status || '').toLowerCase()
  if (['CLOSING', 'FORCE_CLOSE', 'FORCE_EXIT', 'STOPPING'].includes(roundState)) return 'CLOSING'
  if (['COMPLETED', 'DONE', 'FINISHED', 'STOPPED'].includes(roundState)) return 'COMPLETED'
  if (cooldownSessions.value.length) return 'COOLDOWN'
  if (defensiveSessions.value.length || roundState === 'DEFENSIVE') return 'DEFENSIVE'
  if (runningSessions.value.length || roundState === 'RUNNING') return 'RUNNING'
  if (props.candidates.some((item) => String(item.stage || '').toLowerCase() === 'planning' || String(item.stage || '').toLowerCase() === 'placing_orders')) {
    return 'PLANNING'
  }
  if (observingSessions.value.length) return 'SCANNING'
  if (roundState === 'SCANNING' || selectedCandidates.value.length || blockedCandidates.value.length) return 'SCANNING'
  // 请求存在但 Trader 未消费：排队，而不是误报 PLANNING
  if (requestStatus === 'requested' || requestStatus === 'running' || roundState === 'REQUESTED' || roundState === 'START_QUEUED') {
    return 'START_QUEUED'
  }
  return 'WAITING'
})

const startupSteps = computed<StartupStep[]>(() => {
  const traderState = (props.traderProcess?.processState || props.traderProcess?.state || 'OFFLINE').toUpperCase()
  const online = traderState === 'ONLINE' || traderState === 'RUNNING'
  const starting = traderState === 'STARTING'
  const round = phase.value
  const step = (key: string, label: string, status: StartupStep['status']): StartupStep => ({ key, label, status })
  const doneOr = (cond: boolean, active = false): StartupStep['status'] => (cond ? 'done' : active ? 'active' : 'pending')
  return [
    step('process', '启动交易进程', doneOr(online, starting)),
    step('heartbeat', '等待首个心跳', doneOr(online, starting)),
    step('recover', '恢复和交易所对账', doneOr(online && round !== 'WAITING', online && round === 'START_QUEUED')),
    step('window', '检查周末窗口', doneOr(['SCANNING', 'PLANNING', 'RUNNING', 'DEFENSIVE', 'COOLDOWN', 'CLOSING', 'COMPLETED'].includes(round), round === 'START_QUEUED')),
    step('kline', '获取最近三小时 K线', doneOr(['PLANNING', 'RUNNING', 'DEFENSIVE', 'COOLDOWN', 'CLOSING', 'COMPLETED'].includes(round), round === 'SCANNING')),
    step('eval', '波动率与 Regime 评估', doneOr(['PLANNING', 'RUNNING', 'DEFENSIVE', 'COOLDOWN', 'CLOSING', 'COMPLETED'].includes(round), round === 'SCANNING')),
    step('grid', '生成并提交网格', doneOr(['RUNNING', 'DEFENSIVE', 'COOLDOWN', 'CLOSING', 'COMPLETED'].includes(round), round === 'PLANNING')),
  ]
})

const windowLabel = computed(() => {
  const window = props.currentRound?.window
  if (!window) return ''
  if (window.testnetForceWindow) return '强制测试窗口'
  if (!window.kind) return window.reason || ''
  return `${window.kind}${window.allowed ? ' · 允许交易' : ' · 禁止开仓'}`
})

const currentPhaseIndex = computed(() =>
  LIFECYCLE.findIndex((item) => item.key === phase.value),
)

const currentPhaseMeta = computed(() => LIFECYCLE[currentPhaseIndex.value] || LIFECYCLE[0])

const nextPhaseMeta = computed(() => {
  if (phase.value === 'DEFENSIVE') {
    return { key: 'RUNNING', label: '恢复运行', hint: '评分恢复后对账并补齐网格' }
  }
  return LIFECYCLE[currentPhaseIndex.value + 1] || null
})

// 观察进度：取正在观察的会话中进度最靠前的一个作为主线展示。
const observationProgress = computed(() => {
  const values = observingSessions.value
    .map((session) => session.volatilityProgressPct)
    .filter((value): value is number => value != null)
  if (!values.length) return null
  return Math.max(...values)
})

// 距强制离场：取运行会话中最近的关闭时间，换算成剩余分钟。
const forceExitCountdown = computed(() => {
  const deadlines = managedSessions.value
    .map((session) => session.closeTime)
    .filter((value) => Boolean(value))
    .map((value) => new Date(value).getTime())
    .filter((value) => Number.isFinite(value) && value > 0)
  if (!deadlines.length) return null
  const soonest = Math.min(...deadlines)
  const remainingMs = soonest - Date.now()
  if (remainingMs <= 0) return { minutes: 0, overdue: true }
  return { minutes: Math.round(remainingMs / 60000), overdue: false }
})

const budgetUsed = computed(() => {
  if (props.dashboard.windowLossBudget <= 0) return 0
  return Math.max(
    0,
    Math.min(
      100,
      (1 - props.dashboard.windowLossBudgetRemaining / props.dashboard.windowLossBudget) * 100,
    ),
  )
})

const inventoryUsed = computed(() =>
  Math.max(0, Math.min(100, (props.dashboard.latestInventory?.utilization || 0) * 100)),
)

// 止损距离：运行会话中，标记价距止损线的最小百分比距离，越小越危险。
const stopDistancePct = computed(() => {
  const distances = managedSessions.value
    .map((session) => {
      const mark = session.position.markPrice
      const stop = session.stopLossPrice
      if (mark == null || !stop || mark <= 0) return null
      return (Math.abs(mark - stop) / mark) * 100
    })
    .filter((value): value is number => value != null)
  if (!distances.length) return null
  return Math.min(...distances)
})

const streamItems = computed(() => {
  const streams = props.currentRound?.streamHealth.streams || {}
  return ['price', 'kline', 'user'].map((name) => ({
    name,
    label: name === 'price' ? 'Price Stream' : name === 'kline' ? 'Kline Stream' : 'User Stream',
    ...(streams[name] || {
      state: 'WAITING',
      reconnectCount: 0,
      lastMessageAt: '',
      lastError: '',
      updatedAt: '',
    }),
  }))
})

function money(value: number | null | undefined) {
  return value == null ? '—' : `${value >= 0 ? '' : '-'}$${Math.abs(value).toFixed(2)}`
}

function stepIsDone(index: number) {
  return index < currentPhaseIndex.value
}

function stepIsActive(index: number) {
  return index === currentPhaseIndex.value
}
</script>

<template>
  <div class="page-stack">
    <section class="panel" aria-labelledby="lifecycle-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">本轮运行</p>
          <h2 id="lifecycle-title">当前处于「{{ currentPhaseMeta.label }}」阶段</h2>
        </div>
        <StatusBadge
          :tone="phase === 'RUNNING' ? 'good' : ['DEFENSIVE', 'CLOSING', 'COOLDOWN'].includes(phase) ? 'warning' : 'neutral'"
          :label="currentPhaseMeta.label"
        />
      </div>

      <ol class="lifecycle-track" aria-label="本轮生命周期">
        <li
          v-for="(step, index) in LIFECYCLE"
          :key="step.key"
          class="lifecycle-step"
          :class="{
            'lifecycle-step--done': stepIsDone(index),
            'lifecycle-step--active': stepIsActive(index),
          }"
        >
          <span class="lifecycle-step__dot" aria-hidden="true" />
          <span class="lifecycle-step__label">{{ step.label }}</span>
          <span class="lifecycle-step__hint">{{ step.hint }}</span>
        </li>
      </ol>

      <p v-if="nextPhaseMeta" class="lifecycle-next">
        <ArrowRight :size="16" aria-hidden="true" />
        下一步：{{ nextPhaseMeta.label }}（{{ nextPhaseMeta.hint }}）
      </p>
      <p v-else class="lifecycle-next">本轮已到最后阶段，完成后将生成报告。</p>

      <div class="content-grid" style="margin-top:1rem">
        <AutoTradingControl
          :state="autoTrading"
          :trader-state="traderProcess?.processState || traderProcess?.state || 'OFFLINE'"
          :window-label="windowLabel"
          :minutes-to-force-close="currentRound?.window.minutesToForceClose"
          :next-scan-at="control.nextScanAt"
          :busy="actionBusy"
          @start="emit('action', 'auto-trading-start')"
          @stop="emit('action', 'auto-trading-stop')"
        />
        <StartupProgress :steps="startupSteps" title="启动进度" />
      </div>
    </section>

    <div v-if="dataError" class="action-alert action-alert--danger">
      <TriangleAlert :size="20" aria-hidden="true" />
      <span>
        <strong>控制台连接失败</strong>
        <small>{{ dataError }}</small>
      </span>
    </div>

    <div class="metric-grid">
      <MetricCard
        label="本窗口盈亏"
        :value="money(dashboard.windowPnl)"
        :tone="dashboard.windowPnl >= 0 ? 'good' : 'danger'"
        hint="已实现口径"
      />
      <MetricCard
        label="剩余损失预算"
        :value="money(dashboard.windowLossBudgetRemaining)"
        :tone="budgetUsed >= 75 ? 'warning' : 'default'"
        :hint="`已使用 ${budgetUsed.toFixed(0)}%`"
      />
      <MetricCard
        label="距强制离场"
        :value="forceExitCountdown == null ? '—' : forceExitCountdown.overdue ? '已到时' : `${forceExitCountdown.minutes} 分钟`"
        :tone="forceExitCountdown?.overdue ? 'danger' : forceExitCountdown && forceExitCountdown.minutes <= 30 ? 'warning' : 'default'"
        hint="最近一个会话的盘前离场"
      />
      <MetricCard
        label="距止损"
        :value="stopDistancePct == null ? '—' : `${stopDistancePct.toFixed(2)}%`"
        :tone="stopDistancePct != null && stopDistancePct <= 1 ? 'danger' : stopDistancePct != null && stopDistancePct <= 2 ? 'warning' : 'default'"
        hint="运行会话中最近的止损距离"
      />
    </div>

    <section class="panel" aria-labelledby="stream-health-title">
      <div class="panel__header">
        <div>
          <p class="eyebrow">数据流</p>
          <h2 id="stream-health-title">实时连接与重连状态</h2>
        </div>
      </div>
      <div class="stream-health-grid">
        <article v-for="stream in streamItems" :key="stream.name" class="stream-health-item">
          <div>
            <strong>{{ stream.label }}</strong>
            <StatusBadge
              :tone="stream.state === 'ONLINE' ? 'good' : stream.state === 'DEGRADED' || stream.state === 'FAILED' ? 'danger' : 'neutral'"
              :label="stream.state"
            />
          </div>
          <small>重连 {{ stream.reconnectCount }} 次</small>
          <small>最后消息：{{ stream.lastMessageAt || '等待首条消息' }}</small>
          <p v-if="stream.lastError" class="stream-health-error">{{ stream.lastError }}</p>
        </article>
      </div>
    </section>

    <div class="content-grid content-grid--dashboard">
      <section class="panel" aria-labelledby="round-focus-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">本轮焦点</p>
            <h2 id="round-focus-title">系统正在处理什么</h2>
          </div>
        </div>

        <div v-if="phase === 'START_QUEUED'" class="round-focus">
          <Clock :size="22" aria-hidden="true" />
          <div>
            <strong>启动请求已排队</strong>
            <p>Trader 在线后将检查周末/节假日窗口，并拉取最近三小时 K 线评估。</p>
            <p v-if="control.nextScanAt">下次扫描：{{ control.nextScanAt }}</p>
          </div>
        </div>

        <div v-else-if="phase === 'SCANNING'" class="round-focus">
          <Radar :size="22" aria-hidden="true" />
          <div>
            <strong>正在扫描与评估候选</strong>
            <p v-if="selectedCandidates.length">
              候选：{{ selectedCandidates.map((item) => item.symbol).join('、') }}
            </p>
            <p v-else-if="blockedCandidates.length">
              暂无合格标的；已记录 {{ blockedCandidates.length }} 个阻断原因，将按分钟重评。
            </p>
            <p v-else>正在扫描流动性并预填充最近三小时 1m K 线。</p>
            <p v-if="control.nextScanAt">下次评估：{{ control.nextScanAt }}</p>
            <button class="text-button" type="button" @click="emit('navigate', 'market')">
              查看候选评分<ArrowRight :size="16" />
            </button>
          </div>
        </div>

        <div v-else-if="phase === 'PLANNING'" class="round-focus">
          <Clock :size="22" aria-hidden="true" />
          <div>
            <strong>正在生成网格并提交 POST_ONLY 订单</strong>
            <p>候选已通过窗口、数据、Regime 与风险检查，正在挂单。</p>
          </div>
        </div>

        <div v-else-if="managedSessions.length" class="session-preview-list">
          <article v-for="session in managedSessions.slice(0, 3)" :key="session.id" class="session-preview">
            <div>
              <strong>{{ session.symbol }}</strong>
              <StatusBadge :tone="session.state === 'DEFENSIVE' ? 'warning' : 'good'" :label="session.stateLabel" />
            </div>
            <dl>
              <div><dt>已实现盈亏</dt><dd :class="session.pnl >= 0 ? 'positive' : 'negative'">{{ money(session.pnl) }}</dd></div>
              <div><dt>网格</dt><dd>{{ session.gridNum }} 格</dd></div>
              <div><dt>挂单</dt><dd>{{ session.openOrderCount }}</dd></div>
              <div><dt>软性违约</dt><dd>{{ session.softBreachCount || 0 }}/3</dd></div>
            </dl>
            <button class="button button--quiet" type="button" @click="emit('navigate', 'sessions')">
              查看会话
            </button>
          </article>
        </div>

        <div v-else class="empty-state">
          <CirclePause :size="30" aria-hidden="true" />
          <h3>本轮暂无运行中的网格</h3>
          <p>系统会先完成选择与观察，满足市场状态和风险预算后才会挂单。</p>
        </div>

        <CandidateEvaluationTable :candidates="candidates" :limit="12" style="margin-top:1rem" />
      </section>

      <section class="panel" aria-labelledby="protection-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">保护机制</p>
            <h2 id="protection-title">离限制还有多远</h2>
          </div>
          <button class="text-button" type="button" @click="emit('navigate', 'risk')">
            详细预算<ArrowRight :size="16" />
          </button>
        </div>

        <div class="progress-list">
          <div class="progress-item">
            <div><span>窗口损失预算</span><strong>{{ budgetUsed.toFixed(0) }}%</strong></div>
            <div class="progress-track"><span :style="{ width: `${budgetUsed}%` }" /></div>
            <small>剩余 {{ money(dashboard.windowLossBudgetRemaining) }} / {{ money(dashboard.windowLossBudget) }}</small>
          </div>
          <div class="progress-item">
            <div><span>库存利用率</span><strong>{{ inventoryUsed.toFixed(0) }}%</strong></div>
            <div class="progress-track"><span :style="{ width: `${inventoryUsed}%` }" /></div>
            <small>{{ dashboard.latestInventory?.riskLevel || '暂无库存快照' }}</small>
          </div>
          <div class="progress-item">
            <div><span>市场适配度</span><strong>{{ dashboard.latestRegime?.gridScore.toFixed(0) || '—' }}</strong></div>
            <div class="progress-track"><span :style="{ width: `${dashboard.latestRegime?.gridScore || 0}%` }" /></div>
            <small>{{ dashboard.latestRegime?.allowed ? '允许网格' : '等待条件改善' }}</small>
          </div>
        </div>

        <div class="round-actions">
          <button
            v-if="control.newEntriesPaused"
            class="button button--primary"
            type="button"
            @click="emit('action', 'resume')"
          >
            <Play :size="16" />恢复新开仓
          </button>
          <button
            class="button button--secondary"
            type="button"
            @click="emit('navigate', 'sessions')"
          >
            <Layers3 :size="16" />查看会话与库存
          </button>
          <button class="button button--ghost" type="button" @click="emit('navigate', 'risk')">
            <ShieldAlert :size="16" />风险中心
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.lifecycle-track {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  list-style: none;
  margin: 1rem 0 0;
  padding: 0;
}

.stream-health-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: 0.75rem;
}

.stream-health-item {
  display: grid;
  gap: 0.4rem;
  min-width: 0;
  padding: 0.85rem;
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 0.75rem;
  background: rgba(15, 23, 42, 0.35);
}

.stream-health-item > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.stream-health-item small {
  color: var(--muted);
}

.stream-health-error {
  margin: 0;
  color: #fca5a5;
  overflow-wrap: anywhere;
}

.lifecycle-step {
  flex: 1 1 8rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  padding: 0.6rem 0.75rem;
  border-radius: 0.6rem;
  border: 1px solid var(--border, #2a2f3a);
  opacity: 0.55;
}

.lifecycle-step--done {
  opacity: 0.85;
}

.lifecycle-step--active {
  opacity: 1;
  border-color: var(--accent, #4f8cff);
  box-shadow: 0 0 0 1px var(--accent, #4f8cff) inset;
}

.lifecycle-step__dot {
  width: 0.6rem;
  height: 0.6rem;
  border-radius: 50%;
  background: var(--border, #2a2f3a);
}

.lifecycle-step--done .lifecycle-step__dot {
  background: var(--good, #3fb950);
}

.lifecycle-step--active .lifecycle-step__dot {
  background: var(--accent, #4f8cff);
}

.lifecycle-step__label {
  font-weight: 600;
}

.lifecycle-step__hint {
  font-size: 0.75rem;
  opacity: 0.7;
}

.lifecycle-next {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0.9rem 0 0;
  font-size: 0.85rem;
  opacity: 0.8;
}

.round-focus {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
}

.round-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 1rem;
}
</style>
