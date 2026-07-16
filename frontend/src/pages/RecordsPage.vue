<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AlertTriangle, CheckCircle2, Download, RefreshCw, Search } from '@lucide/vue'
import StatusBadge from '../components/StatusBadge.vue'
import {
  loadV2OrderReconciliation,
  type V2OrderReconciliation,
} from '../api'
import type { AuditLog, GridSession } from '../mock'

const props = defineProps<{
  accountId: string
  sessions: GridSession[]
  logs: AuditLog[]
}>()

const tab = ref<'reconciliation' | 'orders' | 'trades' | 'logs'>('reconciliation')
const query = ref('')
const level = ref('all')
const selectedSessionId = ref<number | null>(props.sessions[0]?.id || null)
const reconciliation = ref<V2OrderReconciliation | null>(null)
const reconciliationLoading = ref(false)
const reconciliationError = ref('')

const orders = computed(() => props.sessions.flatMap((session) => session.orders.map((order) => ({
  ...order,
  sessionSymbol: session.symbol,
}))))
const trades = computed(() => props.sessions.flatMap((session) => session.trades.map((trade) => ({
  ...trade,
  sessionSymbol: session.symbol,
}))))
const filteredLogs = computed(() => props.logs.filter((item) => {
  const matchesLevel = level.value === 'all' || item.level === level.value
  const haystack = `${item.module} ${item.message} ${item.level}`.toLowerCase()
  return matchesLevel && haystack.includes(query.value.trim().toLowerCase())
}))

watch(
  () => props.sessions.map((item) => item.id),
  () => {
    if (!props.sessions.some((item) => item.id === selectedSessionId.value)) {
      selectedSessionId.value = props.sessions[0]?.id || null
    }
  },
)
watch(
  () => [props.accountId, selectedSessionId.value],
  () => {
    if (tab.value === 'reconciliation') void refreshReconciliation()
  },
)
watch(tab, (value) => {
  if (value === 'reconciliation') void refreshReconciliation()
})
onMounted(() => void refreshReconciliation())

async function refreshReconciliation() {
  if (selectedSessionId.value == null) {
    reconciliation.value = null
    return
  }
  reconciliationLoading.value = true
  reconciliationError.value = ''
  try {
    reconciliation.value = await loadV2OrderReconciliation(
      selectedSessionId.value,
      props.accountId,
    )
  } catch (reason) {
    reconciliation.value = null
    reconciliationError.value = reason instanceof Error ? reason.message : '无法执行订单对账'
  } finally {
    reconciliationLoading.value = false
  }
}

function exportCurrentView() {
  let rows: Array<Record<string, unknown>> = []
  if (tab.value === 'orders') rows = orders.value
  if (tab.value === 'trades') rows = trades.value
  if (tab.value === 'logs') rows = filteredLogs.value
  if (tab.value === 'reconciliation') rows = reconciliation.value?.differences || []
  if (!rows.length) return
  const headers = Array.from(new Set(rows.flatMap((row) => Object.keys(row))))
  const cell = (value: unknown) => `"${String(
    typeof value === 'object' && value != null ? JSON.stringify(value) : value ?? '',
  ).replaceAll('"', '""')}"`
  const csv = [
    headers.map(cell).join(','),
    ...rows.map((row) => headers.map((header) => cell(row[header])).join(',')),
  ].join('\n')
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' }))
  link.download = `quietgrid-${tab.value}-${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(link.href)
}

function money(value: number) {
  return `${value >= 0 ? '' : '-'}$${Math.abs(value).toFixed(4)}`
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Orders & Logs</p>
        <h2>订单、成交与审计记录</h2>
        <p>按会话查看本地投影；交易所对账差异会作为高优先级告警出现。</p>
      </div>
      <button class="button button--secondary" type="button" @click="exportCurrentView">
        <Download :size="17" />导出
      </button>
    </section>

    <nav class="subtabs" aria-label="记录类型">
      <button type="button" :class="{ active: tab === 'reconciliation' }" @click="tab = 'reconciliation'">
        对账差异 {{ reconciliation?.differences.length || 0 }}
      </button>
      <button type="button" :class="{ active: tab === 'orders' }" @click="tab = 'orders'">订单 {{ orders.length }}</button>
      <button type="button" :class="{ active: tab === 'trades' }" @click="tab = 'trades'">成交 {{ trades.length }}</button>
      <button type="button" :class="{ active: tab === 'logs' }" @click="tab = 'logs'">系统日志 {{ logs.length }}</button>
    </nav>

    <section class="panel">
      <div v-if="tab === 'reconciliation'" class="reconciliation-toolbar">
        <label class="compact-select">
          <span>会话</span>
          <select v-model="selectedSessionId">
            <option v-for="session in sessions" :key="session.id" :value="session.id">
              {{ session.symbol }} · #{{ session.id }} · {{ session.stateLabel }}
            </option>
          </select>
        </label>
        <button class="button button--secondary" type="button" :disabled="reconciliationLoading || selectedSessionId == null" @click="refreshReconciliation">
          <RefreshCw :size="16" :class="{ spin: reconciliationLoading }" />
          重新对账
        </button>
        <span class="muted">{{ reconciliation?.checkedAt || '尚未检查' }}</span>
      </div>
      <div v-if="tab === 'logs'" class="filter-bar">
        <label class="search-field">
          <Search :size="18" />
          <input v-model="query" type="search" placeholder="搜索模块或消息" aria-label="搜索日志">
        </label>
        <label class="compact-select">
          <span>级别</span>
          <select v-model="level">
            <option value="all">全部</option>
            <option value="信息">信息</option>
            <option value="警告">警告</option>
            <option value="错误">错误</option>
          </select>
        </label>
      </div>

      <div v-if="tab === 'reconciliation'" class="reconciliation-view">
        <div v-if="reconciliationError" class="inline-alert inline-alert--danger">
          <AlertTriangle :size="19" />
          <span><strong>对账失败</strong>{{ reconciliationError }}</span>
        </div>
        <div
          v-else-if="reconciliation"
          class="reconciliation-status"
          :class="{ 'reconciliation-status--danger': !reconciliation.consistent }"
        >
          <CheckCircle2 v-if="reconciliation.consistent" :size="22" />
          <AlertTriangle v-else :size="22" />
          <span>
            <strong>{{ reconciliation.consistent ? '交易所与本地投影一致' : '发现需要处理的订单差异' }}</strong>
            本地开放订单 {{ reconciliation.localOrders.length }}，交易所开放订单
            {{ reconciliation.exchangeOrders.length }}，差异 {{ reconciliation.differences.length }}。
            <template v-if="reconciliation.error"> {{ reconciliation.error }}</template>
          </span>
        </div>
        <div v-if="reconciliation?.differences.length" class="table-wrap">
          <table>
            <thead><tr><th>严重度</th><th>类型</th><th>订单</th><th>说明</th><th>本地</th><th>交易所</th></tr></thead>
            <tbody>
              <tr v-for="(difference, index) in reconciliation.differences" :key="`${difference.type}-${difference.orderId}-${index}`">
                <td><StatusBadge :tone="difference.severity === 'CRITICAL' ? 'danger' : 'warning'" :label="difference.severity" /></td>
                <td>{{ difference.type }}</td>
                <td class="mono">{{ difference.clientId || difference.orderId || '—' }}</td>
                <td>{{ difference.message }}</td>
                <td><details><summary>字段</summary><pre>{{ JSON.stringify(difference.local, null, 2) }}</pre></details></td>
                <td><details><summary>字段</summary><pre>{{ JSON.stringify(difference.exchange, null, 2) }}</pre></details></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else-if="reconciliation && !reconciliationLoading" class="empty-inline">
          {{ reconciliation.status === 'ok' ? '没有对账差异' : '交易所订单尚不可读取' }}
        </div>
        <div v-else-if="reconciliationLoading" class="empty-inline">正在读取交易所开放订单并与本地投影比较…</div>
        <div v-else class="empty-inline">请选择会话执行对账</div>
      </div>

      <div v-else-if="tab === 'orders'" class="table-wrap">
        <table>
          <thead><tr><th>会话</th><th>订单 ID</th><th>格位</th><th>方向</th><th>价格</th><th>数量</th><th>状态</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="order in orders" :key="order.id">
              <td><strong>{{ order.sessionSymbol }}</strong><small class="table-subline">#{{ order.sessionId }}</small></td>
              <td class="mono">{{ order.orderId }}</td>
              <td>#{{ order.gridIndex }}</td>
              <td><StatusBadge :tone="order.side === 'BUY' ? 'info' : 'warning'" :label="order.sideLabel" /></td>
              <td>{{ order.price.toFixed(6) }}</td>
              <td>{{ order.qty.toFixed(6) }}</td>
              <td>{{ order.statusLabel }}</td>
              <td>{{ order.createdAt }}</td>
            </tr>
            <tr v-if="!orders.length"><td colspan="8"><div class="empty-inline">暂无订单记录</div></td></tr>
          </tbody>
        </table>
      </div>

      <div v-else-if="tab === 'trades'" class="table-wrap">
        <table>
          <thead><tr><th>会话</th><th>时间</th><th>格位</th><th>方向</th><th>成交价</th><th>数量</th><th>网格利润</th><th>费用</th></tr></thead>
          <tbody>
            <tr v-for="trade in trades" :key="trade.id">
              <td><strong>{{ trade.sessionSymbol }}</strong><small class="table-subline">#{{ trade.sessionId }}</small></td>
              <td>{{ trade.tradeTime }}</td>
              <td>#{{ trade.gridIndex }}</td>
              <td><StatusBadge :tone="trade.side === 'BUY' ? 'info' : 'warning'" :label="trade.sideLabel" /></td>
              <td>{{ trade.price.toFixed(6) }}</td>
              <td>{{ trade.qty.toFixed(6) }}</td>
              <td :class="trade.gridPnl >= 0 ? 'positive' : 'negative'">{{ money(trade.gridPnl) }}</td>
              <td>{{ money(trade.fee + trade.fundingFee) }}</td>
            </tr>
            <tr v-if="!trades.length"><td colspan="8"><div class="empty-inline">暂无成交记录</div></td></tr>
          </tbody>
        </table>
      </div>

      <ol v-else class="log-list">
        <li v-for="(item, index) in filteredLogs" :key="`${item.time}-${index}`">
          <span class="log-marker" />
          <div>
            <header>
              <StatusBadge :tone="item.level === '错误' ? 'danger' : item.level === '警告' ? 'warning' : 'info'" :label="item.level" />
              <strong>{{ item.module }}</strong>
              <time>{{ item.time }}</time>
            </header>
            <p>{{ item.message }}</p>
          </div>
        </li>
        <li v-if="!filteredLogs.length" class="empty-inline">没有符合筛选条件的日志</li>
      </ol>
    </section>
  </div>
</template>
