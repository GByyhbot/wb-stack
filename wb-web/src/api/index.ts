import { request } from './client'
import type {
  Account,
  AccountCreatePayload,
  AccountUpdatePayload,
  ActiveAccountResponse,
  CheckinLogEntry,
  CheckinResult,
  DiagnosticsResponse,
  GatewayInfoResponse,
  OAuthPollResponse,
  OAuthStartPayload,
  OAuthStartResponse,
  QuotaEvent,
  QuotaSnapshot,
  RecommendationResponse,
  RefreshQuotaResult,
  SettingsMap,
  UsageLimitItem,
  UsageResponse,
} from './types'

export * from './types'
export { ApiError } from './client'

export const accountsApi = {
  list: () => request<{ accounts: Account[] }>('GET', '/api/accounts'),
  create: (payload: AccountCreatePayload) =>
    request<{ id: number }>('POST', '/api/accounts', payload),
  update: (id: number, payload: AccountUpdatePayload) =>
    request<{ ok: boolean }>('PUT', `/api/accounts/${id}`, payload),
  remove: (id: number) => request<{ ok: boolean }>('DELETE', `/api/accounts/${id}`),
  removeGatewayOnly: (name: string) =>
    request<{ ok: boolean }>('POST', '/api/gateway-accounts/delete', { name }),
  checkinOne: (id: number) => request<CheckinResult>('POST', `/api/accounts/${id}/checkin`),
  checkinAll: () => request<{ results: CheckinResult[] }>('POST', '/api/checkin/all'),
  refreshQuota: (id: number) =>
    request<RefreshQuotaResult>('POST', `/api/accounts/${id}/refresh-quota`),
  validateToken: (id: number) =>
    request<{ ok: boolean; message: string }>('POST', `/api/accounts/${id}/validate`),
  getActive: () => request<ActiveAccountResponse>('GET', '/api/active-account'),
  getPin: () => request<{ name: string | null }>('GET', '/api/pin-account'),
  setPin: (name: string | null) =>
    request<{ ok: boolean; pinned: string | null; path: string }>('POST', '/api/pin-account', {
      name,
    }),
}

export const usageApi = {
  getSummary: (days = 7) => request<UsageResponse>('GET', `/api/usage?days=${days}`),
  getRecommendation: () => request<RecommendationResponse>('GET', '/api/usage/recommendation'),
  saveLimits: (limits: Record<string, UsageLimitItem>) =>
    request<{ ok: boolean; limits: Record<string, UsageLimitItem> }>('POST', '/api/usage/limits', {
      limits,
    }),
}

export const quotaApi = {
  // 严格只读：仅暴露 GET 接口
  getSnapshot: () => request<QuotaSnapshot>('GET', '/api/model-quotas'),
  getEvents: (limit = 50) =>
    request<QuotaEvent[]>('GET', `/api/model-quotas/events?limit=${limit}`),
}

export const systemApi = {
  getLogs: (limit = 100) =>
    request<{ logs: CheckinLogEntry[] }>('GET', `/api/logs?limit=${limit}`),
  getSettings: () => request<{ settings: SettingsMap }>('GET', '/api/settings'),
  saveSettings: (payload: SettingsMap) =>
    request<{ ok: boolean; changed: string[] }>('PUT', '/api/settings', payload),
  getGatewayInfo: () => request<GatewayInfoResponse>('GET', '/api/gateway-info'),
  getDiagnostics: () => request<DiagnosticsResponse>('GET', '/api/diagnostics'),
}

export const oauthApi = {
  start: (payload: OAuthStartPayload) =>
    request<OAuthStartResponse>('POST', '/api/login/start', payload),
  poll: (state: string) =>
    request<OAuthPollResponse>('GET', `/api/login/poll?state=${encodeURIComponent(state)}`),
  cancel: (state: string) => request<{ ok?: boolean }>('POST', '/api/login/cancel', { state }),
}
