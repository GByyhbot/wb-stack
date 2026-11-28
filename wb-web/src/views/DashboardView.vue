<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CalendarCheck2,
  CheckCircle2,
  ChevronRight,
  Coins,
  Info,
  Layers,
  Pin,
  RefreshCw,
  Sparkles,
  Sprout,
  Users,
} from 'lucide-vue-next'
import { accountsApi, type Account } from '@/api'
import { useAppStore } from '@/stores/app'
import { availOf, fmtCredits, fmtTokens, localDateStr } from '@/utils/format'
import AccountCard from '@/components/AccountCard.vue'
import ConfirmModal from '@/components/ConfirmModal.vue'
import NoteModal from '@/components/NoteModal.vue'

const router = useRouter()
const store = useAppStore()

const batchBusy = ref<'checkin' | 'quota' | null>(null)
const cardBusyKey = ref<string | null>(null)

// 备注弹窗状态
const noteTarget = ref<Account | null>(null)
const noteSaving = ref(false)

// 删除确认弹窗状态
const deleteTarget = ref<Account | null>(null)
const deleting = ref(false)

const stats = computed(() => {
  const accs = store.accounts
  const today = localDateStr()
  let totalCredits = 0
  let checkedIn = 0
  let activeCount = 0
  for (const a of accs) {
    totalCredits += availOf(a) || 0
    if (a.last_checkin_at?.slice(0, 10) === today) checkedIn++
    if (a.status === 'active') activeCount++
  }
  const days = store.usage?.per_day ?? []
  const totalReq = days.reduce((s, x) => s + x.requests, 0)
  const totalTin = days.reduce((s, x) => s + x.tin, 0)
  const totalTout = days.reduce((s, x) => s + x.tout, 0)
  return {
    total: accs.length,
    activeCount,
    totalCredits: Math.round(totalCredits * 100) / 100,
    checkedIn,
    totalReq,
    totalTin,
    totalTout,
  }
})

async function handleCheckinAll() {
  if (batchBusy.value) return
  batchBusy.value = 'checkin'
  try {
    const d = await accountsApi.checkinAll()
    const okCount = d.results.filter((r) => r.ok).length
    const gSum = d.results.reduce((s, r) => s + (r.growth?.credits_gained || 0), 0)
    store.notify(
      `签到完成: ${okCount}/${d.results.length} 成功${gSum ? `，成长中心 +${gSum} 积分` : ''}`,
      okCount === d.results.length,
    )
    await Promise.allSettled([store.fetchAccounts(), store.fetchActive(), store.fetchLogs(100)])
  } catch (e) {
    store.notify(`全部签到失败: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    batchBusy.value = null
  }
}

async function handleRefreshAllQuota() {
  if (batchBusy.value) return
  batchBusy.value = 'quota'
  try {
    for (const a of store.accounts) {
      if (a.id == null) continue
      try {
        await accountsApi.refreshQuota(a.id)
      } catch {
        // 单个失败不中断其余刷新
      }
    }
    store.notify('可用积分刷新完成')
    await Promise.allSettled([store.fetchAccounts(), store.fetchActive()])
  } finally {
    batchBusy.value = null
  }
}

async function handlePin(name: string) {
  cardBusyKey.value = `${name}:pin`
  try {
    const target = store.pinnedName === name ? null : name
    const d = await accountsApi.setPin(target)
    store.pinnedName = d.pinned
    store.notify(
      d.pinned ? `已切换：代理将优先使用「${d.pinned}」` : '已取消置顶，恢复自动选择',
    )
    await Promise.allSettled([store.fetchAccounts(), store.fetchActive()])
  } catch (e) {
    store.notify(`切换失败: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    cardBusyKey.value = null
  }
}

async function handleCheckinOne(id: number) {
  cardBusyKey.value = `${id}:checkin`
  try {
    const d = await accountsApi.checkinOne(id)
    store.notify(d.ok ? `签到成功 ${d.reward || ''}` : `签到失败: ${d.message}`, d.ok)
    await Promise.allSettled([store.fetchAccounts(), store.fetchLogs(100)])
  } catch (e) {
    store.notify(`签到异常: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    cardBusyKey.value = null
  }
}

async function handleRefreshQuota(id: number) {
  cardBusyKey.value = `${id}:quota`
  try {
    const d = await accountsApi.refreshQuota(id)
    store.notify(
      d.ok
        ? `可用积分: ${d.quota_total}${
            d.used_total != null ? `（已用 ${d.used_total} / 共 ${d.granted_total}）` : ''
          }`
        : `刷新失败: ${d.message}`,
      d.ok,
    )
    await Promise.allSettled([store.fetchAccounts(), store.fetchActive()])
  } catch (e) {
    store.notify(`刷新异常: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    cardBusyKey.value = null
  }
}

async function handleValidate(id: number) {
  cardBusyKey.value = `${id}:validate`
  try {
    const d = await accountsApi.validateToken(id)
    store.notify(d.ok ? 'Token 有效' : `Token 无效: ${d.message}`, d.ok)
  } catch (e) {
    store.notify(`验证异常: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    cardBusyKey.value = null
  }
}

async function handleToggleStatus(acc: Account) {
  if (acc.id == null) return
  cardBusyKey.value = `${acc.id}:toggle`
  try {
    await accountsApi.update(acc.id, { status: acc.status === 'active' ? 'disabled' : 'active' })
    store.notify('状态已更新')
    await Promise.allSettled([store.fetchAccounts(), store.fetchActive()])
  } catch (e) {
    store.notify(`更新失败: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    cardBusyKey.value = null
  }
}

async function handleSaveNote(note: string) {
  if (!noteTarget.value || noteTarget.value.id == null) return
  noteSaving.value = true
  try {
    await accountsApi.update(noteTarget.value.id, { note })
    store.notify(note ? '备注已保存' : '备注已清除')
    noteTarget.value = null
    await store.fetchAccounts()
  } catch (e) {
    store.notify(`保存失败: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    noteSaving.value = false
  }
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    const acc = deleteTarget.value
    if (acc.id == null) {
      await accountsApi.removeGatewayOnly(acc.name)
      store.notify('已从代理池删除')
    } else {
      await accountsApi.remove(acc.id)
      store.notify('已删除')
    }
    deleteTarget.value = null
    await Promise.allSettled([store.fetchAccounts(), store.fetchActive(), store.fetchLogs(100)])
  } catch (e) {
    store.notify(`删除失败: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <div class="flex flex-col gap-6">
    <!-- 页头 -->
    <div class="flex items-end justify-between gap-3 flex-wrap">
      <div>
        <h1 class="text-xl font-bold tracking-tight">运行总览</h1>
        <p class="text-xs text-[var(--text-secondary)] mt-1">
          实时同步 WorkBuddy 多账号积分、签到状态与网关轮换调度
        </p>
      </div>
      <div class="flex items-center gap-2.5 flex-wrap">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium shadow-md shadow-indigo-600/25 cursor-pointer disabled:opacity-50"
          :disabled="batchBusy !== null"
          @click="handleCheckinAll"
        >
          <CalendarCheck2 class="w-3.5 h-3.5" />
          {{ batchBusy === 'checkin' ? '签到中…' : '全部签到' }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-card)] hover:bg-[var(--bg-card-hover)] hover:border-indigo-500/40 text-xs font-medium cursor-pointer disabled:opacity-50"
          :disabled="batchBusy !== null"
          @click="handleRefreshAllQuota"
        >
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': batchBusy === 'quota' }" />
          {{ batchBusy === 'quota' ? '刷新中…' : '刷新全部余额' }}
        </button>
      </div>
    </div>

    <!-- 5 张 KPI 卡片 -->
    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-4">
      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-[var(--text-secondary)]">账号总数</span>
          <span class="w-7 h-7 rounded-lg bg-indigo-500/10 text-indigo-400 flex items-center justify-center">
            <Users class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-2xl font-bold mt-3">{{ stats.total }}</div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">{{ stats.activeCount }} 个启用中</div>
      </div>

      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-[var(--text-secondary)]">总可用积分</span>
          <span class="w-7 h-7 rounded-lg bg-amber-400/10 text-amber-400 flex items-center justify-center">
            <Coins class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-2xl font-bold mt-3">{{ fmtCredits(stats.totalCredits) }}</div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">所有账号实时余额之和</div>
      </div>

      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-[var(--text-secondary)]">今日签到</span>
          <span class="w-7 h-7 rounded-lg bg-emerald-400/10 text-emerald-400 flex items-center justify-center">
            <CheckCircle2 class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-2xl font-bold mt-3">
          {{ stats.checkedIn }}<span class="text-sm font-medium text-[var(--text-muted)]">/{{ stats.total }}</span>
        </div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">每天自动签到 + 随机抖动</div>
      </div>

      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-[var(--text-secondary)]">今日成长中心</span>
          <span class="w-7 h-7 rounded-lg bg-teal-400/10 text-teal-400 flex items-center justify-center">
            <Sprout class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-2xl font-bold mt-3 text-[var(--green)]">
          {{ store.growthToday.seen ? `+${store.growthToday.sum}` : '0'
          }}<span class="text-sm font-medium text-[var(--text-muted)] ml-0.5">积分</span>
        </div>
        <div class="text-[11px] text-[var(--text-muted)] mt-1">旅行礼物 · 任务 · 连登兑换 · 盲盒</div>
      </div>

      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-[var(--text-secondary)]">近 7 天代理请求</span>
          <span class="w-7 h-7 rounded-lg bg-sky-400/10 text-sky-400 flex items-center justify-center">
            <Activity class="w-3.5 h-3.5" />
          </span>
        </div>
        <div class="mono text-2xl font-bold mt-3">{{ stats.totalReq.toLocaleString('en-US') }}</div>
        <div class="tabular text-[11px] text-[var(--text-muted)] mt-1">
          {{ fmtTokens(stats.totalTin) }} 入 / {{ fmtTokens(stats.totalTout) }} 出
        </div>
      </div>
    </div>

    <!-- 当前活跃账号横幅 -->
    <div
      class="rounded-2xl px-5 py-4 border border-indigo-500/30 bg-gradient-to-r from-indigo-500/12 via-violet-500/8 to-transparent flex items-center justify-between gap-4 flex-wrap"
    >
      <template v-if="store.activeInfo">
        <div class="flex items-center gap-3.5 flex-wrap">
          <span class="w-2.5 h-2.5 rounded-full bg-[var(--green)] shadow-[0_0_10px_rgba(52,211,153,0.8)]" />
          <span class="text-xs text-[var(--text-secondary)]">当前业务账号</span>
          <span class="mono text-base font-bold">{{ store.activeInfo.active.name }}</span>
          <span
            v-if="store.activeInfo.active.pinned"
            class="inline-flex items-center gap-1 text-[11px] px-2.5 py-0.5 rounded-full bg-amber-400/15 text-amber-400 border border-amber-400/30"
          >
            <Pin class="w-3 h-3" />置顶
          </span>
        </div>
        <div class="flex items-center gap-5">
          <div class="text-right">
            <div class="text-[11px] text-[var(--text-muted)]">优先级 / 剩余占比</div>
            <div class="mono font-semibold mt-0.5">
              优先级 {{ store.activeInfo.active.priority }}
              <template v-if="store.activeInfo.active.quota_percent != null">
                · 额度 {{ Math.round(store.activeInfo.active.quota_percent) }}%
              </template>
            </div>
          </div>
          <div class="w-px h-7 bg-slate-400/20" />
          <div class="text-right">
            <div class="text-[11px] text-[var(--text-muted)]">可用积分</div>
            <div class="mono font-semibold mt-0.5">
              {{ fmtCredits(availOf(store.activeInfo.active)) }}
            </div>
          </div>
        </div>
        <div v-if="store.activeInfo.warning" class="basis-full text-xs text-[var(--amber)]">
          ⚠️ {{ store.activeInfo.warning }}
        </div>
      </template>
      <div v-else-if="store.activeError" class="text-xs text-[var(--amber)]">
        ⚠️ {{ store.activeError }}
      </div>
      <div v-else class="text-xs text-[var(--text-muted)]">加载中…</div>
    </div>

    <!-- 今日用量推荐摘要 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
      <div
        class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between gap-3 flex-wrap"
      >
        <div class="flex items-center gap-2 text-sm font-semibold">
          <Sparkles class="w-4 h-4 text-indigo-400" />
          <span>今日用量推荐</span>
          <span
            class="text-[11px] px-2.5 py-0.5 rounded-full bg-indigo-500/15 text-indigo-300 border border-indigo-500/25 font-normal"
          >
            到期优先
          </span>
        </div>
        <button
          type="button"
          class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-elevated)] hover:border-indigo-500/40 text-[11px] cursor-pointer"
          @click="router.push('/recommend')"
        >
          查看完整推荐 <ArrowRight class="w-3.5 h-3.5" />
        </button>
      </div>
      <div class="p-5">
        <div v-if="!store.recommendation" class="text-xs text-[var(--text-muted)]">加载中…</div>
        <div v-else-if="!store.recommendation.models.length" class="text-xs text-[var(--text-muted)]">
          近 7 天暂无代理请求，先有真实用量才能给出推荐。
        </div>
        <div v-else class="flex flex-col gap-1">
          <div
            v-for="m in store.recommendation.models.slice(0, 3)"
            :key="m.model"
            class="flex items-baseline gap-2 flex-wrap text-xs py-1"
          >
            <span class="mono text-indigo-400">{{ m.model }}</span>
            <span class="text-[var(--text-muted)]">→ 主用</span>
            <b v-if="m.primary">{{ m.primary.account }}</b>
            <span v-else class="text-[var(--red)]">无可用账号</span>
            <span v-if="m.primary?.reasons.length" class="text-[var(--text-muted)]">
              （{{ m.primary.reasons[0] }}）
            </span>
            <span class="text-[var(--text-muted)]">· 日均 {{ m.expected_daily_requests }} 次</span>
          </div>
        </div>

        <button
          v-if="store.recommendation?.risks.length"
          type="button"
          class="mt-3.5 w-full flex items-center justify-between gap-3 px-3.5 py-3 rounded-xl bg-amber-400/10 hover:bg-amber-400/16 border border-amber-400/25 text-amber-400 text-xs text-left cursor-pointer transition-colors"
          @click="router.push('/recommend')"
        >
          <span class="flex items-center gap-2.5">
            <span class="w-8 h-8 rounded-lg bg-amber-400/20 flex items-center justify-center shrink-0">
              <AlertTriangle class="w-4 h-4" />
            </span>
            <span>
              <strong>{{ store.recommendation.risks.length }} 条风险提示</strong>：{{
                store.recommendation.risks[0].message
              }}
            </span>
          </span>
          <ChevronRight class="w-4 h-4 shrink-0" />
        </button>
      </div>
    </div>

    <!-- 账号卡片网格 -->
    <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
      <div class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center gap-2 text-sm font-semibold">
        <Layers class="w-4 h-4 text-indigo-400" />
        <span>账号管理</span>
        <span class="text-[11px] font-normal text-[var(--text-muted)]">共 {{ store.accounts.length }} 个账号</span>
      </div>
      <div class="p-5">
        <div
          class="mb-4 flex items-start gap-2.5 px-3.5 py-2.5 rounded-xl bg-slate-400/6 border border-[var(--border-subtle)] text-xs leading-relaxed text-[var(--text-secondary)]"
        >
          <Info class="w-4 h-4 text-indigo-400 shrink-0 mt-0.5" />
          <span>
            <b class="text-[var(--text-primary)]">双闸门机制：</b>「积分」是账号维度的全局资源，是使用任何模型的前提——积分耗尽则所有模型均不可用；
            「额度」是每个模型每日独立分配的用量，某模型当日额度用尽仅该模型不可用。两道闸门全部通过才能调用。本面板「可用积分」为积分层真实值。
          </span>
        </div>

        <div v-if="!store.accounts.length" class="text-xs text-[var(--text-muted)]">
          暂无账号，去「账号与设置」添加
        </div>
        <div v-else class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          <AccountCard
            v-for="(acc, idx) in store.accounts"
            :key="acc.id ?? acc.name"
            :account="acc"
            :index="idx"
            :is-pinned="store.pinnedName === acc.name"
            :busy-key="cardBusyKey"
            @edit-note="noteTarget = $event"
            @pin="handlePin"
            @checkin="handleCheckinOne"
            @refresh-quota="handleRefreshQuota"
            @validate="handleValidate"
            @toggle-status="handleToggleStatus"
            @remove="deleteTarget = $event"
          />
        </div>
      </div>
    </div>

    <!-- 自定义弹窗：替换原生 prompt / confirm -->
    <NoteModal
      :open="noteTarget !== null"
      :account-name="noteTarget?.name || ''"
      :initial-note="noteTarget?.note || ''"
      :loading="noteSaving"
      @save="handleSaveNote"
      @cancel="noteTarget = null"
    />

    <ConfirmModal
      :open="deleteTarget !== null"
      title="确认删除账号"
      :message="
        deleteTarget?.id == null
          ? `确定从代理池删除「${deleteTarget?.name}」？该账号仅存在于网关，将从 accounts.json 移除。`
          : `确定删除账号「${deleteTarget?.name}」？其签到日志也会一并删除。`
      "
      confirm-text="确认删除"
      danger
      :loading="deleting"
      @confirm="confirmDelete"
      @cancel="deleteTarget = null"
    />
  </div>
</template>
