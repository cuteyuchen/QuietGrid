<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  AlertTriangle,
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
import {
  loadV2SessionWorkspace,
  type V2DashboardData,
  type V2SessionWorkspace,
} from '../api'
import type { GridSession } from '../mock'

const props = defineProps<{
  accountId: string
  sessions: GridSession[]
  dashboard: V2DashboardData
}>()

const emit = defineEmits<{
  close: [session: GridSession]
}>()

const selectedId = ref<number | null>(props.sessions[0]?.id || null)
const detailTab = ref<'grid' | 'inventory' | 'orders' | 'trades'>('grid')
const workspace = ref<V2SessionWorkspace | null>(null)
const workspaceLoading = ref(false)
const workspaceError = ref('')

watch(
  () => props.sessions.map((session) => session.id),
  () => {
    if (!props.sessions.some((session) => session.id === selectedId.value)) {
      selectedId.value = props.sessions[0]?.id || null
    }
  },
)
watch(
  () => [props.accountId, selectedId.value],
  () => void refreshWorkspace(),
  { immediate: true },
)

const selected = computed(() => props.sessions.find((session) => session.id === selectedId.value) || null)
const gridPlan = computed(() => workspace.value?.gridPlan || null)
const inventory = computed(() => workspace.value?.inventory || null)
const inventoryLots = computed(() => workspace.value?.inventoryLots || [])
const currentOrders = computed(() => workspace.value?.orders || selected.value?.orders || [])
const currentTrades = computed(() => workspace.value?.trades || selected.value?.trades || [])
const inventoryHistory = computed(() => workspace.value?.inventoryHistory || [])
const riskSnapshot = computed(() => workspace.value?.risk || null)
const lifecycleEvents = computed(() => (workspace.value?.events || [])
  .filter((item) => [
    'STATE_CHANGED',
    'REGIME_CHANGED',
    'GRID_PLAN_CREATED',
    'RISK_LIMIT_BREACHED',
    'COOLDOWN_STARTED',
    'COOLDOWN_ENDED',
  ].includes(item.eventType))
  .slice(-8)
  .reverse())

const gridLevels = computed(() => {
  const session = selected.value
  const plan = gridPlan.value
  const prices = plan?.prices.length
    ? [...plan.prices].sort((left, right) => right - left)
    : session && session.gridNum >= 1 && session.upper > session.lower
      ? Array.from(
        { length: session.gridNum + 1 },
        (_, index) => session.upper - ((session.upper - session.lower) / session.gridNum) * index,
      )
      : []
  if (!session || !prices.length) {
    return []
  }
  const center = plan?.center || (session.upper + session.lower) / 2
  return prices.map((levelPrice, index) => ({
    index,
    price: levelPrice,
    side: levelPrice > (session.position.markPrice || center) ? 'SELL' : 'BUY',
    order: currentOrders.value.find((order) => Math.abs(order.price - levelPrice) < 1e-8),
    weight: plan?.qtyWeights[index] ?? null,
  }))
})

const currentMarkPosition = computed(() => {
  const session = selected.value
  const upper = gridPlan.value?.upper || session?.upper || 0
  const lower = gridPlan.value?.lower || session?.lower || 0
  if (!session || upper <= lower || session.position.markPrice == null) {
    return 50
  }
  return Math.max(0, Math.min(100, ((upper - session.position.markPrice) / (upper - lower)) * 100))
})

const pnlValues = computed(() => selected.value?.performance.pnlCurve.map((point) => point.value) || [])
const inventoryValues = computed(() => inventoryHistory.value.map((item) => item.utilization))

async function refreshWorkspace() {
  if (selectedId.value == null) {
    workspace.value = null
    return
  }
  workspaceLoading.value = true
  workspaceError.value = ''
  try {
    workspace.value = await loadV2SessionWorkspace(selectedId.value, props.accountId)
  } catch (reason) {
    workspace.value = null
    workspaceError.value = reason instanceof Error ? reason.message : '无法加载会话 v2 工作区'
  } finally {
    workspaceLoading.value = false
  }
}

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
  if (['DEFENSIVE', 'COOLDOWN', 'REBALANCING', 'OBSERVING'].includes(state)) return 'warning'
  if (['CLOSING', 'ERROR'].includes(state)) return 'danger'
  return 'neutral'
}

function directionLabel(value: string) {
  return { NEUTRAL: '中性', LONG: '做多', SHORT: '做空' }[value] || value
}

function intentLabel(value: string) {
  return { OPEN: '开仓', REDUCE: '减仓', SEED: '种子', PROTECTION: '保护' }[value] || value
}

function eventLabel(value: string) {
  return {
    STATE_CHANGED: '状态变化',
    REGIME_CHANGED: '市场状态变化',
    GRID_PLAN_CREATED: '网格计划生成',
    RISK_LIMIT_BREACHED: '触发风险限制',
    COOLDOWN_STARTED: '进入冷却',
    COOLDOWN_ENDED: '冷却结束',
  }[value] || value
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
              <StatusBadge tone="info" :label="`${directionLabel(selected.directionMode)}网格`" />
            </div>
            <p>会话 #{{ selected.id }} · 参数窗口 #{{ selected.windowId }} · {{ selected.directionSource === 'symbol_override' ? '标的覆盖' : '全局模式' }}</p>
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
          <div><span>网格参数</span><strong>{{ gridPlan?.gridNum || selected.gridNum }} 格 · {{ pct(gridPlan?.stepPct ?? selected.stepPct) }}</strong></div>
          <div><span>持仓名义</span><strong>{{ money(selected.position.notional) }}</strong></div>
          <div><span>库存利用率</span><strong>{{ pct(inventory?.utilization ?? null) }}</strong></div>
          <div><span>软性违约</span><strong>{{ selected.softBreachCount || 0 }}/3</strong></div>
          <div><span>种子仓位</span><strong>{{ selected.seedQty ? `${selected.seedPositionSide} ${selected.seedQty.toFixed(6)}` : '无' }}</strong></div>
        </div>

        <div v-if="selected.state === 'DEFENSIVE'" class="inline-alert inline-alert--warning" role="status">
          <ShieldAlert :size="18" />
          <span><strong>防御模式 {{ selected.softBreachCount || 0 }}/3</strong>仅撤销增加库存的订单，不会因普通评分下降市价平仓；评分恢复后将对账并补齐网格。</span>
        </div>

        <dl v-if="selected.seedQty" class="metadata-grid metadata-grid--wide seed-summary">
          <div><dt>种子持仓侧</dt><dd>{{ selected.seedPositionSide }}</dd></div>
          <div><dt>成交价</dt><dd>{{ price(selected.seedEntryPrice) }}</dd></div>
          <div><dt>滑点</dt><dd>{{ pct(selected.seedSlippagePct) }}</dd></div>
          <div><dt>Taker 费用</dt><dd>{{ money(selected.seedFee, 4) }}</dd></div>
        </dl>

        <div v-if="workspaceLoading" class="inline-alert">
          <Clock3 :size="18" />
          正在同步网格计划、库存 Lot、风险快照和事件时间线…
        </div>
        <div v-else-if="workspaceError" class="inline-alert inline-alert--warning">
          <AlertTriangle :size="18" />
          <span><strong>详细快照暂不可用</strong>{{ workspaceError }}；仍显示基础会话数据。</span>
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
                  <small>
                    {{ level.order ? level.order.statusLabel : '等待挂单' }}
                    <template v-if="level.weight != null"> · 权重 {{ Number(level.weight).toFixed(2) }}</template>
                  </small>
                </div>
              </div>
              <div v-else class="empty-inline">等待网格参数</div>
              <dl v-if="gridPlan" class="metadata-grid">
                <div><dt>中枢</dt><dd>{{ price(gridPlan.center) }}</dd></div>
                <div><dt>区间</dt><dd>{{ price(gridPlan.lower) }} – {{ price(gridPlan.upper) }}</dd></div>
                <div><dt>成本地板</dt><dd>{{ pct(gridPlan.costFloorPct) }}</dd></div>
                <div><dt>Regime 分</dt><dd>{{ gridPlan.regimeScore?.toFixed(0) || '—' }}</dd></div>
                <div><dt>参数版本</dt><dd>{{ gridPlan.parameterVersion || '—' }}</dd></div>
                <div><dt>生成时间</dt><dd>{{ gridPlan.asOfTime || '—' }}</dd></div>
              </dl>
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
          <div v-if="inventory" class="inventory-overview">
            <div class="inventory-score">
              <strong>{{ inventory.riskScore.toFixed(0) }}</strong>
              <span>库存风险分</span>
              <StatusBadge
                :tone="inventory.riskLevel === 'NORMAL' ? 'good' : 'warning'"
                :label="inventory.riskLevel"
              />
            </div>
            <dl class="metadata-grid metadata-grid--wide">
              <div><dt>净数量</dt><dd>{{ inventory.netQty.toFixed(6) }}</dd></div>
              <div><dt>净名义仓位</dt><dd>{{ money(inventory.netNotional) }}</dd></div>
              <div><dt>毛名义仓位</dt><dd>{{ money(inventory.grossNotional) }}</dd></div>
              <div><dt>平均成本</dt><dd>{{ price(inventory.avgEntryPrice) }}</dd></div>
              <div><dt>未实现盈亏</dt><dd>{{ money(inventory.unrealizedPnl) }}</dd></div>
              <div><dt>未配对 Lot</dt><dd>{{ inventory.unpairedLots }}</dd></div>
            </dl>
          </div>
          <div v-else class="empty-state">
            <Box :size="30" />
            <h3>暂无此会话的库存快照</h3>
            <p>库存管理器产生快照后，会显示 Lot、利用率与预计最坏损失。</p>
          </div>
          <div v-if="inventoryHistory.length" class="chart-block">
            <div class="panel__header">
              <div><h3>库存利用率历史</h3><p>叠加阈值前的真实快照变化</p></div>
            </div>
            <MiniLineChart :values="inventoryValues" label="库存利用率历史" tone="danger" />
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>开仓时间</th><th>方向</th><th>开仓价</th><th>数量</th><th>来源格位</th><th>目标退出</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="lot in inventoryLots" :key="lot.id">
                  <td>{{ lot.openedAt || '—' }}</td>
                  <td><StatusBadge :tone="lot.side === 'BUY' || lot.side === 'LONG' ? 'info' : 'warning'" :label="lot.side" /></td>
                  <td>{{ price(lot.entryPrice) }}</td>
                  <td>{{ lot.qty.toFixed(6) }}</td>
                  <td>{{ lot.entryGridIndex == null ? '—' : `#${lot.entryGridIndex}` }}</td>
                  <td>{{ price(lot.targetExitPrice) }}</td>
                  <td>{{ lot.status }}</td>
                </tr>
                <tr v-if="!inventoryLots.length"><td colspan="7"><div class="empty-inline">当前没有未配对库存 Lot</div></td></tr>
              </tbody>
            </table>
          </div>
          <div v-if="riskSnapshot" class="risk-inline-summary">
            <ShieldAlert :size="20" />
            <span>
              <strong>{{ riskSnapshot.riskLevel }} · {{ riskSnapshot.action }}</strong>
              {{ riskSnapshot.reason }}
            </span>
          </div>
        </div>

        <div v-else-if="detailTab === 'orders'" class="detail-panel table-wrap">
          <table>
            <thead><tr><th>格位</th><th>方向</th><th>意图</th><th>持仓侧</th><th>价格</th><th>数量</th><th>状态</th><th>创建时间</th></tr></thead>
            <tbody>
              <tr v-for="order in currentOrders" :key="order.id">
                <td>{{ order.orderIntent === 'SEED' ? '种子' : `#${order.gridIndex}` }}</td>
                <td><StatusBadge :tone="order.side === 'BUY' ? 'info' : 'warning'" :label="order.sideLabel" /></td>
                <td><StatusBadge :tone="order.orderIntent === 'OPEN' ? 'info' : order.orderIntent === 'REDUCE' ? 'good' : 'warning'" :label="intentLabel(order.orderIntent)" /></td>
                <td>{{ order.positionSide || '—' }}</td>
                <td>{{ price(order.price) }}</td>
                <td>{{ order.qty.toFixed(6) }}</td>
                <td>{{ order.statusLabel }}</td>
                <td>{{ order.createdAt }}</td>
              </tr>
              <tr v-if="!currentOrders.length"><td colspan="8"><div class="empty-inline">暂无订单</div></td></tr>
            </tbody>
          </table>
        </div>

        <div v-else class="detail-panel table-wrap">
          <table>
            <thead><tr><th>时间</th><th>格位</th><th>方向</th><th>成交价</th><th>数量</th><th>网格利润</th><th>费用</th></tr></thead>
            <tbody>
              <tr v-for="trade in currentTrades" :key="trade.id">
                <td>{{ trade.tradeTime }}</td>
                <td>#{{ trade.gridIndex }}</td>
                <td><StatusBadge :tone="trade.side === 'BUY' ? 'info' : 'warning'" :label="trade.sideLabel" /></td>
                <td>{{ price(trade.price) }}</td>
                <td>{{ trade.qty.toFixed(6) }}</td>
                <td :class="trade.gridPnl >= 0 ? 'positive' : 'negative'">{{ money(trade.gridPnl, 4) }}</td>
                <td>{{ money(trade.fee + trade.fundingFee, 4) }}</td>
              </tr>
              <tr v-if="!currentTrades.length"><td colspan="7"><div class="empty-inline">暂无成交</div></td></tr>
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

        <section class="panel panel--embedded session-event-timeline">
          <div class="panel__header">
            <div>
              <p class="eyebrow">状态时间线</p>
              <h3>最近关键决策</h3>
            </div>
            <span class="muted">{{ workspace?.events.length || 0 }} 个事件</span>
          </div>
          <ol v-if="lifecycleEvents.length" class="compact-timeline">
            <li v-for="event in lifecycleEvents" :key="event.eventId">
              <span class="log-marker" />
              <div>
                <strong>{{ eventLabel(event.eventType) }}</strong>
                <time>{{ event.eventTime }}</time>
                <small>{{ JSON.stringify(event.payload) }}</small>
              </div>
            </li>
          </ol>
          <div v-else class="empty-inline">暂无关键事件快照</div>
        </section>
      </section>

      <section v-else class="panel empty-state">
        <PackageOpen :size="34" />
        <h2>还没有可以查看的会话</h2>
        <p>启动并通过市场状态与风险检查后，会话会出现在这里。</p>
      </section>
    </div>
  </div>
</template>
