<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Grid3x3, ListFilter, RefreshCw, Send, ShieldAlert, SkipForward } from 'lucide-vue-next'
import type { QuotaCell } from '@/api'
import { useAppStore } from '@/stores/app'
import { fmtEventTime, fmtTokens, localDateStr } from '@/utils/format'

const store = useAppStore()
const loading = ref(false)

async function refresh() {
  loading.value = true
  try {
    await store.fetchQuota()
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refresh()
})

const down = computed(() => store.quotaDown || store.quotaSnapshot?.status === 'down')
const enabled = computed(() => !down.value && store.quotaSnapshot?.quota_enabled !== false)
const mode = computed(() =>
  down.value ? '网关不可达' : enabled.value ? store.quotaSnapshot?.quota_mode || 'observe' : 'off',
)

const cells = computed(() => store.quotaSnapshot?.cells ?? [])
const events = computed(() => store.quotaEvents)

const todaySkips = computed(() => {
  const today = localDateStr()
  return events.value.filter(
    (e) => e.event_type === 'would_skip' && String(e.created_at || '').slice(0, 10) === today,
  ).length
})

const last429 = computed(() => events.value.find((e) => Number(e.http_status) === 429))
const totalReq = computed(() =>
  cells.value.reduce((s, c) => s + (Number(c.success_count) || 0), 0),
)

const models = computed(
  () =>
    store.quotaSnapshot?.models ?? [
      'glm-5.3-flash',
      'deepseek-v4.1-flash',
      'hy4free',
      'hy4flash-free',
    ],
)

const sortedAccounts = computed(() => {
  const list = store.quotaSnapshot?.accounts ?? []
  return list
    .slice()
    .sort(
      (a, b) =>
        (Number(a.priority) || 100) - (Number(b.priority) || 100) ||
        String(a.name).localeCompare(String(b.name), 'zh'),
    )
})

const cellMap = computed(() => {
  const m = new Map<string, QuotaCell>()
  for (const c of cells.value) {
    m.set(`${c.account_name}|${c.model_id}`, c)
  }
  return m
})

function levelClass(status?: string): string {
  const level = String(status || 'normal').replace(/[^a-z]/g, '')
  switch (level) {
    case 'disabled':
      return 'bg-slate-400/12 text-[var(--text-muted)] border-[var(--border-subtle)]'
    case 'exhausted':
      return 'bg-rose-400/15 text-[var(--red)] border-rose-400/25'
    case 'cooldown':
      return 'bg-orange-400/15 text-orange-400 border-orange-400/25'
    case 'threshold':
      return 'bg-amber-400/16 text-[var(--amber)] border-amber-400/25'
    case 'warning':
      return 'bg-yellow-300/12 text-yellow-300 border-yellow-300/22'
    case 'unknown':
      return 'bg-slate-400/8 text-[var(--text-muted)] border-[var(--border-subtle)]'
    default:
      return 'bg-emerald-400/15 text-[var(--green)] border-emerald-400/25'
  }
}
</script>

<template>
  <div class="flex flex-col gap-6">
    <div class="flex items-end justify-between gap-3 flex-wrap">
      <div>
        <h1 class="text-xl font-bold tracking-tight">模型额度监控</h1>
        <p class="text-xs text-[var(--text-secondary)] mt-1">
          事前 95% 阈值主动切号 · 全员达限直接停止返回 503（免费模型免查积分）
        </p>
      </div>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-card)] hover:bg-[var(--bg-card-hover)] text-xs font-medium cursor-pointer"
        :disabled="loading"
        @click="refresh"
      >
        <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />刷新
      </button>
    </div>

    <!-- 4 KPI -->
    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-[var(--text-secondary)]">引擎状态</span>
          <span
            class="mono text-[10px] px-2 py-0.5 rounded-full bg-amber-400/15 text-amber-400 border border-amber-400/30"
          >
            {{ mode === 'observe' ? '观察记账' : mode }}
          </span>
        </div>
        <div class="mono text-xl font-bold mt-2.5">{{ mode }}</div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">默认提前切号阈值：95%</div>
      </div>

      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-[var(--text-secondary)]">今日总请求</span>
          <span class="w-7 h-7 rounded-lg bg-indigo-500/10 text-indigo-400 flex items-center justify-center">
            <Send class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-2xl font-bold mt-2.5">{{ totalReq }}</div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">已落盘 model_quota_usage</div>
      </div>

      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-[var(--text-secondary)]">今日 would_skip</span>
          <span class="w-7 h-7 rounded-lg bg-amber-400/10 text-amber-400 flex items-center justify-center">
            <SkipForward class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-2xl font-bold mt-2.5 text-[var(--amber)]">{{ todaySkips }}</div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">命中 95% 阈值探针/切号</div>
      </div>

      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-[var(--text-secondary)]">最近一次 429</span>
          <span class="w-7 h-7 rounded-lg bg-emerald-400/10 text-emerald-400 flex items-center justify-center">
            <ShieldAlert class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-base font-bold mt-2.5">
          {{ fmtEventTime(last429?.created_at) }}
        </div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">
          {{ last429 ? '最近一条上游限流样本' : '今日暂无上游限流样本' }}
        </div>
      </div>
    </div>

    <!-- 账号 × 模型状态矩阵 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
      <div
        class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between gap-3 flex-wrap"
      >
        <div class="flex items-center gap-2 text-sm font-semibold">
          <Grid3x3 class="w-4 h-4 text-indigo-400" />
          <span>账号 × 模型状态矩阵</span>
          <span
            v-if="enabled"
            class="mono text-[11px] font-normal px-2 py-0.5 rounded-full bg-[var(--bg-elevated)] text-[var(--text-muted)]"
          >
            {{ sortedAccounts.length }} × {{ models.length }}
          </span>
        </div>
        <span class="text-[11px] text-[var(--text-muted)]">七级状态口径与第 2 批一致，本面板只读</span>
      </div>

      <div class="max-h-[480px] overflow-auto">
        <table class="w-full min-w-[780px] border-collapse text-xs">
          <thead class="sticky top-0 bg-[var(--bg-card)] z-10">
            <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
              <th class="px-4 py-3 text-left w-44">账号</th>
              <th v-for="m in models" :key="m" class="px-4 py-3 text-left mono">
                {{ m }}
                <span v-if="m.startsWith('hy4')" class="text-[var(--green)] ml-1">免费</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="down || !enabled">
              <td :colspan="models.length + 1" class="text-center py-8 text-[var(--text-muted)]">
                {{ down ? '网关不可达' : '额度引擎未启用' }}
              </td>
            </tr>
            <tr v-else-if="!sortedAccounts.length">
              <td :colspan="models.length + 1" class="text-center py-8 text-[var(--text-muted)]">
                账号池为空
              </td>
            </tr>
            <tr
              v-for="a in sortedAccounts"
              v-else
              :key="a.name"
              class="border-b border-slate-800/60 hover:bg-slate-400/3"
              :class="{ 'opacity-55': a.disabled }"
            >
              <td class="px-4 py-3 font-semibold">{{ a.name }}</td>
              <td
                v-for="m in models"
                :key="m"
                class="px-4 py-3 min-w-[170px]"
                :class="{
                  'opacity-45':
                    cellMap.get(`${a.name}|${m}`)?.status === 'unknown' &&
                    !(Number(cellMap.get(`${a.name}|${m}`)?.success_count) || 0),
                }"
              >
                <template v-if="cellMap.get(`${a.name}|${m}`)">
                  <span
                    class="inline-flex items-center text-[11px] px-2.5 py-0.5 rounded-full border whitespace-nowrap"
                    :class="levelClass(cellMap.get(`${a.name}|${m}`)?.status)"
                  >
                    {{ cellMap.get(`${a.name}|${m}`)?.status_label || '正常' }}
                    <template
                      v-if="
                        cellMap.get(`${a.name}|${m}`)?.status === 'cooldown' &&
                        cellMap.get(`${a.name}|${m}`)?.remaining_seconds
                      "
                    >
                      · 剩 {{ cellMap.get(`${a.name}|${m}`)?.remaining_seconds }}s
                    </template>
                  </span>
                  <div class="mt-1 text-[11px] text-[var(--text-muted)] tabular whitespace-nowrap">
                    {{ Number(cellMap.get(`${a.name}|${m}`)?.success_count) || 0 }} 次 ·
                    {{ fmtTokens(cellMap.get(`${a.name}|${m}`)?.tokens_used) }} tk
                  </div>
                  <div class="mt-0.5 text-[11px] text-[var(--text-muted)] tabular whitespace-nowrap">
                    <template v-if="cellMap.get(`${a.name}|${m}`)?.percent == null">
                      限额未知 · 重置 —
                    </template>
                    <template v-else>
                      {{ Number(cellMap.get(`${a.name}|${m}`)?.success_count) || 0 }}/{{
                        cellMap.get(`${a.name}|${m}`)?.safe_request_limit
                      }}
                      · {{ cellMap.get(`${a.name}|${m}`)?.percent }}% · 重置 —
                    </template>
                  </div>
                </template>
                <span v-else class="text-[var(--text-muted)]">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 最近事件 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
      <div
        class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between gap-3"
      >
        <div class="flex items-center gap-2 text-sm font-semibold">
          <ListFilter class="w-4 h-4 text-indigo-400" />
          <span>最近事件</span>
          <span class="text-[11px] font-normal px-2 py-0.5 rounded-full bg-[var(--bg-elevated)] text-[var(--text-muted)]">
            {{ events.length }} 条
          </span>
        </div>
        <span class="text-[11px] text-[var(--text-muted)]">model_quota_events 实时观测</span>
      </div>
      <div class="max-h-[340px] overflow-auto">
        <table class="w-full border-collapse text-xs">
          <thead class="sticky top-0 bg-[var(--bg-card)]">
            <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
              <th class="px-4 py-2.5 text-left">时间</th>
              <th class="px-4 py-2.5 text-left">账号</th>
              <th class="px-4 py-2.5 text-left">模型</th>
              <th class="px-4 py-2.5 text-left">事件</th>
              <th class="px-4 py-2.5 text-left">状态 / 错误码</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!events.length">
              <td colspan="5" class="px-4 py-6 text-center text-[var(--text-muted)]">暂无事件</td>
            </tr>
            <tr
              v-for="(ev, idx) in events"
              :key="idx"
              class="border-b border-slate-800/50"
              :class="{ 'bg-amber-400/6': ev.event_type === 'would_skip' }"
            >
              <td class="px-4 py-2.5 mono">{{ fmtEventTime(ev.created_at) }}</td>
              <td class="px-4 py-2.5">{{ ev.account_name || '—' }}</td>
              <td class="px-4 py-2.5 mono">{{ ev.model_id || '—' }}</td>
              <td class="px-4 py-2.5">
                {{ ev.event_type || '—' }}
                <span
                  v-if="ev.has_raw"
                  class="ml-1 text-[10px] px-1.5 py-0.5 rounded-full bg-[var(--bg-elevated)] text-[var(--text-muted)]"
                >
                  raw
                </span>
              </td>
              <td class="px-4 py-2.5 mono">
                {{ ev.http_status == null ? '—' : ev.http_status }} {{ ev.error_code || '' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
