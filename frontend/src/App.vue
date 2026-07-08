<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  Database,
  Gauge,
  History,
  LayoutDashboard,
  Play,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  Trash2,
} from '@lucide/vue'
import { loadConsoleData } from './api'
import {
  auditLogs as fallbackAuditLogs,
  sessions as fallbackSessions,
  summary as fallbackSummary,
  verificationRows as fallbackVerificationRows,
  volatilityOptions,
} from './mock'

const tabs = [
  { key: 'overview', label: '总览', icon: LayoutDashboard },
  { key: 'grids', label: '网格控制', icon: Activity },
  { key: 'strategy', label: '策略参数', icon: SlidersHorizontal },
  { key: 'testnet', label: '测试网验证', icon: ShieldCheck },
  { key: 'logs', label: '日志审计', icon: History },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('overview')
const selectedVolatility = ref('std')
const testRunSeconds = ref(600)
const paused = ref(false)
const loading = ref(false)
const dataError = ref('')
const summary = ref(fallbackSummary)
const sessions = ref(fallbackSessions)
const verificationRows = ref(fallbackVerificationRows)
const auditLogs = ref(fallbackAuditLogs)

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

function formatAuditModule(value: string) {
  return auditModuleLabels[value] ?? value
}

async function refreshData() {
  loading.value = true
  try {
    const data = await loadConsoleData()
    summary.value = data.summary
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
          <button class="danger-button" type="button">
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
            <button class="primary-button" type="button">
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
          <button class="secondary-button" type="button" @click="paused = !paused">
            <CirclePause :size="18" />
            {{ paused ? '恢复新开仓' : '暂停新开仓' }}
          </button>
          <button class="danger-button" type="button">
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
              <span>操作</span>
            </div>
            <div v-for="session in sessions" :key="session.id" class="table-row" role="row">
              <strong>{{ session.symbol }}</strong>
              <span class="state-pill">{{ session.stateLabel || formatState(session.state) }}</span>
              <span>{{ session.lower.toFixed(2) }} - {{ session.upper.toFixed(2) }}</span>
              <span>{{ session.gridNum }} / {{ formatPct(session.stepPct) }}</span>
              <span>{{ formatPct(session.currentVolatility) }}</span>
              <button class="compact-danger" type="button">停止</button>
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
          <label>
            <span>波动率算法</span>
            <select v-model="selectedVolatility">
              <option v-for="option in volatilityOptions" :key="option" :value="option">
                {{ formatVolatilityMethod(option) }}
              </option>
            </select>
          </label>
          <label>
            <span>最大并发标的</span>
            <input value="3" type="number" min="1" max="5" />
          </label>
          <label>
            <span>观察窗口分钟</span>
            <input value="180" type="number" min="30" />
          </label>
          <label>
            <span>最小网格步长</span>
            <input value="0.0015" type="number" step="0.0001" />
          </label>
          <button class="primary-button wide" type="button">
            <CheckCircle2 :size="18" />
            保存为下轮生效
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
          <button class="primary-button" type="button">
            <Play :size="18" />
            执行测试流程
          </button>
          <button class="danger-button" type="button">
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
  </main>
</template>
