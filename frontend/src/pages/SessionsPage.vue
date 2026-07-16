<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  ArrowDownToLine,
  Box,
  Clock3,
  Layers3,
  ListChecks,
  PackageOpen,
  ShieldAlert,
  XCircle,
} from '@lucide/vue'
import MiniLineChart from '../components/MiniLineChart.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { V2DashboardData } from '../api'
import type { GridSession } from '../mock'

const props = defineProps<{
  sessions: GridSession[]
  dashboard: V2DashboardData
}>()

const emit = defineEmits<{
  close: [session: GridSession]
}>()

const selectedId = ref<number | null>(props.sessions[0]?.id || null)
const detailTab = ref<'grid' | 'inventory' | 'orders' | 'trades'>('grid')

watch(
  () => props.sessions.map((session) => session.id),
  () => {
    if (!props.sessions.some((session) => session.id === selectedId.value)) {
      selectedId.value = props.sessions[0]?.id || null
    }
  },
)

const selected = computed(() => props.sessions.find((session) => session.id === selectedId.value) || null)

const gridLevels = computed(() => {
  const session = selected.value
  if (!session || session.gridNum < 1 || session.upper <= session.lower) {
    return []
  }
  const step = (session.upper - session.lower) / session.gridNum
  return Array.from({ length: session.gridNum + 1 }, (_, index) => ({
    index,
    price: session.upper - step * index,
    side: session.upper - step * index > (session.position.markPrice || (session.upper + session.lower) / 2)
      ? 'SELL'
      : 'BUY',
    order: session.orders.find((order) => order.gridIndex === session.gridNum - index),
  }))
})

const currentMarkPosition = computed(() => {
  const session = selected.value
  if (!session || session.upper <= session.lower || session.position.markPrice == null) {
    return 50
  }
  return Math.max(0, Math.min(100, ((session.upper - session.position.markPrice) / (session.upper - session.lower)) * 100))
})

const pnlValues = computed(() => selected.value?.performance.pnlCurve.map((point) => point.value) || [])

function money(value: number | null | undefined, digits = 2) {
  return value == null ? '—' : `${value >= 0 ? '' : '-'}$${Math.abs(value).toFixed(digits)}`
}

function price(value: number | null | undefined) {
  return value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 6 })
}

function pct(value: number | null | undefined) {
  return value == null ? '—' : `${(value * 100).toFixed(2)}%`
}

function stateTone(state: string) {
  if (state === 'RUNNING') return 'good'
  if (['COOLDOWN', 'REBALANCING', 'OBSERVING'].includes(state)) return 'warning'
  if (['CLOSING', 'ERROR'].includes(state)) return 'danger'
  return 'neutral'
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Sessions & Inventory</p>
        <h2>会话、网格与真实库存</h2>
        <p>先选择会话，再查看网格、库存、订单和成交；危险操作始终留在同一位置。</p>
      </div>
      <StatusBadge
        :tone="sessions.length ? 'good' : 'neutral'"
        :label="`${sessions.length} 个会话`"
      />
    </section>

    <div class="session-workspace">
      <aside class="session-selector" aria-label="会话列表">
        <button
          v-for="session in sessions"
          :key="session.id"
          type="button"
          :class="{ active: selectedId === session.id }"
          @click="selectedId = session.id"
        >
          <span>
            <strong>{{ session.symbol }}</strong>
            <StatusBadge :tone="stateTone(session.state)" :label="session.stateLabel" />
          </span>
          <small>#{{ session.id }} · {{ session.openTime }}</small>
          <b :class="session.pnl >= 0 ? 'positive' : 'negative'">{{ money(session.pnl) }}</b>
        </button>
        <div v-if="!sessions.length" class="empty-state empty-state--compact">
          <PackageOpen :size="28" />
          <p>当前没有会话</p>
        </div>
      </aside>

      <section v-if="selected" class="session-detail">
        <header class="session-detail__header">
          <div>
            <div class="title-row">
              <h2>{{ selected.symbol }}</h2>
              <StatusBadge :tone="stateTone(selected.state)" :label="selected.stateLabel" />
            </div>
            <p>会话 #{{ selected.id }} · 参数窗口 #{{ selected.windowId }}</p>
          </div>
          <button
            v-if="!['STOPPED', 'CLOSED'].includes(selected.state)"
            class="button button--danger-outline"
            type="button"
            @click="emit('close', selected)"
          >
            <XCircle :size="17" />
            请求关闭
          </button>
          <StatusBadge v-else tone="neutral" label="会话已结束" />
        </header>

        <div class="session-kpis">
          <div><span>已实现盈亏</span><strong :class="selected.pnl >= 0 ? 'positive' : 'negative'">{{ money(selected.pnl) }}</strong></div>
          <div><span>未实现盈亏</span><strong :class="(selected.position.unrealizedPnl || 0) >= 0 ? 'positive' : 'negative'">{{ money(selected.position.unrealizedPnl) }}</strong></div>
          <div><span>当前价格</span><strong>{{ price(selected.position.markPrice) }}</strong></div>
          <div><span>网格参数</span><strong>{{ selected.gridNum }} 格 · {{ pct(selected.stepPct) }}</strong></div>
          <div><span>持仓名义</span><strong>{{ money(selected.position.notional) }}</strong></div>
          <div><span>库存利用率</span><strong>{{ pct(dashboard.latestInventory?.sessionId === selected.id ? dashboard.latestInventory.utilization : null) }}</strong></div>
        </div>

        <nav class="subtabs" aria-label="会话详情">
          <button type="button" :class="{ active: detailTab === 'grid' }" @click="detailTab = 'grid'"><Layers3 :size="17" />网格</button>
          <button type="button" :class="{ active: detailTab === 'inventory' }" @click="detailTab = 'inventory'"><Box :size="17" />库存</button>
          <button type="button" :class="{ active: detailTab === 'orders' }" @click="detailTab = 'orders'"><ListChecks :size="17" />订单</button>
          <button type="button" :class="{ active: detailTab === 'trades' }" @click="detailTab = 'trades'"><ArrowDownToLine :size="17" />成交</button>
        </nav>

        <div v-if="detailTab === 'grid'" class="detail-panel">
          <div class="content-grid content-grid--grid-detail">
            <section class="panel panel--embedded">
              <div class="panel__header">
                <div>
                  <p class="eyebrow">价格阶梯</p>
                  <h3>网格与当前价格</h3>
                </div>
                <span class="muted">硬止损 {{ price(selected.stopLossPrice) }}</span>
              </div>
              <div v-if="gridLevels.length" class="price-ladder">
                <div
                  class="price-ladder__mark"
                  :style="{ top: `${currentMarkPosition}%` }"
                  aria-label="当前价格位置"
                >
                  <span>当前 {{ price(selected.position.markPrice) }}</span>
                </div>
                <div
                  v-for="level in gridLevels"
                  :key="level.index"
                  class="price-level"
                  :class="`price-level--${level.side.toLowerCase()}`"
                >
                  <span>{{ level.side === 'BUY' ? '买' : '卖' }}</span>
                  <strong>{{ price(level.price) }}</strong>
                  <small>{{ level.order ? level.order.statusLabel : '等待挂单' }}</small>
                </div>
              </div>
              <div v-else class="empty-inline">等待网格参数</div>
            </section>

            <section class="panel panel--embedded">
              <div class="panel__header">
                <div>
                  <p class="eyebrow">会话绩效</p>
                  <h3>盈亏曲线</h3>
                </div>
              </div>
              <MiniLineChart :values="pnlValues" label="会话累计盈亏" :tone="selected.pnl >= 0 ? 'good' : 'danger'" />
              <dl class="metadata-grid">
                <div><dt>网格毛利润</dt><dd>{{ money(selected.performance.grossGridPnl) }}</dd></div>
                <div><dt>交易费用</dt><dd>{{ money(selected.performance.tradingFees) }}</dd></div>
                <div><dt>资金费</dt><dd>{{ money(selected.performance.fundingFee) }}</dd></div>
                <div><dt>未配对交易</dt><dd>{{ selected.performance.unpairedTradeCount }}</dd></div>
                <div><dt>运行时长</dt><dd>{{ selected.performance.durationHours?.toFixed(1) || '—' }} 小时</dd></div>
                <div><dt>收益率</dt><dd>{{ pct(selected.performance.roi) }}</dd></div>
              </dl>
            </section>
          </div>
        </div>

        <div v-else-if="detailTab === 'inventory'" class="detail-panel">
          <div v-if="dashboard.latestInventory?.sessionId === selected.id" class="inventory-overview">
            <div class="inventory-score">
              <strong>{{ dashboard.latestInventory.riskScore.toFixed(0) }}</strong>
              <span>库存风险分</span>
              <StatusBadge
                :tone="dashboard.latestInventory.riskLevel === 'NORMAL' ? 'good' : 'warning'"
                :label="dashboard.latestInventory.riskLevel"
              />
            </div>
            <dl class="metadata-grid metadata-grid--wide">
              <div><dt>净数量</dt><dd>{{ dashboard.latestInventory.netQty.toFixed(6) }}</dd></div>
              <div><dt>净名义仓位</dt><dd>{{ money(dashboard.latestInventory.netNotional) }}</dd></div>
              <div><dt>毛名义仓位</dt><dd>{{ money(dashboard.latestInventory.grossNotional) }}</dd></div>
              <div><dt>平均成本</dt><dd>{{ price(dashboard.latestInventory.avgEntryPrice) }}</dd></div>
              <div><dt>未实现盈亏</dt><dd>{{ money(dashboard.latestInventory.unrealizedPnl) }}</dd></div>
              <div><dt>未配对 Lot</dt><dd>{{ dashboard.latestInventory.unpairedLots }}</dd></div>
            </dl>
          </div>
          <div v-else class="empty-state">
            <Box :size="30" />
            <h3>暂无此会话的库存快照</h3>
            <p>库存管理器产生快照后，会显示 Lot、利用率与预计最坏损失。</p>
          </div>
        </div>

        <div v-else-if="detailTab === 'orders'" class="detail-panel table-wrap">
          <table>
            <thead><tr><th>格位</th><th>方向</th><th>价格</th><th>数量</th><th>状态</th><th>创建时间</th></tr></thead>
            <tbody>
              <tr v-for="order in selected.orders" :key="order.id">
                <td>#{{ order.gridIndex }}</td>
                <td><StatusBadge :tone="order.side === 'BUY' ? 'info' : 'warning'" :label="order.sideLabel" /></td>
                <td>{{ price(order.price) }}</td>
                <td>{{ order.qty.toFixed(6) }}</td>
                <td>{{ order.statusLabel }}</td>
                <td>{{ order.createdAt }}</td>
              </tr>
              <tr v-if="!selected.orders.length"><td colspan="6"><div class="empty-inline">暂无订单</div></td></tr>
            </tbody>
          </table>
        </div>

        <div v-else class="detail-panel table-wrap">
          <table>
            <thead><tr><th>时间</th><th>格位</th><th>方向</th><th>成交价</th><th>数量</th><th>网格利润</th><th>费用</th></tr></thead>
            <tbody>
              <tr v-for="trade in selected.trades" :key="trade.id">
                <td>{{ trade.tradeTime }}</td>
                <td>#{{ trade.gridIndex }}</td>
                <td><StatusBadge :tone="trade.side === 'BUY' ? 'info' : 'warning'" :label="trade.sideLabel" /></td>
                <td>{{ price(trade.price) }}</td>
                <td>{{ trade.qty.toFixed(6) }}</td>
                <td :class="trade.gridPnl >= 0 ? 'positive' : 'negative'">{{ money(trade.gridPnl, 4) }}</td>
                <td>{{ money(trade.fee + trade.fundingFee, 4) }}</td>
              </tr>
              <tr v-if="!selected.trades.length"><td colspan="7"><div class="empty-inline">暂无成交</div></td></tr>
            </tbody>
          </table>
        </div>

        <footer class="session-timeline">
          <Clock3 :size="18" />
          <div>
            <strong>{{ selected.volatilityStageLabel }}</strong>
            <span>开仓 {{ selected.openTime }} · 最近波动更新 {{ selected.currentVolatilityAt || '—' }}</span>
          </div>
          <StatusBadge
            :tone="selected.controlRequested || selected.stopRequested ? 'warning' : 'good'"
            :label="selected.controlRequested || selected.stopRequested ? '控制命令处理中' : '控制链路正常'"
          />
        </footer>
      </section>

      <section v-else class="panel empty-state">
        <PackageOpen :size="34" />
        <h2>还没有可以查看的会话</h2>
        <p>启动并通过市场状态与风险检查后，会话会出现在这里。</p>
      </section>
    </div>
  </div>
</template>
