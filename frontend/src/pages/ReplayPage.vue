<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  ChevronLeft,
  ChevronRight,
  Pause,
  Play,
  RotateCcw,
  SkipBack,
  SkipForward,
} from '@lucide/vue'
import { loadV2SessionEvents, type V2SessionEvent } from '../api'
import StatusBadge from '../components/StatusBadge.vue'
import type { GridSession } from '../mock'

const props = defineProps<{
  accountId: string
  sessions: GridSession[]
}>()

const selectedSessionId = ref<number | null>(props.sessions[0]?.id || null)
const events = ref<V2SessionEvent[]>([])
const cursor = ref(0)
const playing = ref(false)
const speed = ref(1)
const loading = ref(false)
const error = ref('')
let timer: number | undefined

const currentSession = computed(() => props.sessions.find((item) => item.id === selectedSessionId.value) || null)
const currentEvent = computed(() => events.value[cursor.value] || null)
const progress = computed(() => events.value.length <= 1 ? 0 : cursor.value / (events.value.length - 1) * 100)

watch(selectedSessionId, loadEvents, { immediate: true })
watch(() => props.accountId, loadEvents)
watch([playing, speed], schedule)
onUnmounted(stopTimer)

async function loadEvents() {
  stop()
  events.value = []
  cursor.value = 0
  if (selectedSessionId.value == null) return
  loading.value = true
  error.value = ''
  try {
    events.value = await loadV2SessionEvents(selectedSessionId.value, props.accountId)
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
  }
  return labels[type] || type || '事件'
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
          <div class="replay-price-zone">
            <span class="zone-label zone-label--upper">上沿 {{ currentSession.upper.toFixed(4) }}</span>
            <div
              v-for="level in Math.min(currentSession.gridNum + 1, 20)"
              :key="level"
              class="replay-grid-line"
              :style="{ top: `${(level - 1) / Math.max(1, Math.min(currentSession.gridNum, 19)) * 100}%` }"
            />
            <span class="zone-label zone-label--lower">下沿 {{ currentSession.lower.toFixed(4) }}</span>
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
