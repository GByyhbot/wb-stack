<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { AlertTriangle, CalendarDays, RotateCw, Target } from 'lucide-vue-next'
import { useAppStore } from '@/stores/app'
import { fmtCredits } from '@/utils/format'

const store = useAppStore()
const loading = ref(false)

async function recalculate() {
  loading.value = true
  try {
    await store.fetchRecommendation()
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (!store.recommendation) recalculate()
})
</script>

<template>
  <div class="flex flex-col gap-6">
    <div class="flex items-end justify-between gap-3 flex-wrap">
      <div class="flex items-center gap-2.5 flex-wrap">
        <h1 class="text-xl font-bold tracking-tight">每日用量推荐</h1>
        <span
          class="text-[11px] px-2.5 py-0.5 rounded-full bg-indigo-500/15 text-indigo-300 border border-indigo-500/25"
        >
          目标：到期优先
        </span>
        <span
          class="mono text-[11px] px-2.5 py-0.5 rounded-full bg-[var(--bg-elevated)] text-[var(--text-secondary)] border border-[var(--border-subtle)]"
        >
          {{ store.recommendation ? `${store.recommendation.date} · 近 ${store.recommendation.window_days} 天数据` : '-' }}
        </span>
      </div>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-card)] hover:bg-[var(--bg-card-hover)] text-xs font-medium cursor-pointer"
        :disabled="loading"
        @click="recalculate"
      >
        <RotateCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />重新计算
      </button>
    </div>

    <!-- 风险提示 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-5">
      <div class="flex items-center gap-2 text-sm font-semibold mb-3.5">
        <AlertTriangle class="w-4 h-4 text-indigo-400" />
        <span>风险提示</span>
      </div>
      <div v-if="!store.recommendation" class="text-xs text-[var(--text-muted)]">加载中…</div>
      <div v-else-if="!store.recommendation.risks.length" class="text-xs text-[var(--text-muted)]">
        暂无风险提示 🎉
      </div>
      <div v-else class="flex flex-col gap-2">
        <div
          v-for="(r, idx) in store.recommendation.risks"
          :key="idx"
          class="flex items-start gap-2.5 text-xs leading-relaxed rounded-xl px-3 py-2.5 border"
          :class="
            r.type === 'no_candidate' || r.type === 'error'
              ? 'bg-rose-400/8 border-rose-400/30'
              : 'bg-amber-400/8 border-amber-400/25'
          "
        >
          <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5 text-amber-400" />
          <span>{{ r.message }}</span>
        </div>
      </div>
    </div>

    <!-- 每模型推荐 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
      <div class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between">
        <div class="flex items-center gap-2 text-sm font-semibold">
          <Target class="w-4 h-4 text-indigo-400" />
          <span>每模型推荐（主用 / 备选 / 今日分配）</span>
        </div>
        <span class="text-[11px] text-[var(--text-muted)]">按临期积分权重摊派</span>
      </div>
      <div class="max-h-[460px] overflow-auto">
        <table class="w-full border-collapse text-xs">
          <thead class="sticky top-0 bg-[var(--bg-card)]">
            <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
              <th class="px-4 py-3 text-left min-w-[130px]">模型</th>
              <th class="px-4 py-3 text-left">日均量</th>
              <th class="px-4 py-3 text-left min-w-[170px]">推荐主用</th>
              <th class="px-4 py-3 text-left">备选账号</th>
              <th class="px-4 py-3 text-left min-w-[300px]">今日建议分配（次）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!store.recommendation?.models.length">
              <td colspan="5" class="px-4 py-6 text-center text-[var(--text-muted)]">近 7 天暂无代理请求</td>
            </tr>
            <tr
              v-for="m in store.recommendation?.models"
              :key="m.model"
              class="border-b border-slate-800/50 align-top"
            >
              <td class="px-4 py-3 mono">{{ m.model }}</td>
              <td class="px-4 py-3 mono">{{ m.expected_daily_requests }} 次</td>
              <td class="px-4 py-3">
                <template v-if="m.primary">
                  <div class="font-bold text-[13px]">{{ m.primary.account }}</div>
                  <div
                    v-for="(why, i) in m.primary.reasons"
                    :key="i"
                    class="text-[11px] text-[var(--text-muted)] leading-snug"
                  >
                    · {{ why }}
                  </div>
                </template>
                <span v-else class="text-[var(--red)]">无可用账号</span>
              </td>
              <td class="px-4 py-3">
                <template v-if="m.backup">
                  <div>{{ m.backup.account }}</div>
                  <div
                    v-for="(why, i) in m.backup.reasons"
                    :key="i"
                    class="text-[11px] text-[var(--text-muted)] leading-snug"
                  >
                    · {{ why }}
                  </div>
                </template>
                <span v-else class="text-[var(--text-muted)]">—</span>
              </td>
              <td class="px-4 py-3">
                <template v-if="m.shares.length">
                  <ShareBar :shares="m.shares" />
                </template>
                <span v-else class="text-[var(--text-muted)]">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 每账号今日计划 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
      <div class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between">
        <div class="flex items-center gap-2 text-sm font-semibold">
          <CalendarDays class="w-4 h-4 text-indigo-400" />
          <span>每账号今日计划</span>
        </div>
        <span class="text-[11px] text-[var(--text-muted)]">
          共 {{ store.recommendation?.accounts.length ?? 0 }} 个账号
        </span>
      </div>
      <div class="max-h-[460px] overflow-auto">
        <table class="w-full border-collapse text-xs">
          <thead class="sticky top-0 bg-[var(--bg-card)]">
            <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
              <th class="px-4 py-3 text-left">账号</th>
              <th class="px-4 py-3 text-right">可用积分</th>
              <th class="px-4 py-3 text-left">到期</th>
              <th class="px-4 py-3 text-right">今日积分目标</th>
              <th class="px-4 py-3 text-right">建议承接</th>
              <th class="px-4 py-3 text-left">承接模型分配</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!store.recommendation?.accounts.length">
              <td colspan="6" class="px-4 py-6 text-center text-[var(--text-muted)]">暂无账号</td>
            </tr>
            <tr
              v-for="a in store.recommendation?.accounts"
              :key="a.account"
              class="border-b border-slate-800/50 align-top"
            >
              <td class="px-4 py-3">
                <b>{{ a.account }}</b>
                <div v-if="!a.candidate" class="text-[11px] text-[var(--amber)] mt-0.5">
                  未参与分配：{{ a.excluded_reason }}
                </div>
              </td>
              <td class="px-4 py-3 mono text-right">
                {{ a.available_credits == null ? '未知' : fmtCredits(a.available_credits) }}
              </td>
              <td class="px-4 py-3">
                <span
                  v-if="a.days_left != null"
                  class="text-[11px] px-2 py-0.5 rounded-full border whitespace-nowrap"
                  :class="
                    a.days_left <= 3
                      ? 'bg-rose-400/15 text-[var(--red)] border-rose-400/25'
                      : a.days_left <= 7
                        ? 'bg-amber-400/15 text-[var(--amber)] border-amber-400/25'
                        : 'bg-slate-400/10 text-[var(--text-secondary)] border-[var(--border-subtle)]'
                  "
                >
                  {{ a.expire_at ? `${String(a.expire_at).slice(5, 10)} · ` : '' }}剩 {{ a.days_left }} 天
                </span>
                <span v-else class="text-[var(--text-muted)]">—</span>
              </td>
              <td class="px-4 py-3 mono text-right">
                <template v-if="a.daily_credits_target == null">
                  <span class="text-[var(--text-muted)]">—</span>
                </template>
                <template v-else>{{ a.daily_credits_target }} 分/天</template>
              </td>
              <td class="px-4 py-3 mono text-right"><b>{{ a.suggested_requests }}</b> 次</td>
              <td class="px-4 py-3">
                <div v-if="a.models.length" class="flex flex-wrap gap-1">
                  <span
                    v-for="x in a.models"
                    :key="x.model"
                    class="text-[11px] px-2 py-0.5 rounded-full bg-[var(--bg-elevated)] border border-[var(--border-subtle)] whitespace-nowrap"
                  >
                    {{ x.model }} · <b>{{ x.suggested_requests }}</b>
                  </span>
                </div>
                <span v-else class="text-[var(--text-muted)]">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
