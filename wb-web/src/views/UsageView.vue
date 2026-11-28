<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { BarChart3, RefreshCw, Save, Table2, Zap } from 'lucide-vue-next'
import { usageApi, type UsageLimitItem } from '@/api'
import { useAppStore } from '@/stores/app'
import { fmtTokens } from '@/utils/format'

const store = useAppStore()
const saving = ref(false)
const limitMsg = ref('0 = 不限。限量仅作用量展示，不拦截请求。')

const usage = computed(() => store.usage)

// ---- 纯 CSS 7 日柱状图 ----
const chartDays = computed(() => {
  const days = usage.value?.per_day ?? []
  const map = new Map(days.map((x) => [x.date, x]))
  const labels: Array<{ date: string; requests: number; tin: number; tout: number }> = []
  for (let i = 6; i >= 0; i--) {
    const dt = new Date(Date.now() - i * 86400000)
    const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(
      dt.getDate(),
    ).padStart(2, '0')}`
    const v = map.get(key)
    labels.push({ date: key, requests: v?.requests || 0, tin: v?.tin || 0, tout: v?.tout || 0 })
  }
  return labels
})

const maxReq = computed(() => Math.max(1, ...chartDays.value.map((x) => x.requests)))
const totalReq = computed(() => chartDays.value.reduce((s, x) => s + x.requests, 0))

// ---- 限量草稿（响应式表单，仅非负整数） ----
const limitDrafts = reactive<Record<string, { requests: string; tokens: string }>>({})

watch(
  usage,
  (u) => {
    if (!u) return
    for (const r of u.by_account_model ?? []) {
      const key = `${r.account}|${r.model}`
      if (!(key in limitDrafts)) {
        limitDrafts[key] = {
          requests: String(r.limit_requests || 0),
          tokens: String(r.limit_tokens || 0),
        }
      }
    }
  },
  { immediate: true },
)

function clampNonNegInt(raw: string): string {
  const n = Math.max(0, Math.floor(Number(raw) || 0))
  return String(n)
}

function normalizeDraft(key: string, field: 'requests' | 'tokens') {
  limitDrafts[key][field] = clampNonNegInt(limitDrafts[key][field])
}

async function refreshUsage() {
  await store.fetchUsage()
}

async function saveLimits() {
  if (saving.value) return
  saving.value = true
  try {
    const limits: Record<string, UsageLimitItem> = {}
    for (const [key, d] of Object.entries(limitDrafts)) {
      limits[key] = {
        requests: Number(clampNonNegInt(d.requests)),
        tokens: Number(clampNonNegInt(d.tokens)),
      }
    }
    const res = await usageApi.saveLimits(limits)
    limitMsg.value = `已保存 ${Object.keys(res.limits || limits).length} 条限量（0 = 不限）`
    store.notify('限量已保存')
    await store.fetchUsage()
  } catch (e) {
    limitMsg.value = `保存失败: ${e instanceof Error ? e.message : e}`
    store.notify(limitMsg.value, false)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="flex flex-col gap-6">
    <div class="flex items-end justify-between gap-3 flex-wrap">
      <div>
        <h1 class="text-xl font-bold tracking-tight">用量统计</h1>
        <p class="text-xs text-[var(--text-secondary)] mt-1">
          统计范围：{{ usage ? `${usage.since} 起 · 近 7 天` : '近 7 天' }} · 请求与 Token 消耗分布
        </p>
      </div>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-card)] hover:bg-[var(--bg-card-hover)] text-xs font-medium cursor-pointer"
        @click="refreshUsage"
      >
        <RefreshCw class="w-3.5 h-3.5" />刷新用量
      </button>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <!-- 近 7 天趋势 -->
      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
        <div
          class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between gap-3"
        >
          <div class="flex items-center gap-2 text-sm font-semibold">
            <BarChart3 class="w-4 h-4 text-indigo-400" />
            <span>近 7 天用量趋势</span>
            <span
              class="mono text-[11px] px-2 py-0.5 rounded-full bg-[var(--bg-elevated)] text-[var(--text-muted)]"
            >
              {{ usage ? `${usage.since} 起` : '—' }}
            </span>
          </div>
          <span class="mono text-[11px] text-[var(--text-muted)]">
            总计 {{ totalReq.toLocaleString('en-US') }} 次
          </span>
        </div>
        <div class="p-5">
          <div v-if="!chartDays.length" class="h-36 flex items-center justify-center text-xs text-[var(--text-muted)]">
            近 7 天暂无代理请求
          </div>
          <div v-else class="flex items-end gap-2 h-40">
            <div v-for="d in chartDays" :key="d.date" class="flex-1 min-w-0 flex flex-col items-center gap-1.5">
              <div class="mono text-[10px]" :class="d.requests ? 'text-[var(--text-secondary)]' : 'text-[var(--text-muted)] opacity-60'">
                {{ d.requests }}
              </div>
              <div class="w-full h-28 flex items-end rounded-md bg-slate-900/70 overflow-hidden">
                <div
                  class="w-full rounded-md bg-gradient-to-t from-indigo-600 to-violet-400/80 transition-all"
                  :style="{
                    height: `${Math.max(3, (d.requests / maxReq) * 100)}%`,
                    opacity: d.requests ? 1 : 0.25,
                  }"
                  :title="`${d.date}：${d.requests} 次 / ${fmtTokens(d.tin + d.tout)} tk（入 ${fmtTokens(d.tin)} / 出 ${fmtTokens(d.tout)}）`"
                />
              </div>
              <div class="text-[10px] text-[var(--text-muted)] whitespace-nowrap">{{ d.date.slice(5) }}</div>
              <div class="mono text-[10px] text-[var(--text-muted)] whitespace-nowrap" :title="`${d.tin + d.tout} tokens`">
                {{ fmtTokens(d.tin + d.tout) }}
              </div>
            </div>
          </div>

          <!-- 模型分布 chips -->
          <div class="mt-4 flex flex-wrap gap-1.5">
            <template v-if="usage?.by_model?.length">
              <span
                v-for="m in usage.by_model"
                :key="m.model"
                class="text-[11px] px-2.5 py-1 rounded-full bg-[var(--bg-elevated)] border border-[var(--border-subtle)]"
              >
                {{ m.model }} · <b>{{ m.requests }}</b> 次 · {{ fmtTokens(m.tin + m.tout) }} tk
              </span>
            </template>
            <span v-else class="text-[11px] text-[var(--text-muted)]">暂无模型数据</span>
          </div>
        </div>
      </div>

      <!-- 最近请求 -->
      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
        <div
          class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between gap-3"
        >
          <div class="flex items-center gap-2 text-sm font-semibold">
            <Zap class="w-4 h-4 text-amber-400" />
            <span>最近请求</span>
            <span class="text-[11px] px-2 py-0.5 rounded-full bg-[var(--bg-elevated)] text-[var(--text-muted)]">
              {{ usage?.recent?.length ?? 0 }} 条
            </span>
          </div>
          <span class="text-[11px] text-[var(--text-muted)]">按时间倒序</span>
        </div>
        <div class="max-h-[360px] overflow-auto">
          <table class="w-full border-collapse text-xs">
            <thead class="sticky top-0 bg-[var(--bg-card)]">
              <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
                <th class="px-4 py-2.5 text-left">时间</th>
                <th class="px-4 py-2.5 text-left">账号</th>
                <th class="px-4 py-2.5 text-left">模型</th>
                <th class="px-4 py-2.5 text-center">状态</th>
                <th class="px-4 py-2.5 text-right">耗时</th>
                <th class="px-4 py-2.5 text-right">Tokens</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!usage?.recent?.length">
                <td colspan="6" class="px-4 py-6 text-center text-[var(--text-muted)]">暂无请求</td>
              </tr>
              <tr
                v-for="(r, idx) in usage?.recent ?? []"
                :key="idx"
                class="border-b border-slate-800/50"
              >
                <td class="px-4 py-2.5 mono text-[var(--text-muted)]">{{ (r.ts || '').slice(5, 16) }}</td>
                <td class="px-4 py-2.5">{{ r.account || '-' }}</td>
                <td class="px-4 py-2.5 mono">{{ r.model || '-' }}</td>
                <td
                  class="px-4 py-2.5 mono text-center"
                  :class="r.status === 200 ? 'text-[var(--green)]' : 'text-[var(--red)]'"
                >
                  {{ r.status }}
                </td>
                <td class="px-4 py-2.5 mono text-right text-[var(--text-muted)]">
                  {{ ((r.duration_ms || 0) / 1000).toFixed(1) }}s
                </td>
                <td class="px-4 py-2.5 mono text-right text-[var(--text-muted)]">
                  {{ fmtTokens((r.prompt_tokens || 0) + (r.completion_tokens || 0)) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 账号 × 模型用量与限量 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
      <div
        class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between gap-3 flex-wrap"
      >
        <div class="flex items-center gap-2 text-sm font-semibold">
          <Table2 class="w-4 h-4 text-indigo-400" />
          <span>账号 × 模型用量（每日限量配置）</span>
        </div>
        <span class="mono text-[11px] text-[var(--text-muted)]">{{ usage ? `${usage.since} 起` : '' }}</span>
      </div>
      <div class="overflow-auto">
        <table class="w-full min-w-[860px] border-collapse text-xs">
          <thead class="sticky top-0 bg-[var(--bg-card)]">
            <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
              <th class="px-4 py-3 text-left">账号</th>
              <th class="px-4 py-3 text-left">模型</th>
              <th class="px-4 py-3 text-right">次数</th>
              <th class="px-4 py-3 text-left">Tokens（入 + 出）</th>
              <th class="px-4 py-3 text-center">限流(429)</th>
              <th class="px-4 py-3 text-left">使用率</th>
              <th class="px-4 py-3 text-right">每日限量（次 / tk）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!usage?.by_account_model?.length">
              <td colspan="7" class="px-4 py-6 text-center text-[var(--text-muted)]">暂无用量记录</td>
            </tr>
            <tr
              v-for="r in usage?.by_account_model ?? []"
              :key="`${r.account}|${r.model}`"
              class="border-b border-slate-800/50"
            >
              <td class="px-4 py-3 font-medium">{{ r.account }}</td>
              <td class="px-4 py-3 mono">{{ r.model }}</td>
              <td class="px-4 py-3 mono text-right">{{ r.requests }}</td>
              <td class="px-4 py-3 mono text-[var(--text-muted)]">
                {{ fmtTokens(r.tin) }} + {{ fmtTokens(r.tout) }}
              </td>
              <td
                class="px-4 py-3 mono text-center"
                :class="r.limited ? 'text-[var(--red)]' : 'text-[var(--text-muted)]'"
              >
                {{ r.limited }}
              </td>
              <td class="px-4 py-3">
                <div class="flex items-center gap-1.5">
                  <span class="text-[var(--text-muted)]">次</span>
                  <span class="w-16 h-1.5 rounded-full bg-slate-900/80 overflow-hidden inline-block">
                    <span
                      v-if="r.req_pct != null"
                      class="block h-full rounded-full"
                      :class="r.req_pct >= 85 ? 'bg-rose-400' : 'bg-indigo-400'"
                      :style="{ width: `${Math.min(100, r.req_pct)}%` }"
                    />
                  </span>
                  <span class="text-[var(--text-muted)]">{{ r.req_pct == null ? '不限' : `${r.req_pct}%` }}</span>
                </div>
                <div v-if="r.tok_pct != null" class="flex items-center gap-1.5 mt-1">
                  <span class="text-[var(--text-muted)]">Tk</span>
                  <span class="w-16 h-1.5 rounded-full bg-slate-900/80 overflow-hidden inline-block">
                    <span
                      class="block h-full rounded-full"
                      :class="r.tok_pct >= 85 ? 'bg-rose-400' : 'bg-indigo-400'"
                      :style="{ width: `${Math.min(100, r.tok_pct)}%` }"
                    />
                  </span>
                  <span class="text-[var(--text-muted)]">{{ r.tok_pct }}%</span>
                </div>
              </td>
              <td class="px-4 py-3">
                <div class="flex items-center justify-end gap-1.5">
                  <input
                    v-model="limitDrafts[`${r.account}|${r.model}`].requests"
                    type="number"
                    min="0"
                    step="1"
                    class="w-16 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 mono text-xs text-[var(--text-primary)] outline-none focus:border-indigo-500"
                    @blur="normalizeDraft(`${r.account}|${r.model}`, 'requests')"
                  />
                  <span class="text-[var(--text-muted)]">/</span>
                  <input
                    v-model="limitDrafts[`${r.account}|${r.model}`].tokens"
                    type="number"
                    min="0"
                    step="1"
                    class="w-20 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-2 py-1 mono text-xs text-[var(--text-primary)] outline-none focus:border-indigo-500"
                    @blur="normalizeDraft(`${r.account}|${r.model}`, 'tokens')"
                  />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div
        class="px-5 py-3.5 border-t border-[var(--border-subtle)] flex items-center gap-3 flex-wrap"
      >
        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium cursor-pointer disabled:opacity-50"
          :disabled="saving"
          @click="saveLimits"
        >
          <Save class="w-3.5 h-3.5" />{{ saving ? '保存中…' : '保存限量' }}
        </button>
        <span class="text-[11px] text-[var(--text-muted)]">{{ limitMsg }}</span>
      </div>
    </div>
  </div>
</template>
