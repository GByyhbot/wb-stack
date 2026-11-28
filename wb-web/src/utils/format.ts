import type { Account } from '@/api'

export const AVATAR_COLORS = ['#6366f1', '#8b5cf6', '#34d399', '#f59e0b', '#fb7185', '#06b6d4']
export const SHARE_COLORS = [
  '#6366f1',
  '#8b5cf6',
  '#34d399',
  '#f59e0b',
  '#fb7185',
  '#06b6d4',
  '#a78bfa',
  '#ec4899',
]

export function fmtTokens(n?: number | null): string {
  const val = n || 0
  if (val >= 1e6) return (val / 1e6).toFixed(1) + 'M'
  if (val >= 1e3) return (val / 1e3).toFixed(1) + 'k'
  return String(val)
}

export function fmtCredits(n?: number | null): string {
  return Number(n || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function fmtEventTime(s?: string | null): string {
  if (!s) return '—'
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/)
  if (m) return `${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`
  return String(s).slice(5, 19).replace('T', ' ')
}

export function localDateStr(d = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`
}

export function availOf(a: Account): number {
  return a.available_credits != null
    ? a.available_credits
    : Math.max(0, (a.quota_total || 0) - (a.quota_used || 0))
}
