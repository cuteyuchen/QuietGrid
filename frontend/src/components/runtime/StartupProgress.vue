<script setup lang="ts">
import { computed } from 'vue'
import { CheckCircle2, Circle, LoaderCircle } from '@lucide/vue'

export type StartupStep = {
  key: string
  label: string
  status: 'done' | 'active' | 'pending' | 'failed'
}

const props = defineProps<{
  steps: StartupStep[]
  title?: string
}>()

const doneCount = computed(() => props.steps.filter((step) => step.status === 'done').length)
</script>

<template>
  <section class="startup-progress" aria-label="启动进度">
    <header>
      <strong>{{ title || '启动进度' }}</strong>
      <small>{{ doneCount }}/{{ steps.length }}</small>
    </header>
    <ol>
      <li
        v-for="step in steps"
        :key="step.key"
        :class="`startup-progress__item startup-progress__item--${step.status}`"
      >
        <CheckCircle2 v-if="step.status === 'done'" :size="16" />
        <LoaderCircle v-else-if="step.status === 'active'" :size="16" class="spin" />
        <Circle v-else :size="16" />
        <span>{{ step.label }}</span>
        <em>
          {{
            step.status === 'done'
              ? '完成'
              : step.status === 'active'
                ? '进行中'
                : step.status === 'failed'
                  ? '失败'
                  : '等待'
          }}
        </em>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.startup-progress {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 12px;
  padding: 0.9rem 1rem;
  background: rgba(15, 23, 42, 0.35);
}
.startup-progress header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 0.75rem;
}
.startup-progress ol {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.55rem;
}
.startup-progress__item {
  display: grid;
  grid-template-columns: 16px 1fr auto;
  gap: 0.55rem;
  align-items: center;
  font-size: 0.9rem;
}
.startup-progress__item em {
  font-style: normal;
  opacity: 0.7;
  font-size: 0.8rem;
}
.startup-progress__item--done {
  color: #86efac;
}
.startup-progress__item--active {
  color: #93c5fd;
}
.startup-progress__item--failed {
  color: #fca5a5;
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
