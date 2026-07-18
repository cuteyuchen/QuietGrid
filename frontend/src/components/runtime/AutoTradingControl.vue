<script setup lang="ts">
import { Play, CircleStop } from '@lucide/vue'
import StatusBadge from '../StatusBadge.vue'
import type { AutoTradingUiState } from '../../api'

defineProps<{
  state: AutoTradingUiState
  traderState: string
  windowLabel: string
  minutesToForceClose?: number | null
  nextScanAt?: string
  busy?: boolean
}>()

const emit = defineEmits<{
  start: []
  stop: []
}>()
</script>

<template>
  <section class="auto-trading-control">
    <div class="auto-trading-control__head">
      <div>
        <p class="eyebrow">自动交易</p>
        <h3>{{ state.transitioning ? (state.enabled ? '正在停止…' : '正在启动…') : state.enabled ? '已开启' : '已停止' }}</h3>
      </div>
      <StatusBadge
        :tone="state.transitioning ? 'info' : state.enabled ? 'good' : 'warning'"
        :label="state.transitioning ? state.transitionState : state.enabled ? 'AUTO ON' : 'AUTO OFF'"
      />
    </div>
    <dl>
      <div><dt>Trader</dt><dd>{{ traderState }}</dd></div>
      <div><dt>窗口</dt><dd>{{ windowLabel || '—' }}</dd></div>
      <div>
        <dt>距强制离场</dt>
        <dd>{{ minutesToForceClose == null ? '—' : `${Math.round(minutesToForceClose)} 分钟` }}</dd>
      </div>
      <div><dt>下次评估</dt><dd>{{ nextScanAt || '—' }}</dd></div>
    </dl>
    <div class="button-row">
      <button
        v-if="!state.enabled"
        class="button button--primary"
        type="button"
        :disabled="busy || !state.canStart"
        @click="emit('start')"
      >
        <Play :size="16" />{{ state.transitioning ? '正在启动…' : '启动自动交易' }}
      </button>
      <button
        v-else
        class="button button--danger-outline"
        type="button"
        :disabled="busy || !state.canStop"
        @click="emit('stop')"
      >
        <CircleStop :size="16" />{{ state.transitioning ? '正在停止…' : '停止自动交易' }}
      </button>
    </div>
    <p v-if="state.blockedReason" class="hint hint--warning">{{ state.blockedReason }}</p>
    <p class="hint">停止自动交易 ≠ 立即平仓。需要平仓请使用“停止本轮”或“安全退出”。</p>
  </section>
</template>

<style scoped>
.auto-trading-control {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 12px;
  padding: 1rem;
  background: rgba(15, 23, 42, 0.35);
  display: grid;
  gap: 0.85rem;
}
.auto-trading-control__head {
  display: flex;
  justify-content: space-between;
  align-items: start;
}
.auto-trading-control h3 {
  margin: 0.15rem 0 0;
}
.auto-trading-control dl {
  display: grid;
  gap: 0.35rem;
  margin: 0;
}
.auto-trading-control dl > div {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}
.auto-trading-control dt {
  opacity: 0.7;
}
.auto-trading-control dd {
  margin: 0;
}
.hint {
  margin: 0;
  opacity: 0.7;
  font-size: 0.8rem;
}
.button-row {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
</style>
