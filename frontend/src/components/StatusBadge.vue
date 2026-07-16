<script setup lang="ts">
withDefaults(defineProps<{
  tone?: 'good' | 'warning' | 'danger' | 'info' | 'neutral'
  label: string
}>(), {
  tone: 'neutral',
})
</script>

<template>
  <span class="status-badge" :class="`status-badge--${tone}`">
    <span class="status-badge__dot" aria-hidden="true" />
    {{ label }}
  </span>
</template>
