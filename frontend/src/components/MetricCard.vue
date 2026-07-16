<script setup lang="ts">
defineProps<{
  label: string
  value: string
  hint?: string
  tone?: 'default' | 'good' | 'warning' | 'danger'
}>()
</script>

<template>
  <article class="metric-card" :class="tone && tone !== 'default' ? `metric-card--${tone}` : ''">
    <p>{{ label }}</p>
    <strong>{{ value }}</strong>
    <small v-if="hint">{{ hint }}</small>
  </article>
</template>
