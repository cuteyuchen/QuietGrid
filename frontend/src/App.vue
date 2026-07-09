<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
import { executeConsoleAction, loadConsoleData, saveStrategyConfigDraft, type ConsoleAction } from './api'
import {
  auditLogs as fallbackAuditLogs,
  controlState as fallbackControlState,
  sessions as fallbackSessions,
  strategyConfig as fallbackStrategyConfig,
  summary as fallbackSummary,
  verificationRows as fallbackVerificationRows,
  type StrategySettings,
} from './mock'

const tabs = [
  { key: 'overview', label: '总览', icon: LayoutDashboard },
  { key: 'grids', label: '网格控制', icon: Activity },
  { key: 'strategy', label: '策略参数', icon: SlidersHorizontal },
  { key: 'testnet', label: '测试网验证', icon: ShieldCheck },
  { key: 'logs', label: '日志审计', icon: History },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('overview')
const testRunSeconds = ref(600)
const loading = ref(false)
const actionBusy = ref<ConsoleAction | ''>('')
const strategyBusy = ref(false)
const dataError = ref('')
const actionMessage = ref('')
const actionError = ref('')
const strategyError = ref('')
const summary = ref(fallbackSummary)
const controlState = ref(fallbackControlState)
const strategyConfig = ref(fallbackStrategyConfig)
const strategyForm = ref<StrategySettings>({ ...fallbackStrategyConfig.draft })
const sessions = ref(fallbackSessions)
const verificationRows = ref(fallbackVerificationRows)
const auditLogs = ref(fallbackAuditLogs)
const pendingAction = ref<ActionConfig | null>(null)
const actionReason = ref('控制台手动操作')

const activeTabMeta = computed(() => tabs.find((tab) => tab.key === activeTab.value) ?? tabs[0])

const stateLabels: Record<string, string> = {
  RUNNING: '运行中',
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
  { label: '账户余额', value: formatBalance(summary.value.balance), detail: '交易所余额阶段 C 接入', tone: 'accent' },
])

const dataSourceLabel = computed(() => (dataError.value ? '离线示例' : '实时数据'))
const paused = computed(() => controlState.value.newEntriesPaused)

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

function formatState(value: string) {
  return stateLabels[value] ?? value
}

function formatVolatilityMethod(value: string) {
  return volatilityLabels[value] ?? value
}

function formatStopRequestStatus(value: string) {
  const labels: Record<string, string> = {
    requested: '停止请求已提交',
    closing: '停止清理中',
    completed: '停止已完成',
  }
  return labels[value] ?? value
}

function formatAuditModule(value: string) {
  return auditModuleLabels[value] ?? value
}

async function refreshData() {
  loading.value = true
  try {
    const data = await loadConsoleData()
    summary.value = data.summary
    controlState.value = data.controlState
    strategyConfig.value = data.strategyConfig
    strategyForm.value = { ...data.strategyConfig.draft }
    sessions.value = data.sessions
    verificationRows.value = data.verificationRows
    auditLogs.value = data.auditLogs
    dataError.value = ''
  } catch (error) {
    dataError.value = error instanceof Error ? error.message : '无法连接控制台 API'
  } finally {
    loading.value = false
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
    const result = await saveStrategyConfigDraft(strategyForm.value)
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
  if (key === 'min_step_pct') {
    return formatPct(Number(value))
  }
  if (key === 'observe_hours') {
    return `${Number(value).toFixed(2)} 小时`
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
      reason: actionReason.value.trim() || config.title,
      loopSeconds: config.action === 'testnet-run' ? testRunSeconds.value : undefined,
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

function testnetRunAction(): ActionConfig {
  return {
    action: 'testnet-run',
    title: '执行一键测试网流程',
    description: `将运行 ${testRunSeconds.value} 秒有界测试，并自动执行前置持仓检查、安全清扫和后置检查。`,
    buttonLabel: '确认执行',
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
      ? '后续交易循环可以继续选择新标的并创建新网格。'
      : '后续交易循环会跳过新标的选择，不会新建网格；已存在会话仍继续对账和风控。',
    buttonLabel: willResume ? '确认恢复' : '确认暂停',
    tone: willResume ? 'secondary' : 'danger',
  }
}

onMounted(() => {
  void refreshData()
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
            <button class="primary-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(testnetRunAction())">
              <Play :size="18" />
              启动有界测试
            </button>
            <button class="secondary-button" type="button">
            <Square :size="18" />
              停止循环
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

        <div class="split-grid">
          <section class="surface">
            <div class="section-title">
              <BarChart3 :size="18" />
              <h3>波动率与风险摘要</h3>
            </div>
            <div class="volatility-strip">
              <div v-if="sessions.length === 0" class="empty-state">暂无网格会话，启动测试网流程后会显示波动率快照。</div>
              <div v-for="session in sessions" :key="session.id" class="vol-row">
                <span>{{ session.symbol }}</span>
                <strong>{{ formatPct(session.currentVolatility) }}</strong>
                <small>{{ session.volatilityMethodLabel || formatVolatilityMethod(session.volatilityMethod) }}</small>
              </div>
            </div>
          </section>
          <section class="surface">
            <div class="section-title">
              <ShieldCheck :size="18" />
              <h3>测试网验证</h3>
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
          <button class="secondary-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(pauseToggleAction())">
            <CirclePause :size="18" />
            {{ paused ? '恢复新开仓' : '暂停新开仓' }}
          </button>
          <span class="control-note">
            当前：{{ paused ? '已暂停新开仓' : '允许新开仓' }} · {{ controlState.newEntriesPausedUpdatedAt }}
          </span>
          <button class="danger-button" type="button" disabled>
            <Square :size="18" />
            停止全部网格
          </button>
        </div>
        <section class="surface table-surface">
          <div class="section-title">
            <Database :size="18" />
            <h3>活动与最近网格</h3>
          </div>
          <div class="data-table" role="table" aria-label="网格会话表">
            <div class="table-row table-head" role="row">
              <span>标的</span>
              <span>状态</span>
              <span>区间</span>
              <span>网格</span>
              <span>波动率</span>
              <span>下一轮开仓</span>
              <span>操作</span>
            </div>
            <div v-for="session in sessions" :key="session.id" class="table-row" role="row">
              <strong>{{ session.symbol }}</strong>
              <span class="state-pill">{{ session.stateLabel || formatState(session.state) }}</span>
              <span>{{ session.lower.toFixed(2) }} - {{ session.upper.toFixed(2) }}</span>
              <span>{{ session.gridNum }} / {{ formatPct(session.stepPct) }}</span>
              <span>{{ formatPct(session.currentVolatility) }}</span>
              <span class="entry-state" :class="{ blocked: session.nextEntryDisabled }">
                {{ session.nextEntryDisabled ? '已禁用' : '允许' }}
              </span>
              <div class="row-actions">
                <button
                  class="compact-danger"
                  type="button"
                  :disabled="Boolean(actionBusy) || !canStopSession(session)"
                  @click="openAction(stopSessionAction(session))"
                >
                  <Power :size="16" />
                  {{ session.stopRequested ? formatStopRequestStatus(session.stopRequestStatus) : '停止' }}
                </button>
                <button
                  class="compact-secondary"
                  :class="{ warning: !session.nextEntryDisabled }"
                  type="button"
                  :disabled="Boolean(actionBusy)"
                  @click="openAction(symbolToggleAction(session))"
                >
                  <Ban :size="16" />
                  {{ session.nextEntryDisabled ? '启用开仓' : '禁用开仓' }}
                </button>
              </div>
            </div>
            <div v-if="sessions.length === 0" class="table-row empty-row" role="row">
              <span>暂无活动或最近网格</span>
            </div>
          </div>
        </section>
      </section>

      <section v-if="activeTab === 'strategy'" class="panel-stack">
        <section class="surface form-grid">
          <div class="section-title wide">
            <SlidersHorizontal :size="18" />
            <h3>下轮生效参数草稿</h3>
          </div>
          <div class="config-summary wide">
            <div>
              <span>当前运行配置</span>
              <strong>{{ formatVolatilityMethod(strategyConfig.current.volatilityMethod) }}</strong>
              <small>
                并发 {{ strategyConfig.current.maxConcurrent }} · 观察 {{ strategyConfig.current.observeHours }} 小时 ·
                最小步长 {{ formatPct(strategyConfig.current.minStepPct) }}
              </small>
            </div>
            <div>
              <span>草稿更新时间</span>
              <strong>{{ strategyConfig.draftUpdatedAt }}</strong>
              <small>{{ strategyConfig.diff.length ? `有 ${strategyConfig.diff.length} 项下轮变更` : '草稿与当前配置一致' }}</small>
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
            <span>最大并发标的</span>
            <input v-model.number="strategyForm.maxConcurrent" type="number" min="1" max="10" />
          </label>
          <label>
            <span>观察窗口小时</span>
            <input v-model.number="strategyForm.observeHours" type="number" min="0.1" max="24" step="0.1" />
          </label>
          <label>
            <span>最小网格步长</span>
            <input v-model.number="strategyForm.minStepPct" type="number" min="0.0001" max="0.05" step="0.0001" />
          </label>
          <label>
            <span>最大网格数量</span>
            <input v-model.number="strategyForm.maxGridNum" type="number" min="1" max="200" />
          </label>
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
            {{ strategyBusy ? '保存中' : '保存为下轮生效' }}
          </button>
        </section>
      </section>

      <section v-if="activeTab === 'testnet'" class="panel-stack">
        <section class="surface form-grid">
          <div class="section-title wide">
            <ShieldCheck :size="18" />
            <h3>一键测试网流程</h3>
          </div>
          <label>
            <span>运行秒数</span>
            <input v-model="testRunSeconds" type="number" min="20" step="10" />
          </label>
          <button class="primary-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(testnetRunAction())">
            <Play :size="18" />
            执行测试流程
          </button>
          <button class="danger-button" type="button" :disabled="Boolean(actionBusy)" @click="openAction(safetySweepAction())">
            <Trash2 :size="18" />
            仅执行安全清扫
          </button>
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
