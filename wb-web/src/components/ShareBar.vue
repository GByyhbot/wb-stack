<script setup lang="ts">
import { computed } from 'vue'
import type { RecommendationShare } from '@/api'
import { SHARE_COLORS } from '@/utils/format'

const props = defineProps<{
  shares: RecommendationShare[]
}>()

// 占比条：各账号按 share_pct 横向堆叠，颜色与图例一致
const segments = computed(() =>
  props.shares.map((s, i) => ({
    key: `${s.account}:${i}`,
    width: Math.max(0, s.share_pct),
    color: SHARE_COLORS[i % SHARE_COLORS.length],
    title: `${s.account} ${s.share_pct}%`,
  })),
)
</script>

<template>
  <div>
    <div
      class="flex h-2.5 rounded-md overflow-hidden bg-[var(--bg-elevated)] border border-[var(--border-subtle)] min-w-[130px]"
    >
      <div
        v-for="seg in segments"
        :key="seg.key"
        class="h-full"
        :style="{ width: `${seg.width}%`, background: seg.color }"
        :title="seg.title"
      />
    </div>
    <div class="text-[11px] text-[var(--text-muted)] mt-1.5 leading-relaxed">
      <span v-for="(s, idx) in shares" :key="s.account" class="mr-2">
        <b :style="{ color: SHARE_COLORS[idx % SHARE_COLORS.length] }">■</b>
        {{ s.account }} <b class="text-[var(--text-primary)]">{{ s.suggested_requests }}</b> 次{{
          s.limit_requests ? `（限 ${s.limit_requests}）` : ''
        }}
      </span>
    </div>
  </div>
</template>
