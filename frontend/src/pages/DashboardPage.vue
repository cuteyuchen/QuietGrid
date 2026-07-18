<script setup lang="ts">
import { computed } from 'vue'
import {
  ArrowRight,
  CheckCircle2,
  CirclePause,
  Play,
  ShieldAlert,
  TriangleAlert,
} from '@lucide/vue'
import MetricCard from '../components/MetricCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { AutoTradingUiState, V2DashboardData } from '../api'
import type { ConsoleSummary, ControlState, GridSession } from '../mock'

const props = defineProps<{
  summary: ConsoleSummary
  dashboard: V2DashboardData
  control: ControlState
  sessions: GridSession[]
  autoTrading: AutoTradingUiState
  actionBusy: boolean
  loading: boolean
  dataError: string
}>()

const emit = defineEmits<{
  navigate: [page: string]
  action: [action: string, session?: GridSession]
}>()

const riskCritical = computed(() => {
  const level = props.dashboard.globalRiskLevel.toUpperCase()
  return ['HIGH', 'CRITICAL', 'EMERGENCY'].includes(level)
})

const dataUnhealthy = computed(() => {
  const health = props.dashboard.dataHealth.toUpperCase()
  return ['STALE', 'ERROR', 'UNHEALTHY', 'DISCONNECTED'].includes(health)
})

const status = computed(() => {
  if (props.dataError) {
    return {
      tone: 'danger' as const,
      eyebrow: '连接异常',
      title: '暂时无法确认系统是否安全',
      text: '控制台没有拿到最新数据。恢复连接前，不建议启动新一轮。',
      icon: TriangleAlert,
    }
  }
  if (riskCritical.value) {
    return {
      tone: 'danger' as const,
      eyebrow: '需要处理',
      title: '风险已接近或超过限制',
      text: props.dashboard.latestRisk?.reason || '系统已进入高风险状态，请先查看风险中心。',
      icon: ShieldAlert,
    }
  }
  if (dataUnhealthy.value) {
    return {
      tone: 'warning' as const,
      eyebrow: '等待数据恢复',
      title: '行情数据不够新鲜',
      text: '系统不会在数据过期时新增风险。请检查连接与时间戳。',
      icon: TriangleAlert,
    }
  }
  if (props.control.newEntriesPaused) {
    return {
      tone: 'info' as const,
      eyebrow: '受控暂停',
      title: '新开仓已暂停',
      text: '现有会话仍受风控管理；恢复前会再次校验市场状态和风险预算。',
      icon: CirclePause,
    }
  }
  if (props.dashboard.activeSessions > 0) {
    return {
      tone: 'good' as const,
      eyebrow: '受控运行',
      title: `${props.dashboard.activeSessions} 个会话正在运行`,
      text: '系统持续检查市场状态、库存利用率和窗口损失预算。',
      icon: CheckCircle2,
    }
  }
  if (props.autoTrading.enabled) {
    return {
      tone: 'info' as const,
      eyebrow: '自动扫描中',
      title: '自动交易已开启，正在扫描机会',
      text: props.dashboard.latestRegime?.reasons[0] || '暂无合适标的属于正常状态；系统会按分钟重新评估。',
      icon: CirclePause,
    }
  }
  if (props.dashboard.latestRegime?.allowed) {
    return {
      tone: 'good' as const,
      eyebrow: '可以准备',
      title: '当前市场条件允许网格',
      text: '启动请求仍会经过交易进程的最终风控，不会由网页直接下单。',
      icon: Play,
    }
  }
  return {
    tone: 'neutral' as const,
    eyebrow: '保持观察',
    title: '当前没有合适的网格机会',
    text: props.dashboard.latestRegime?.reasons[0] || '系统正在等待市场状态、数据和风险预算同时满足。',
    icon: CirclePause,
  }
})

const budgetUsed = computed(() => {
  if (props.dashboard.windowLossBudget <= 0) {
    return 0
  }
  return Math.max(
    0,
    Math.min(100, (1 - props.dashboard.windowLossBudgetRemaining / props.dashboard.windowLossBudget) * 100),
  )
})

const inventoryUsed = computed(() => Math.max(
  0,
  Math.min(100, (props.dashboard.latestInventory?.utilization || 0) * 100),
))

const activeSessionItems = computed(() => props.sessions.filter(
  (session) => !['STOPPED', 'CLOSED'].includes(session.state),
))

const actionAlerts = computed(() => {
  const alerts: Array<{ tone: 'warning' | 'danger'; title: string; detail: string; page: string }> = []
  if (props.dataError) {
    alerts.push({ tone: 'danger', title: '控制台连接失败', detail: props.dataError, page: 'operations' })
  }
  if (dataUnhealthy.value) {
    alerts.push({
      tone: 'danger',
      title: '市场数据过期或断开',
      detail: `当前数据状态：${props.dashboard.dataHealth}`,
      page: 'operations',
    })
  }
  if (props.dashboard.latestRegime?.hardBlocks.length) {
    alerts.push({
      tone: 'warning',
      title: '市场状态存在硬阻断',
      detail: props.dashboard.latestRegime.hardBlocks.join('；'),
      page: 'market',
    })
  }
  if (inventoryUsed.value >= 70) {
    alerts.push({
      tone: inventoryUsed.value >= 90 ? 'danger' : 'warning',
      title: '库存利用率偏高',
      detail: `当前已使用 ${inventoryUsed.value.toFixed(0)}%，系统将限制同方向新增订单。`,
      page: 'risk',
    })
  }
  return alerts
})

function money(value: number | null | undefined) {
  return value == null ? '—' : `${value >= 0 ? '' : '-'}$${Math.abs(value).toFixed(2)}`
}

function pct(value: number | null | undefined) {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}
</script>

<template>
  <div class="page-stack">
    <section class="safety-hero" :class="`safety-hero--${status.tone}`">
      <div class="safety-hero__icon">
        <component :is="status.icon" :size="26" aria-hidden="true" />
      </div>
      <div class="safety-hero__copy">
        <span>{{ status.eyebrow }}</span>
        <h2>{{ status.title }}</h2>
        <p>{{ status.text }}</p>
      </div>
      <div class="safety-hero__actions">
        <button
          v-if="control.newEntriesPaused"
          class="button button--primary"
          type="button"
          @click="emit('action', 'resume')"
        >
          恢复新开仓
        </button>
        <button
          v-if="!control.newEntriesPaused && dashboard.activeSessions === 0 && !autoTrading.enabled"
          class="button button--primary"
          type="button"
          :disabled="!autoTrading.canStart || actionBusy"
          @click="emit('action', 'auto-trading-start')"
        >
          {{ autoTrading.transitioning ? '正在启动…' : '启动自动交易' }}
        </button>
        <button
          v-else-if="!control.newEntriesPaused && dashboard.activeSessions === 0 && autoTrading.enabled"
          class="button button--danger-outline"
          type="button"
          :disabled="!autoTrading.canStop || actionBusy"
          @click="emit('action', 'auto-trading-stop')"
        >
          {{ autoTrading.transitioning ? '正在停止…' : '停止自动交易' }}
        </button>
        <button
          v-if="!control.newEntriesPaused && dashboard.activeSessions === 0 && !autoTrading.enabled"
          class="button button--secondary"
          type="button"
          :disabled="!control.roundStartAvailable"
          @click="emit('action', 'start-round')"
        >
          启动本轮（高级）
        </button>
        <button
          v-if="!control.newEntriesPaused && dashboard.activeSessions > 0"
          class="button button--secondary"
          type="button"
          @click="emit('action', 'pause')"
        >
          暂停新开仓
        </button>
        <button class="button button--ghost" type="button" @click="emit('navigate', 'risk')">
          查看风险
          <ArrowRight :size="16" />
        </button>
      </div>
    </section>

    <section aria-labelledby="account-overview-title">
      <div class="section-heading">
        <div>
          <p class="eyebrow">账户与窗口</p>
          <h2 id="account-overview-title">当前资金状态</h2>
        </div>
        <StatusBadge
          :tone="dashboard.globalRiskLevel === 'LOW' ? 'good' : 'warning'"
          :label="`风险 ${dashboard.globalRiskLevel}`"
        />
      </div>
      <div class="metric-grid">
        <MetricCard label="账户权益" :value="money(dashboard.equity || summary.balance)" hint="包含未实现盈亏" />
        <MetricCard label="可用余额" :value="money(dashboard.availableBalance)" hint="可用于保证金与费用" />
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
      </div>
    </section>

    <section v-if="actionAlerts.length" class="action-alerts" aria-labelledby="action-alerts-title">
      <div class="section-heading">
        <div>
          <p class="eyebrow">需要行动</p>
          <h2 id="action-alerts-title">只显示真正需要处理的告警</h2>
        </div>
      </div>
      <button
        v-for="alert in actionAlerts"
        :key="alert.title"
        class="action-alert"
        :class="`action-alert--${alert.tone}`"
        type="button"
        @click="emit('navigate', alert.page)"
      >
        <TriangleAlert :size="20" aria-hidden="true" />
        <span>
          <strong>{{ alert.title }}</strong>
          <small>{{ alert.detail }}</small>
        </span>
        <ArrowRight :size="18" aria-hidden="true" />
      </button>
    </section>

    <div class="content-grid content-grid--dashboard">
      <section class="panel" aria-labelledby="session-preview-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">活跃会话</p>
            <h2 id="session-preview-title">系统正在做什么</h2>
          </div>
          <button class="text-button" type="button" @click="emit('navigate', 'sessions')">
            查看全部
            <ArrowRight :size="16" />
          </button>
        </div>

        <div v-if="activeSessionItems.length" class="session-preview-list">
          <article v-for="session in activeSessionItems.slice(0, 3)" :key="session.id" class="session-preview">
            <div>
              <strong>{{ session.symbol }}</strong>
              <StatusBadge
                :tone="session.state === 'RUNNING' ? 'good' : session.state === 'COOLDOWN' ? 'warning' : 'neutral'"
                :label="session.stateLabel"
              />
            </div>
            <dl>
              <div><dt>已实现盈亏</dt><dd :class="session.pnl >= 0 ? 'positive' : 'negative'">{{ money(session.pnl) }}</dd></div>
              <div><dt>库存名义</dt><dd>{{ money(session.position.notional) }}</dd></div>
              <div><dt>网格</dt><dd>{{ session.gridNum }} 格 · {{ pct(session.stepPct) }}</dd></div>
            </dl>
            <button class="button button--quiet" type="button" @click="emit('navigate', 'sessions')">
              查看会话
            </button>
          </article>
        </div>
        <div v-else class="empty-state">
          <CirclePause :size="30" aria-hidden="true" />
          <h3>当前没有活跃会话</h3>
          <p>系统会先扫描流动性与市场状态，满足条件后才允许创建网格。</p>
        </div>
      </section>

      <section class="panel" aria-labelledby="protection-title">
        <div class="panel__header">
          <div>
            <p class="eyebrow">保护机制</p>
            <h2 id="protection-title">离限制还有多远</h2>
          </div>
          <button class="text-button" type="button" @click="emit('navigate', 'risk')">
            详细预算
            <ArrowRight :size="16" />
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
      </section>
    </div>
  </div>
</template>
