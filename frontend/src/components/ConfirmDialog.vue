<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { AlertTriangle, X } from '@lucide/vue'

const props = defineProps<{
  open: boolean
  title: string
  description: string
  confirmationText: string
  busy?: boolean
  danger?: boolean
}>()

const emit = defineEmits<{
  cancel: []
  confirm: [reason: string]
}>()

const input = ref('')
const reason = ref('控制台人工操作')
const confirmationInput = ref<HTMLInputElement | null>(null)
const canSubmit = computed(() => input.value.trim() === props.confirmationText && !props.busy)

watch(
  () => props.open,
  async (open) => {
    if (!open) {
      return
    }
    input.value = ''
    reason.value = '控制台人工操作'
    await nextTick()
    confirmationInput.value?.focus()
  },
)

function submit() {
  if (canSubmit.value) {
    emit('confirm', reason.value.trim() || '控制台人工操作')
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dialog-backdrop" @click.self="emit('cancel')">
      <section
        class="confirm-dialog"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="'confirm-dialog-title'"
      >
        <header>
          <span class="dialog-icon" :class="{ 'dialog-icon--danger': danger }">
            <AlertTriangle :size="20" aria-hidden="true" />
          </span>
          <div>
            <h2 id="confirm-dialog-title">{{ title }}</h2>
            <p>{{ description }}</p>
          </div>
          <button class="icon-button" type="button" aria-label="关闭确认窗口" @click="emit('cancel')">
            <X :size="20" />
          </button>
        </header>

        <label class="field">
          <span>操作原因</span>
          <input v-model="reason" type="text" autocomplete="off">
        </label>

        <label class="field">
          <span>输入 <strong>{{ confirmationText }}</strong> 以确认</span>
          <input
            ref="confirmationInput"
            v-model="input"
            type="text"
            autocomplete="off"
            :aria-invalid="input.length > 0 && input.trim() !== confirmationText"
            @keydown.enter.prevent="submit"
          >
        </label>

        <footer>
          <button class="button button--ghost" type="button" :disabled="busy" @click="emit('cancel')">
            取消
          </button>
          <button
            class="button"
            :class="danger ? 'button--danger' : 'button--primary'"
            type="button"
            :disabled="!canSubmit"
            @click="submit"
          >
            {{ busy ? '正在提交…' : '确认执行' }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
