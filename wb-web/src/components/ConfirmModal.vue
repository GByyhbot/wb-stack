<script setup lang="ts">
defineProps<{
  open: boolean
  title: string
  message: string
  confirmText?: string
  danger?: boolean
  loading?: boolean
}>()

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4"
      @click.self="emit('cancel')"
    >
      <div
        class="w-full max-w-md rounded-2xl border border-[var(--border-strong)] bg-[var(--bg-card)] p-5 shadow-2xl"
      >
        <h3 class="text-sm font-semibold text-[var(--text-primary)]">{{ title }}</h3>
        <p class="mt-2 text-xs leading-relaxed text-[var(--text-secondary)]">{{ message }}</p>
        <div class="mt-5 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3.5 py-2 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
            :disabled="loading"
            @click="emit('cancel')"
          >
            取消
          </button>
          <button
            type="button"
            class="rounded-xl px-3.5 py-2 text-xs font-medium text-white cursor-pointer disabled:opacity-50"
            :class="danger ? 'bg-rose-600 hover:bg-rose-500' : 'bg-indigo-600 hover:bg-indigo-500'"
            :disabled="loading"
            @click="emit('confirm')"
          >
            {{ loading ? '处理中…' : confirmText || '确定' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
