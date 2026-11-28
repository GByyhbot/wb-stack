// ---- 账号管理 ----
export type AccountStatus = 'active' | 'disabled' | 'error' | 'gateway'

export interface Account {
  id: number | null
  name: string
  user_id: string
  priority: number
  quota_total: number
  quota_used: number
  status: AccountStatus
  token: string
  note: string | null
  last_checkin_at: string | null
  last_checkin_result: string | null
  checkin_streak: number
  quota_checked_at: string | null
  quota_expire_at: string | null
  quota_percent: number | null
  available_credits: number | null
  gateway_only: boolean
  gateway_disabled?: boolean | null
}

export interface AccountCreatePayload {
  name: string
  user_id: string
  token: string
  priority?: number
  quota_total?: number
  quota_used?: number
}

export interface AccountUpdatePayload {
  name?: string
  user_id?: string
  token?: string
  priority?: number
  quota_total?: number
  quota_used?: number
  status?: 'active' | 'disabled' | 'error'
  note?: string
}

export interface ActiveAccount extends Account {
  pinned: boolean
  last_credits?: number | null
}

export interface ActiveAccountResponse {
  active: ActiveAccount
  pinned: string | null
  warning: string
}

export interface CheckinResult {
  ok: boolean
  message: string
  reward?: string | number | null
  total_credits?: number | null
  streak_days?: number | null
  available_credits?: number | null
  growth?: { credits_gained?: number } | null
}

export interface RefreshQuotaResult {
  ok: boolean
  quota_total: number | null
  granted_total: number | null
  used_total: number | null
  expire_at: string | null
  days_left: number | null
  message: string
}

// ---- 用量统计与限量 ----
export interface UsageDay {
  date: string
  requests: number
  tin: number
  tout: number
}

export interface UsageByModel {
  model: string
  requests: number
  tin: number
  tout: number
}

export interface UsageByAccountModel {
  account: string
  model: string
  requests: number
  tin: number
  tout: number
  failed: number
  limited: number
  limit_requests: number
  limit_tokens: number
  req_pct: number | null
  tok_pct: number | null
}

export interface RecentRequestLog {
  ts?: string
  account?: string
  model?: string
  status: number
  duration_ms?: number
  prompt_tokens?: number
  completion_tokens?: number
  error?: string
}

export interface UsageLimitItem {
  requests: number
  tokens: number
}

export interface UsageResponse {
  since: string
  per_day: UsageDay[]
  by_model: UsageByModel[]
  by_account_model: UsageByAccountModel[]
  limits: Record<string, UsageLimitItem>
  recent: RecentRequestLog[]
}

// ---- 用量推荐 ----
export interface RecommendationRisk {
  type: string
  message: string
}

export interface RecommendationCandidate {
  account: string
  reasons: string[]
}

export interface RecommendationShare {
  account: string
  share_pct: number
  suggested_requests: number
  limit_requests?: number
}

export interface RecommendationModel {
  model: string
  expected_daily_requests: number
  primary: RecommendationCandidate | null
  backup: RecommendationCandidate | null
  shares: RecommendationShare[]
}

export interface RecommendationAccount {
  account: string
  available_credits: number | null
  days_left: number | null
  expire_at: string | null
  daily_credits_target: number | null
  suggested_requests: number
  candidate: boolean
  excluded_reason?: string
  models: Array<{ model: string; suggested_requests: number }>
}

export interface RecommendationResponse {
  date: string
  window_days: number
  risks: RecommendationRisk[]
  models: RecommendationModel[]
  accounts: RecommendationAccount[]
}

// ---- 日志、设置与网关信息 ----
export interface CheckinLogEntry {
  id?: number
  created_at: string
  account_id: number | null
  account_name: string | null
  ok: boolean | number
  message: string | null
  reward: string | number | null
}

export interface SettingsMap {
  checkin_time?: string
  quota_min_percent?: string | number
  switch_enabled?: string
  growth_enabled?: string
  wecom_webhook?: string
}

export interface GatewayInfoResponse {
  endpoints: Array<{ path: string; name: string; clients: string }>
  routes: Record<string, string>
  models: string[]
}

// ---- 健康诊断 ----
export interface DiagnosticsResponse {
  generated_at: string
  overall: 'ok' | 'degraded'
  manager: {
    status: 'ok' | 'error'
    uptime_seconds: number
    accounts_total: number
    accounts_active: number
    accounts_error: number
  }
  rotator: {
    status: 'ok' | 'down' | string
    error?: string
  }
}

// ---- 模型额度监控（严格只读） ----
export type QuotaCellLevel =
  | 'normal'
  | 'warning'
  | 'threshold'
  | 'cooldown'
  | 'exhausted'
  | 'disabled'
  | 'unknown'

export interface QuotaCell {
  account_name: string
  model_id: string
  status?: QuotaCellLevel | string
  status_label?: string
  success_count?: number
  tokens_used?: number
  percent?: number | null
  safe_request_limit?: number | null
  remaining_seconds?: number | null
}

export interface QuotaAccountEntry {
  name: string
  priority?: number
  disabled?: boolean
}

export interface QuotaSnapshot {
  status?: 'ok' | 'down' | string
  quota_enabled?: boolean
  quota_mode?: 'observe' | 'enforce' | string
  generated_at?: string
  accounts?: QuotaAccountEntry[]
  models?: string[]
  cells?: QuotaCell[]
  error?: string
}

export interface QuotaEvent {
  created_at: string
  account_name: string | null
  model_id: string | null
  event_type: string
  http_status: number | null
  error_code: string | null
  retry_after: number | null
  reset_at: string | null
  has_raw: boolean
}

// ---- 官方 OAuth 设备授权 ----
export interface OAuthStartPayload {
  realm: 'cn' | 'intl'
  name: string
  priority: number
}

export interface OAuthStartResponse {
  state: string
  authUrl: string
}

export interface OAuthPollResponse {
  status: 'pending' | 'ok' | 'error' | 'expired' | 'cancelled'
  message?: string
  import_result?: {
    name: string
    uid: string
    realm: string
    panel: string
    panel_id: number
    gateway: string
  }
}
