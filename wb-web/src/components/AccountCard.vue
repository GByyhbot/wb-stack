<script setup lang="ts">
import { computed } from 'vue'
import { Clock, Flame, Hourglass, Pin, Star } from 'lucide-vue-next'
import type { Account } from '@/api'
import { AVATAR_COLORS, availOf, fmtCredits } from '@/utils/format'

const props = defineProps<{
  account: Account
  index: number
  isPinned: boolean
  busyKey: string | null
}>()

const emit = defineEmits<{
  editNote: [account: Account]
  pin: [name: string]
  checkin: [id: number]
  refreshQuota: [id: number]
  validate: [id: number]
  toggleStatus: [account: Account]
  remove: [account: Account]
}>()

const avail = computed(() => availOf(props.account))
const pct = computed(() => props.account.quota_percent)
const barClass = computed(() => {
  if (pct.value == null) return 'from-indigo-500 to-violet-400'
  if (pct.value <= 10) return 'from-red-500 to-rose-400'
  if (pct.value <= 30) return 'from-amber-500 to-amber-300'
  return 'from-indigo-500 to-violet-400'
})

const avatarBg = computed(() => AVATAR_COLORS[props.index % AVATAR_COLORS.length])
const avatarChar = computed(
  () => (props.account.name || '?').replace(/[^0-9A-Za-z\u4e00-\u9fa5]/g, '').slice(-1) || '?',
)

const expireInfo = computed(() => {
  const exp = props.account.quota_expire_at
  if (!exp) return null
  const days = Math.floor((new Date(exp).getTime() - Date.now()) / 86400000)
  const label = `${exp.slice(5, 10)} 到期`
  if (days < 0) return { text: '已过期', tone: 'text-[var(--red)]', expired: true }
  if (days <= 3) return { text: `${label}（${days}天）`, tone: 'text-[var(--amber)]', expired: false }
  return { text: label, tone: '', expired: false }
})

const statusMeta = computed(() => {
  if (props.account.gateway_only) {
    return {
      text: '仅代理',
      cls: 'bg-violet-400/15 text-[var(--violet)] border-violet-400/25',
    }
  }
  if (props.account.status === 'active') {
    return { text: '正常', cls: 'bg-emerald-400/15 text-[var(--green)] border-emerald-400/25' }
  }
  if (props.account.status === 'error') {
    return { text: '异常', cls: 'bg-rose-400/15 text-[var(--red)] border-rose-400/25' }
  }
  return {
    text: '停用',
    cls: 'bg-slate-400/12 text-[var(--text-muted)] border-[var(--border-subtle)]',
  }
})

const isBusy = (action: string) =>
  props.busyKey === `${props.account.id ?? props.account.name}:${action}`
</script>

<template>
  <div
    class="rounded-2xl border bg-[var(--bg-elevated)] p-4 flex flex-col gap-3.5 transition-colors hover:border-indigo-500/40"
    :class="[
      account.priority === 1
        ? 'border-indigo-500/50 shadow-[inset_0_0_0_1px_rgba(99,102,241,0.2)]'
        : 'border-[var(--border-subtle)]',
      account.gateway_only ? 'border-dashed opacity-92' : '',
    ]"
  >
    <!-- 顶部身份与状态 -->
    <div class="flex items-start justify-between gap-2">
      <div class="flex items-center gap-2.5 min-w-0">
        <div
          class="w-9 h-9 rounded-full shrink-0 text-white font-bold text-xs flex items-center justify-center"
          :style="{ background: avatarBg }"
        >
          {{ avatarChar }}
        </div>
        <div class="min-w-0">
          <div class="text-sm font-semibold flex items-center gap-1.5 truncate">
            <Star v-if="account.priority === 1" class="w-3.5 h-3.5 text-amber-400 shrink-0" />
            <span class="truncate">{{ account.name }}</span>
          </div>
          <div class="mono text-[11px] text-[var(--text-muted)] truncate max-w-[180px]">
            {{ account.user_id || (account.gateway_only ? 'gateway pool' : '-') }}
          </div>
        </div>
      </div>
      <span class="text-[11px] px-2.5 py-0.5 rounded-full border whitespace-nowrap" :class="statusMeta.cls">
        {{ statusMeta.text }}
      </span>
    </div>

    <!-- 备注 -->
    <div
      v-if="account.gateway_only"
      class="text-[11px] text-[var(--text-secondary)] bg-[var(--bg-card)] border border-dashed border-[var(--border-subtle)] rounded-xl px-2.5 py-1.5"
    >
      🔗 仅代理池账号 · 面板未纳管签到
    </div>
    <button
      v-else
      type="button"
      class="text-left text-[11px] text-[var(--text-secondary)] bg-[var(--bg-card)] border border-dashed border-[var(--border-subtle)] rounded-xl px-2.5 py-1.5 hover:border-indigo-500/40 hover:text-[var(--text-primary)] cursor-pointer transition-colors"
      @click="emit('editNote', account)"
    >
      {{ account.note ? `📝 ${account.note}` : '📝 添加备注…' }}
    </button>

    <!-- 积分进度 -->
    <div>
      <div class="flex items-baseline justify-between gap-2">
        <div>
          <span class="mono text-xl font-bold">
            {{ avail != null ? fmtCredits(Math.round(avail * 100) / 100) : '—' }}
          </span>
          <span class="text-[11px] text-[var(--text-muted)] ml-1.5">可用积分</span>
        </div>
        <span class="mono text-xs text-[var(--text-secondary)]">
          {{ pct != null ? `${Math.round(pct)}%` : '' }}
        </span>
      </div>
      <div class="mt-1.5 h-1.5 rounded-full bg-slate-900/80 overflow-hidden">
        <div
          class="h-full rounded-full bg-gradient-to-r transition-all duration-400"
          :class="barClass"
          :style="{ width: `${Math.min(100, Math.max(0, pct || 0))}%` }"
        />
      </div>
    </div>

    <!-- 元信息 -->
    <div
      v-if="account.gateway_only"
      class="flex justify-between text-[11px] text-[var(--text-muted)]"
    >
      <span>无独立令牌</span>
      <span>不参与签到</span>
    </div>
    <div v-else class="flex flex-wrap justify-between gap-1.5 text-[11px] text-[var(--text-muted)]">
      <span class="inline-flex items-center gap-1">
        <Flame class="w-3.5 h-3.5" />连签 {{ account.checkin_streak || 0 }} 天
      </span>
      <span class="inline-flex items-center gap-1">
        <Clock class="w-3.5 h-3.5" />{{
          account.last_checkin_at ? account.last_checkin_at.slice(5, 16) : '从未签到'
        }}
      </span>
      <span v-if="expireInfo" class="inline-flex items-center gap-1" :class="expireInfo.tone">
        <Flame v-if="expireInfo.expired" class="w-3.5 h-3.5" />
        <Hourglass v-else class="w-3.5 h-3.5" />
        {{ expireInfo.text }}
      </span>
    </div>

    <!-- 操作栏 -->
    <div class="flex items-center justify-between gap-1.5 flex-wrap pt-2.5 border-t border-[var(--border-subtle)]">
      <div class="flex gap-1.5 flex-wrap">
        <button
          type="button"
          class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] border cursor-pointer transition-colors disabled:opacity-50"
          :class="
            isPinned
              ? 'bg-indigo-500/18 text-indigo-300 border-indigo-500/35'
              : 'bg-[var(--bg-card)] text-[var(--text-secondary)] border-[var(--border-subtle)] hover:border-indigo-500/40 hover:text-[var(--text-primary)]'
          "
          :disabled="isBusy('pin')"
          @click="emit('pin', account.name)"
        >
          <Pin class="w-3 h-3" />{{ isPinned ? '当前使用' : '设为当前' }}
        </button>

        <template v-if="!account.gateway_only && account.id != null">
          <button
            type="button"
            class="px-2.5 py-1 rounded-lg text-[11px] border border-[var(--border-subtle)] bg-[var(--bg-card)] text-[var(--text-secondary)] hover:border-indigo-500/40 hover:text-[var(--text-primary)] cursor-pointer disabled:opacity-50"
            :disabled="isBusy('checkin')"
            @click="emit('checkin', account.id)"
          >
            {{ isBusy('checkin') ? '签到中…' : '签到' }}
          </button>
          <button
            type="button"
            class="px-2.5 py-1 rounded-lg text-[11px] border border-[var(--border-subtle)] bg-[var(--bg-card)] text-[var(--text-secondary)] hover:border-indigo-500/40 hover:text-[var(--text-primary)] cursor-pointer disabled:opacity-50"
            :disabled="isBusy('quota')"
            @click="emit('refreshQuota', account.id)"
          >
            {{ isBusy('quota') ? '刷新中…' : '余额' }}
          </button>
          <button
            type="button"
            class="px-2.5 py-1 rounded-lg text-[11px] border border-[var(--border-subtle)] bg-[var(--bg-card)] text-[var(--text-secondary)] hover:border-indigo-500/40 hover:text-[var(--text-primary)] cursor-pointer disabled:opacity-50"
            :disabled="isBusy('validate')"
            @click="emit('validate', account.id)"
          >
            {{ isBusy('validate') ? '验证中…' : '验证' }}
          </button>
        </template>
      </div>

      <div class="flex gap-1.5">
        <button
          v-if="!account.gateway_only"
          type="button"
          class="px-2.5 py-1 rounded-lg text-[11px] border border-[var(--border-subtle)] bg-[var(--bg-card)] cursor-pointer disabled:opacity-50"
          :class="account.status === 'active' ? 'text-[var(--red)]' : 'text-indigo-300'"
          :disabled="isBusy('toggle')"
          @click="emit('toggleStatus', account)"
        >
          {{ account.status === 'active' ? '停用' : '启用' }}
        </button>
        <button
          type="button"
          class="px-2.5 py-1 rounded-lg text-[11px] border border-[var(--border-subtle)] bg-[var(--bg-card)] text-[var(--red)] hover:border-rose-500/40 cursor-pointer"
          @click="emit('remove', account)"
        >
          删除
        </button>
      </div>
    </div>
  </div>
</template>
