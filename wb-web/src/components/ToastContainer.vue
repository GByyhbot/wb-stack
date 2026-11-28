<script setup lang="ts">
import { useAppStore } from '@/stores/app'

const store = useAppStore()
</script>

<template>
  <div class="fixed top-5 right-5 z-50 flex flex-col gap-2 pointer-events-none">
    <div
      v-for="t in store.toasts"
      :key="t.id"
      class="pointer-events-auto rounded-xl px-4 py-2.5 text-xs shadow-xl border bg-[var(--bg-elevated)] text-[var(--text-primary)]"
      :class="t.ok ? 'border-[var(--green)]' : 'border-[var(--red)]'"
    >
      {{ t.message }}
    </div>
  </div>
</template>
