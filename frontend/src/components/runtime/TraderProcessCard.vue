<script setup lang="ts">
import { Play, CircleStop, RefreshCw, ServerCog } from '@lucide/vue'
import StatusBadge from '../StatusBadge.vue'
import type { TraderProcessState } from '../../mock'

const props = defineProps<{
  process: TraderProcessState
  busy?: boolean
}>()

const emit = defineEmits<{
  start: []
  stop: []
  restart: []
}>()

function tone() {
  const state = props.process.processState || props.process.state
  if (state === 'ONLINE' || state === 'running') return 'good'
  if (state === 'STALE' || state === 'STARTING' || state === 'starting') return 'warning'
  if (state === 'FAILED' || state === 'failed') return 'danger'
  return 'neutral'
}
</script>

<template>
  <section class="trader-process-card">
    <header>
      <div>
        <p class="eyebrow">交易进程</p>
        <h3>{{ process.processState || process.state }}</h3>
      </div>
      <StatusBadge :tone="tone()" :label="`Trader ${process.processState || process.state}`" />
    </header>
    <dl>
      <div><dt>PID</dt><dd>{{ process.pid ?? '—' }}</dd></div>
      <div><dt>心跳</dt><dd>{{ process.heartbeatAt || '—' }}</dd></div>
      <div>
        <dt>心跳年龄</dt>
        <dd>{{ process.heartbeatAgeSeconds == null ? '—' : `${Math.round(process.heartbeatAgeSeconds)}s` }}</dd>
      </div>
      <div><dt>运行状态</dt><dd>{{ process.runtimeState || '—' }}</dd></div>
      <div><dt>最后状态</dt><dd>{{ process.lastStatus || '—' }}</dd></div>
      <div><dt>最后错误</dt><dd>{{ process.lastError || '—' }}</dd></div>
      <div><dt>控制模式</dt><dd>{{ process.processControlMode || process.mode }}</dd></div>
    </dl>
    <div class="button-row">
      <button
        v-if="(process.processState || process.state) !== 'ONLINE' && process.state !== 'running'"
        class="button button--secondary"
        type="button"
        :disabled="busy"
        @click="emit('start')"
      >
        <Play :size="16" />启动
      </button>
      <button class="button button--secondary" type="button" :disabled="busy" @click="emit('restart')">
        <RefreshCw :size="16" />重启
      </button>
      <button class="button button--danger-outline" type="button" :disabled="busy" @click="emit('stop')">
        <CircleStop :size="16" />停止
      </button>
    </div>
    <p class="hint"><ServerCog :size="14" /> 停止进程不会自动平仓。</p>
  </section>
</template>

<style scoped>
.trader-process-card {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 12px;
  padding: 1rem;
  background: rgba(15, 23, 42, 0.35);
  display: grid;
  gap: 0.85rem;
}
header {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}
h3 {
  margin: 0.15rem 0 0;
}
dl {
  display: grid;
  gap: 0.35rem;
  margin: 0;
}
dl > div {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}
dt {
  opacity: 0.7;
}
dd {
  margin: 0;
}
.button-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.hint {
  margin: 0;
  opacity: 0.7;
  font-size: 0.8rem;
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
</style>
