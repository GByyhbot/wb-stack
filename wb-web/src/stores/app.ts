import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  accountsApi,
  quotaApi,
  systemApi,
  usageApi,
  type Account,
  type ActiveAccountResponse,
  type CheckinLogEntry,
  type DiagnosticsResponse,
  type QuotaEvent,
  type QuotaSnapshot,
  type RecommendationResponse,
  type UsageResponse,
} from '@/api'
import { localDateStr } from '@/utils/format'

export interface ToastItem {
  id: number
  message: string
  ok: boolean
}

export const useAppStore = defineStore('app', () => {
  // ---- Toast ----
  const toasts = ref<ToastItem[]>([])
  let toastSeq = 0
  function notify(message: string, ok = true) {
    const id = ++toastSeq
    toasts.value.push({ id, message, ok })
    window.setTimeout(() => {
      toasts.value = toasts.value.filter((t) => t.id !== id)
    }, 2800)
  }

  // ---- 核心状态 ----
  const accounts = ref<Account[]>([])
  const activeInfo = ref<ActiveAccountResponse | null>(null)
  const activeError = ref<string | null>(null)
  const pinnedName = ref<string | null>(null)
  const usage = ref<UsageResponse | null>(null)
  const recommendation = ref<RecommendationResponse | null>(null)
  const logs = ref<CheckinLogEntry[]>([])
  const diagnostics = ref<DiagnosticsResponse | null>(null)

  // ---- 额度监控（只读） ----
  const quotaSnapshot = ref<QuotaSnapshot | null>(null)
  const quotaEvents = ref<QuotaEvent[]>([])
  const quotaDown = ref(false)

  // ---- 衍生计算 ----
  const growthToday = computed(() => {
    const today = localDateStr()
    let sum = 0
    let seen = false
    for (const l of logs.value) {
      if ((l.created_at || '').startsWith(today) && (l.message || '').startsWith('成长中心：')) {
        seen = true
        sum += parseFloat(String(l.reward ?? 0)) || 0
      }
    }
    return { seen, sum }
  })

  const quotaModeBadge = computed<'OBS' | 'ENF' | 'OFF' | 'DOWN'>(() => {
    if (quotaDown.value || quotaSnapshot.value?.status === 'down') return 'DOWN'
    if (!quotaSnapshot.value) return 'OBS'
    if (quotaSnapshot.value.quota_enabled === false) return 'OFF'
    return quotaSnapshot.value.quota_mode === 'enforce' ? 'ENF' : 'OBS'
  })

  const riskCount = computed(() => recommendation.value?.risks.length ?? 0)

  // ---- 数据拉取方法 ----
  async function fetchAccounts() {
    const res = await accountsApi.list()
    accounts.value = res.accounts
  }

  async function fetchActive() {
    try {
      activeInfo.value = await accountsApi.getActive()
      activeError.value = null
    } catch (e) {
      activeInfo.value = null
      activeError.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function fetchPin() {
    try {
      pinnedName.value = (await accountsApi.getPin()).name
    } catch {
      pinnedName.value = null
    }
  }

  async function fetchUsage(days = 7) {
    usage.value = await usageApi.getSummary(days)
  }

  async function fetchRecommendation() {
    try {
      recommendation.value = await usageApi.getRecommendation()
    } catch {
      // 不阻塞主视图
    }
  }

  async function fetchLogs(limit = 100) {
    try {
      const res = await systemApi.getLogs(limit)
      logs.value = res.logs
    } catch {
      // 不阻塞主视图
    }
  }

  async function fetchDiagnostics() {
    try {
      diagnostics.value = await systemApi.getDiagnostics()
    } catch {
      diagnostics.value = null
    }
  }

  async function fetchQuota() {
    try {
      const [snap, evs] = await Promise.all([quotaApi.getSnapshot(), quotaApi.getEvents(50)])
      quotaSnapshot.value = snap
      quotaEvents.value = Array.isArray(evs) ? evs : []
      quotaDown.value = snap.status === 'down'
    } catch {
      quotaDown.value = true
      quotaSnapshot.value = { status: 'down', cells: [], quota_enabled: false }
      quotaEvents.value = []
    }
  }

  async function refreshCore() {
    await Promise.allSettled([
      fetchAccounts(),
      fetchActive(),
      fetchPin(),
      fetchUsage(),
      fetchRecommendation(),
      fetchLogs(100),
      fetchDiagnostics(),
      fetchQuota(),
    ])
  }

  return {
    toasts,
    notify,
    accounts,
    activeInfo,
    activeError,
    pinnedName,
    usage,
    recommendation,
    logs,
    diagnostics,
    quotaSnapshot,
    quotaEvents,
    quotaDown,
    growthToday,
    quotaModeBadge,
    riskCount,
    fetchAccounts,
    fetchActive,
    fetchPin,
    fetchUsage,
    fetchRecommendation,
    fetchLogs,
    fetchDiagnostics,
    fetchQuota,
    refreshCore,
  }
})
