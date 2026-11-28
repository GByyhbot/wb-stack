<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{
  open: boolean
  accountName: string
  initialNote: string
  loading?: boolean
}>()

const emit = defineEmits<{
  save: [note: string]
  cancel: []
}>()

const draft = ref('')
watch(
  () => props.open,
  (v) => {
    if (v) draft.value = props.initialNote
  },
)
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
        <h3 class="text-sm font-semibold">编辑备注 · {{ accountName }}</h3>
        <p class="mt-1 text-xs text-[var(--text-muted)]">留空并保存可清除备注</p>
        <input
          v-model="draft"
          type="text"
          placeholder="输入备注内容…"
          class="mt-3 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs text-[var(--text-primary)] outline-none focus:border-indigo-500"
          @keyup.enter="emit('save', draft.trim())"
        />
        <div class="mt-4 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3.5 py-2 text-xs text-[var(--text-secondary)] cursor-pointer"
            :disabled="loading"
            @click="emit('cancel')"
          >
            取消
          </button>
          <button
            type="button"
            class="rounded-xl bg-indigo-600 px-3.5 py-2 text-xs font-medium text-white hover:bg-indigo-500 cursor-pointer disabled:opacity-50"
            :disabled="loading"
            @click="emit('save', draft.trim())"
          >
            {{ loading ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
