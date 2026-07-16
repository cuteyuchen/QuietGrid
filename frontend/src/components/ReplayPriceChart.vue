<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  prices: number[]
  gridPrices: number[]
  lower: number
  upper: number
  currentPrice: number | null
}>()

const width = 900
const height = 360
const padding = 28
const domain = computed(() => {
  const candidates = [
    ...props.prices,
    ...props.gridPrices,
    props.lower,
    props.upper,
    props.currentPrice,
  ].filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  const min = Math.min(...candidates)
  const max = Math.max(...candidates)
  if (!candidates.length || min === max) {
    return { min: (min || 0) - 1, max: (max || 0) + 1 }
  }
  const margin = (max - min) * 0.08
  return { min: min - margin, max: max + margin }
})

function y(value: number) {
  const range = domain.value.max - domain.value.min || 1
  return padding + (1 - (value - domain.value.min) / range) * (height - padding * 2)
}

const linePoints = computed(() => props.prices.map((value, index) => {
  const x = padding + (index / Math.max(1, props.prices.length - 1)) * (width - padding * 2)
  return `${x.toFixed(2)},${y(value).toFixed(2)}`
}).join(' '))

const lastPoint = computed(() => {
  if (!props.prices.length) return null
  return {
    x: props.prices.length === 1 ? padding : width - padding,
    y: y(props.prices.at(-1) || 0),
  }
})
</script>

<template>
  <div class="replay-price-chart" role="img" :aria-label="prices.length ? `已回放 ${prices.length} 个价格事件，当前价格 ${currentPrice ?? '未知'}` : '当前没有价格事件'">
    <svg :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none" aria-hidden="true">
      <g class="replay-price-chart__grid">
        <line
          v-for="(gridPrice, index) in gridPrices"
          :key="`${gridPrice}-${index}`"
          :x1="padding"
          :x2="width - padding"
          :y1="y(gridPrice)"
          :y2="y(gridPrice)"
        />
      </g>
      <line class="replay-price-chart__bound" :x1="padding" :x2="width - padding" :y1="y(upper)" :y2="y(upper)" />
      <line class="replay-price-chart__bound replay-price-chart__bound--lower" :x1="padding" :x2="width - padding" :y1="y(lower)" :y2="y(lower)" />
      <polyline v-if="linePoints" class="replay-price-chart__line" :points="linePoints" />
      <circle v-if="lastPoint" class="replay-price-chart__point" :cx="lastPoint.x" :cy="lastPoint.y" r="7" />
    </svg>
    <div class="replay-price-chart__labels">
      <span>上沿 {{ upper.toLocaleString('en-US', { maximumFractionDigits: 6 }) }}</span>
      <strong>{{ currentPrice == null ? '等待价格事件' : `当前 ${currentPrice.toLocaleString('en-US', { maximumFractionDigits: 6 })}` }}</strong>
      <span>下沿 {{ lower.toLocaleString('en-US', { maximumFractionDigits: 6 }) }}</span>
    </div>
  </div>
</template>
