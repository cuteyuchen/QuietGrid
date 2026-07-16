<script setup lang="ts">
import { computed, ref } from 'vue'
import { Download, Search } from '@lucide/vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { AuditLog, GridSession } from '../mock'

const props = defineProps<{
  sessions: GridSession[]
  logs: AuditLog[]
}>()

const tab = ref<'orders' | 'trades' | 'logs'>('orders')
const query = ref('')
const level = ref('all')

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
      <button class="button button--secondary" type="button" disabled title="导出接口将在审计 API 完成后启用">
        <Download :size="17" />导出
      </button>
    </section>

    <nav class="subtabs" aria-label="记录类型">
      <button type="button" :class="{ active: tab === 'orders' }" @click="tab = 'orders'">订单 {{ orders.length }}</button>
      <button type="button" :class="{ active: tab === 'trades' }" @click="tab = 'trades'">成交 {{ trades.length }}</button>
      <button type="button" :class="{ active: tab === 'logs' }" @click="tab = 'logs'">系统日志 {{ logs.length }}</button>
    </nav>

    <section class="panel">
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

      <div v-if="tab === 'orders'" class="table-wrap">
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
