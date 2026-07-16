<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  values: number[]
  height?: number
  label: string
  tone?: 'primary' | 'good' | 'danger'
}>(), {
  height: 180,
  tone: 'primary',
})

const width = 800
const padding = 12
const points = computed(() => {
  if (props.values.length === 0) {
    return ''
  }
  const min = Math.min(...props.values)
  const max = Math.max(...props.values)
  const range = max - min || 1
  return props.values
    .map((value, index) => {
      const x = padding + (index / Math.max(1, props.values.length - 1)) * (width - padding * 2)
      const y = padding + (1 - (value - min) / range) * (props.height - padding * 2)
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
})

const summary = computed(() => {
  if (props.values.length === 0) {
    return `${props.label}暂无数据`
  }
  return `${props.label}，起点 ${props.values[0].toFixed(2)}，终点 ${props.values.at(-1)?.toFixed(2)}，最低 ${Math.min(...props.values).toFixed(2)}，最高 ${Math.max(...props.values).toFixed(2)}`
})
</script>

<template>
  <div class="mini-chart" role="img" :aria-label="summary">
    <svg v-if="values.length > 1" :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none">
      <line
        v-for="line in 4"
        :key="line"
        x1="0"
        :x2="width"
        :y1="(height / 5) * line"
        :y2="(height / 5) * line"
        class="mini-chart__grid"
      />
      <polyline :points="points" class="mini-chart__line" :class="`mini-chart__line--${tone}`" />
    </svg>
    <div v-else class="empty-inline">等待足够的数据点</div>
  </div>
</template>
