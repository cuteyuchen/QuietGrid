<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Pause,
  Play,
  RotateCcw,
  SkipBack,
  SkipForward,
} from '@lucide/vue'
import {
  loadV2SessionWorkspace,
  type V2SessionEvent,
  type V2SessionWorkspace,
} from '../api'
import ReplayPriceChart from '../components/ReplayPriceChart.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { GridSession } from '../mock'

const props = defineProps<{
  accountId: string
  sessions: GridSession[]
}>()

const selectedSessionId = ref<number | null>(props.sessions[0]?.id || null)
const events = ref<V2SessionEvent[]>([])
const workspace = ref<V2SessionWorkspace | null>(null)
const cursor = ref(0)
const playing = ref(false)
const speed = ref(1)
const loading = ref(false)
const error = ref('')
let timer: number | undefined

const currentSession = computed(() => props.sessions.find((item) => item.id === selectedSessionId.value) || null)
const currentEvent = computed(() => events.value[cursor.value] || null)
const progress = computed(() => events.value.length <= 1 ? 0 : cursor.value / (events.value.length - 1) * 100)
const gridPrices = computed(() => {
  if (workspace.value?.gridPlan?.prices.length) {
    return workspace.value.gridPlan.prices
  }
  const session = currentSession.value
  if (!session || session.gridNum < 1) return []
  return Array.from(
    { length: session.gridNum + 1 },
    (_, index) => session.lower + ((session.upper - session.lower) / session.gridNum) * index,
  )
})
const lower = computed(() => workspace.value?.gridPlan?.lower || currentSession.value?.lower || 0)
const upper = computed(() => workspace.value?.gridPlan?.upper || currentSession.value?.upper || 0)
const priceSeries = computed(() => events.value
  .slice(0, cursor.value + 1)
  .map((event) => eventPrice(event))
  .filter((value): value is number => value != null))
const currentPrice = computed(() => priceSeries.value.at(-1) ?? currentSession.value?.position.markPrice ?? null)
const currentRegimeEvent = computed(() => latestEventBeforeCursor(['REGIME_CHANGED', 'regime.decided']))
const currentRiskEvent = computed(() => latestEventBeforeCursor(['RISK_LIMIT_BREACHED', 'RISK_DECIDED', 'risk.decided']))
const currentInventoryEvent = computed(() => latestEventBeforeCursor(['INVENTORY_UPDATED', 'inventory.updated']))

watch(selectedSessionId, loadEvents, { immediate: true })
watch(() => props.accountId, loadEvents)
watch([playing, speed], schedule)
onUnmounted(stopTimer)

async function loadEvents() {
  stop()
  events.value = []
  workspace.value = null
  cursor.value = 0
  if (selectedSessionId.value == null) return
  loading.value = true
  error.value = ''
  try {
    workspace.value = await loadV2SessionWorkspace(selectedSessionId.value, props.accountId)
    events.value = workspace.value.events
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法读取会话事件'
  } finally {
    loading.value = false
  }
}

function schedule() {
  stopTimer()
  if (!playing.value || events.value.length < 2) return
  timer = window.setInterval(() => {
    if (cursor.value >= events.value.length - 1) {
      stop()
      return
    }
    cursor.value += 1
  }, Math.max(200, 1000 / speed.value))
}

function stopTimer() {
  if (timer != null) {
    window.clearInterval(timer)
    timer = undefined
  }
}

function stop() {
  playing.value = false
  stopTimer()
}

function seek(value: number) {
  cursor.value = Math.max(0, Math.min(events.value.length - 1, value))
}

function eventLabel(type: string) {
  const labels: Record<string, string> = {
    'session.created': '会话创建',
    'session.state_changed': '状态变化',
    'regime.decided': '市场状态评估',
    'grid.planned': '生成网格',
    'inventory.updated': '库存更新',
    'risk.decided': '风险决策',
    'order.updated': '订单更新',
    'trade.created': '成交',
    SESSION_CREATED: '会话创建',
    STATE_CHANGED: '状态变化',
    REGIME_CHANGED: '市场状态评估',
    GRID_PLAN_CREATED: '生成网格',
    INVENTORY_UPDATED: '库存更新',
    RISK_DECIDED: '风险决策',
    RISK_LIMIT_BREACHED: '触发风险限制',
    ORDER_UPDATED: '订单更新',
    ORDER_FILLED: '订单成交',
    TRADE_CREATED: '成交',
    BAR_CLOSED: 'K 线收盘',
    COOLDOWN_STARTED: '进入冷却',
    COOLDOWN_ENDED: '冷却结束',
  }
  return labels[type] || type || '事件'
}

function eventPrice(event: V2SessionEvent): number | null {
  for (const key of ['close', 'price', 'mark_price', 'current_price', 'fill_price', 'stopped_at_price']) {
    const value = event.payload[key]
    const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN
    if (Number.isFinite(parsed) && parsed > 0) return parsed
  }
  return null
}

function latestEventBeforeCursor(types: string[]) {
  for (let index = cursor.value; index >= 0; index -= 1) {
    const event = events.value[index]
    if (event && types.includes(event.eventType)) return event
  }
  return null
}
</script>

<template>
  <div class="page-stack">
    <section class="page-intro">
      <div>
        <p class="eyebrow">Strategy Replay</p>
        <h2>策略事件重放</h2>
        <p>按真实发生顺序检查市场判断、网格、库存和风险动作，定位系统为何这样做。</p>
      </div>
      <label class="compact-select">
        <span>会话</span>
        <select v-model="selectedSessionId">
          <option v-for="session in sessions" :key="session.id" :value="session.id">
            #{{ session.id }} · {{ session.symbol }} · {{ session.stateLabel }}
          </option>
        </select>
      </label>
    </section>

    <div v-if="error" class="inline-alert inline-alert--danger" role="alert">{{ error }}</div>
    <div v-else class="replay-source-note">
      <StatusBadge tone="info" label="历史实盘事件" />
      <span>画布只显示当前游标之前已经发生的事件，不提前展示未来价格。</span>
      <button type="button" disabled title="需要记录反事实决策流后才能启用">严格系统路径对比（等待数据）</button>
    </div>

    <section class="replay-stage">
      <div class="replay-canvas">
        <header>
          <div>
            <p class="eyebrow">回放画布</p>
            <h2>{{ currentSession?.symbol || '请选择会话' }}</h2>
          </div>
          <StatusBadge :tone="playing ? 'good' : 'neutral'" :label="playing ? '正在播放' : '已暂停'" />
        </header>

        <div v-if="currentSession" class="replay-market">
          <ReplayPriceChart
            :prices="priceSeries"
            :grid-prices="gridPrices"
            :lower="lower"
            :upper="upper"
            :current-price="currentPrice"
          />
          <div class="replay-snapshot-grid">
            <article>
              <span>Regime</span>
              <strong>{{ currentRegimeEvent ? eventLabel(currentRegimeEvent.eventType) : '尚无快照' }}</strong>
              <small>{{ currentRegimeEvent?.payload.state || currentRegimeEvent?.payload.grid_score || '—' }}</small>
            </article>
            <article>
              <span>Risk</span>
              <strong>{{ currentRiskEvent ? eventLabel(currentRiskEvent.eventType) : workspace?.risk?.riskLevel || '尚无快照' }}</strong>
              <small>{{ currentRiskEvent?.payload.reason || workspace?.risk?.reason || '—' }}</small>
            </article>
            <article>
              <span>Inventory</span>
              <strong>{{ currentInventoryEvent ? eventLabel(currentInventoryEvent.eventType) : workspace?.inventory?.riskLevel || '尚无快照' }}</strong>
              <small>利用率 {{ ((workspace?.inventory?.utilization || 0) * 100).toFixed(1) }}%</small>
            </article>
          </div>
          <div class="replay-event-card">
            <template v-if="currentEvent">
              <small>{{ currentEvent.eventTime }}</small>
              <strong>{{ eventLabel(currentEvent.eventType) }}</strong>
              <span>{{ currentEvent.aggregateType }} · {{ currentEvent.aggregateId }}</span>
            </template>
            <template v-else>
              <strong>暂无可回放事件</strong>
              <span>新事件会写入事件存储并出现在这里。</span>
            </template>
          </div>
        </div>
        <div v-else class="empty-state">暂无会话</div>
      </div>

      <aside class="replay-inspector">
        <div class="panel__header">
          <div>
            <p class="eyebrow">当前事件</p>
            <h2>{{ currentEvent ? eventLabel(currentEvent.eventType) : '等待事件' }}</h2>
          </div>
        </div>
        <div v-if="currentEvent && currentEvent.availableTime > currentEvent.eventTime" class="inline-alert inline-alert--warning">
          <AlertTriangle :size="17" />
          该事件在发生后才可用；重放以 availableTime 作为策略可见时间。
        </div>
        <dl v-if="currentEvent" class="metadata-list">
          <div><dt>事件时间</dt><dd>{{ currentEvent.eventTime }}</dd></div>
          <div><dt>可用时间</dt><dd>{{ currentEvent.availableTime }}</dd></div>
          <div><dt>聚合对象</dt><dd>{{ currentEvent.aggregateType }}</dd></div>
          <div><dt>对象 ID</dt><dd>{{ currentEvent.aggregateId }}</dd></div>
        </dl>
        <pre v-if="currentEvent">{{ JSON.stringify(currentEvent.payload, null, 2) }}</pre>
        <div v-else class="empty-inline">{{ loading ? '正在加载事件…' : '暂无事件内容' }}</div>

        <div class="event-list">
          <button
            v-for="(event, index) in events"
            :key="event.eventId"
            type="button"
            :class="{ active: index === cursor }"
            @click="seek(index)"
          >
            <span>{{ index + 1 }}</span>
            <div><strong>{{ eventLabel(event.eventType) }}</strong><small>{{ event.eventTime }}</small></div>
            <ChevronRight :size="16" />
          </button>
        </div>
      </aside>
    </section>

    <section class="replay-controls" aria-label="回放控制">
      <button class="icon-button" type="button" aria-label="回到开始" :disabled="!events.length" @click="seek(0)">
        <RotateCcw :size="20" />
      </button>
      <button class="icon-button" type="button" aria-label="上一个事件" :disabled="cursor <= 0" @click="seek(cursor - 1)">
        <SkipBack :size="20" />
      </button>
      <button class="play-button" type="button" :disabled="events.length < 2" @click="playing = !playing">
        <Pause v-if="playing" :size="22" />
        <Play v-else :size="22" />
        {{ playing ? '暂停' : '播放' }}
      </button>
      <button class="icon-button" type="button" aria-label="下一个事件" :disabled="cursor >= events.length - 1" @click="seek(cursor + 1)">
        <SkipForward :size="20" />
      </button>
      <span class="replay-counter">{{ events.length ? cursor + 1 : 0 }} / {{ events.length }}</span>
      <input
        :value="cursor"
        type="range"
        min="0"
        :max="Math.max(0, events.length - 1)"
        :disabled="events.length < 2"
        aria-label="回放进度"
        @input="seek(Number(($event.target as HTMLInputElement).value))"
      >
      <span class="sr-only">进度 {{ progress.toFixed(0) }}%</span>
      <div class="speed-control" aria-label="播放速度">
        <button type="button" aria-label="降低播放速度" @click="speed = Math.max(0.5, speed / 2)"><ChevronLeft :size="16" /></button>
        <span>{{ speed }}×</span>
        <button type="button" aria-label="提高播放速度" @click="speed = Math.min(8, speed * 2)"><ChevronRight :size="16" /></button>
      </div>
    </section>
  </div>
</template>
