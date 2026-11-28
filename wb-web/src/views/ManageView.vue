<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Clock,
  ExternalLink,
  Network,
  Plus,
  QrCode,
  ScrollText,
  Save,
  UserPlus,
} from 'lucide-vue-next'
import {
  accountsApi,
  systemApi,
  type CheckinLogEntry,
  type GatewayInfoResponse,
  type SettingsMap,
} from '@/api'
import { useAppStore } from '@/stores/app'
import { useOAuthLogin } from '@/composables/useOAuthLogin'
import { fmtEventTime } from '@/utils/format'

const store = useAppStore()

// ---- 添加账号表单 ----
const form = reactive({
  name: '',
  user_id: '',
  token: '',
  priority: '100',
  quota_total: '0',
  quota_used: '0',
})
const adding = ref(false)

async function addAccount() {
  const name = form.name.trim()
  const token = form.token.trim()
  if (!name || !token) {
    store.notify('名称和 Token 必填', false)
    return
  }
  adding.value = true
  try {
    await accountsApi.create({
      name,
      user_id: form.user_id.trim(),
      token,
      priority: parseInt(form.priority, 10) || 100,
      quota_total: parseFloat(form.quota_total) || 0,
      quota_used: parseFloat(form.quota_used) || 0,
    })
    store.notify('账号已添加')
    form.name = ''
    form.user_id = ''
    form.token = ''
    await Promise.allSettled([store.fetchAccounts(), store.fetchActive()])
  } catch (e) {
    store.notify(`添加失败: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    adding.value = false
  }
}

// ---- 官方 OAuth 设备授权 ----
const oauthRealm = ref<'cn' | 'intl'>('cn')
const oauthName = ref('')
const oauthPriority = ref('100')

const oauth = useOAuthLogin(() => {
  store.notify('账号已入库')
  void Promise.allSettled([store.fetchAccounts(), store.fetchActive()])
})

async function oauthStart() {
  try {
    await oauth.start({
      realm: oauthRealm.value,
      name: oauthName.value.trim(),
      priority: parseInt(oauthPriority.value, 10) || 100,
    })
  } catch {
    store.notify('授权启动失败', false)
  }
}

// ---- 调度与告警设置 ----
const settings = reactive<Required<SettingsMap>>({
  checkin_time: '',
  quota_min_percent: '',
  switch_enabled: '1',
  growth_enabled: '1',
  wecom_webhook: '',
})
const savingSettings = ref(false)

async function loadSettings() {
  try {
    const d = await systemApi.getSettings()
    const s = d.settings || {}
    settings.checkin_time = s.checkin_time || ''
    settings.quota_min_percent = s.quota_min_percent != null ? String(s.quota_min_percent) : ''
    settings.switch_enabled = s.switch_enabled || '1'
    settings.growth_enabled = s.growth_enabled || '1'
    settings.wecom_webhook = s.wecom_webhook || ''
  } catch (e) {
    store.notify(`设置加载失败: ${e instanceof Error ? e.message : e}`, false)
  }
}

async function saveSettings() {
  if (savingSettings.value) return
  savingSettings.value = true
  try {
    await systemApi.saveSettings({
      checkin_time: settings.checkin_time.trim(),
      quota_min_percent: parseFloat(String(settings.quota_min_percent)) || 10,
      switch_enabled: settings.switch_enabled,
      growth_enabled: settings.growth_enabled,
      wecom_webhook: settings.wecom_webhook.trim(),
    })
    store.notify('设置已保存')
  } catch (e) {
    store.notify(`保存失败: ${e instanceof Error ? e.message : e}`, false)
  } finally {
    savingSettings.value = false
  }
}

// ---- 网关协议与模型路由 ----
const gatewayInfo = ref<GatewayInfoResponse | null>(null)
const gatewayRoutes = computed(() => Object.entries(gatewayInfo.value?.routes || {}))
const gatewayModels = computed(() => (gatewayInfo.value?.models || []).join(' · ') || '-')

// ---- 签到与成长日志（复用 store 合并拉取的 logs，取最近 50 条） ----
const recentLogs = computed<CheckinLogEntry[]>(() => store.logs.slice(0, 50))

function isGrowth(l: CheckinLogEntry): boolean {
  return (l.message || '').startsWith('成长中心：')
}
function logMessage(l: CheckinLogEntry): string {
  const msg = isGrowth(l) ? (l.message || '').slice(5) : l.message || ''
  return isGrowth(l) ? msg.slice(0, 160) : msg.slice(0, 60)
}
function okBool(l: CheckinLogEntry): boolean {
  return Boolean(l.ok)
}

onMounted(() => {
  loadSettings()
  systemApi
    .getGatewayInfo()
    .then((d) => (gatewayInfo.value = d))
    .catch(() => {
      // 网关信息失败不阻塞面板
    })
})
</script>

<template>
  <div class="flex flex-col gap-6">
    <div>
      <h1 class="text-xl font-bold tracking-tight">账号与设置</h1>
      <p class="text-xs text-[var(--text-secondary)] mt-1">
        接入新账号、配置定时签到与告警策略、查看网关多协议路由及成长中心日志
      </p>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
      <!-- 左列 -->
      <div class="flex flex-col gap-4 min-w-0">
        <!-- 添加账号 -->
        <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-5">
          <div class="flex items-center gap-2 text-sm font-semibold mb-4 flex-wrap">
            <UserPlus class="w-4 h-4 text-indigo-400" />
            <span>添加账号</span>
            <span class="text-[11px] font-normal text-[var(--text-muted)]">令牌随刷新自动轮换更新</span>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">名称（如：主号）</span>
              <input
                v-model="form.name"
                type="text"
                placeholder="主号"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              />
            </label>
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">账号标识（备注）</span>
              <input
                v-model="form.user_id"
                type="text"
                placeholder="邮箱/手机号"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              />
            </label>
            <label class="block sm:col-span-2">
              <span class="text-[11px] text-[var(--text-secondary)]">Refresh Token（长效令牌）</span>
              <input
                v-model="form.token"
                type="text"
                placeholder="从 rt_auth.py --export-rt 导出"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 mono text-xs outline-none focus:border-indigo-500"
              />
            </label>
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">优先级（1=主账号）</span>
              <input
                v-model="form.priority"
                type="number"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              />
            </label>
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">可用积分（0=自动刷新）</span>
              <input
                v-model="form.quota_total"
                type="number"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              />
            </label>
            <label class="block sm:col-span-2">
              <span class="text-[11px] text-[var(--text-secondary)]">已用积分（0=自动）</span>
              <input
                v-model="form.quota_used"
                type="number"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              />
            </label>
          </div>
          <div class="mt-4 flex items-center justify-between gap-3 flex-wrap">
            <span class="text-[11px] text-[var(--text-muted)]">可用/已用积分留空将自动从上游查询</span>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium cursor-pointer disabled:opacity-50"
              :disabled="adding"
              @click="addAccount"
            >
              <Plus class="w-3.5 h-3.5" />{{ adding ? '添加中…' : '添加账号' }}
            </button>
          </div>

          <!-- 官网扫码登录 -->
          <div class="mt-5 pt-4 border-t border-[var(--border-subtle)]">
            <div class="flex items-center gap-2 text-sm font-semibold">
              <QrCode class="w-4 h-4 text-indigo-400" />
              <span>官网扫码登录</span>
              <span class="text-[11px] font-normal text-[var(--text-muted)]">CLI 通道，免手工导出令牌</span>
            </div>
            <p class="mt-2 text-[11px] leading-relaxed text-[var(--text-muted)]">
              点「开始授权」→ 打开官方登录页：<b class="text-[var(--text-secondary)]">国内版微信扫码</b>，国际版网页登录 → 自动取回令牌，
              <b class="text-[var(--text-secondary)]">同时写入面板与代理池</b>，无需重启容器。仍需本人在官方页完成一次登录；会话 10 分钟内有效，令牌只在后端流转。
            </p>
            <div class="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-3">
              <label class="block">
                <span class="text-[11px] text-[var(--text-secondary)]">版本</span>
                <select
                  v-model="oauthRealm"
                  class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
                >
                  <option value="cn">国内版（copilot.tencent.com）</option>
                  <option value="intl">国际版（workbuddy.ai）</option>
                </select>
              </label>
              <label class="block">
                <span class="text-[11px] text-[var(--text-secondary)]">名称（留空用官方昵称）</span>
                <input
                  v-model="oauthName"
                  type="text"
                  placeholder="如：姐姐大号"
                  class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
                />
              </label>
              <label class="block">
                <span class="text-[11px] text-[var(--text-secondary)]">优先级（1=主账号）</span>
                <input
                  v-model="oauthPriority"
                  type="number"
                  class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
                />
              </label>
            </div>
            <div class="mt-4 flex items-center gap-2 flex-wrap">
              <button
                type="button"
                class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium cursor-pointer disabled:opacity-50"
                :disabled="oauth.running.value"
                @click="oauthStart"
              >
                <ExternalLink class="w-3.5 h-3.5" />开始授权
              </button>
              <button
                v-if="oauth.running.value"
                type="button"
                class="px-3.5 py-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] cursor-pointer"
                @click="oauth.cancel"
              >
                取消
              </button>
            </div>
            <div
              v-if="oauth.statusText.value || oauth.authUrl.value"
              class="mt-3 rounded-xl bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-3 py-2.5 text-[11px] leading-relaxed break-all"
            >
              <a
                v-if="oauth.authUrl.value"
                :href="oauth.authUrl.value"
                target="_blank"
                rel="noopener"
                class="text-indigo-400 underline decoration-indigo-400/40 break-all"
              >
                {{ oauth.authUrl.value }}
              </a>
              <div v-if="oauth.statusText.value" class="text-[var(--text-secondary)]">
                {{ oauth.statusText.value }}
              </div>
            </div>
          </div>
        </div>

        <!-- 调度与告警设置 -->
        <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-5">
          <div class="flex items-center gap-2 text-sm font-semibold mb-4">
            <Clock class="w-4 h-4 text-indigo-400" />
            <span>调度与告警设置</span>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">每日签到时间（±30 分钟抖动）</span>
              <input
                v-model="settings.checkin_time"
                type="text"
                placeholder="08:30"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              />
            </label>
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">余额告警阈值 (%)</span>
              <input
                v-model="settings.quota_min_percent"
                type="number"
                placeholder="10"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              />
            </label>
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">自动切换</span>
              <select
                v-model="settings.switch_enabled"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              >
                <option value="1">开启</option>
                <option value="0">关闭</option>
              </select>
            </label>
            <label class="block">
              <span class="text-[11px] text-[var(--text-secondary)]">成长中心（签到后领旅行/任务/连登/盲盒）</span>
              <select
                v-model="settings.growth_enabled"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 text-xs outline-none focus:border-indigo-500"
              >
                <option value="1">开启</option>
                <option value="0">关闭</option>
              </select>
            </label>
            <label class="block sm:col-span-2">
              <span class="text-[11px] text-[var(--text-secondary)]">企业微信机器人 Webhook（异常/到期告警，留空不通知）</span>
              <input
                v-model="settings.wecom_webhook"
                type="text"
                placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
                class="mt-1 w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] px-3 py-2 mono text-xs outline-none focus:border-indigo-500"
              />
            </label>
          </div>
          <div class="mt-4">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium cursor-pointer disabled:opacity-50"
              :disabled="savingSettings"
              @click="saveSettings"
            >
              <Save class="w-3.5 h-3.5" />{{ savingSettings ? '保存中…' : '保存设置' }}
            </button>
          </div>
        </div>

        <!-- 网关协议与模型路由 -->
        <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-5">
          <div class="flex items-center gap-2 text-sm font-semibold flex-wrap">
            <Network class="w-4 h-4 text-indigo-400" />
            <span>网关协议与模型路由</span>
            <span class="text-[11px] font-normal text-[var(--text-muted)]">:8310 三协议同池</span>
          </div>
          <div class="mt-4 grid grid-cols-3 gap-2">
            <div class="rounded-xl bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-3 py-2.5">
              <div class="text-xs font-semibold">OpenAI</div>
              <div class="mono text-[10px] text-[var(--text-muted)] mt-0.5">/v1/chat/completions</div>
            </div>
            <div class="rounded-xl bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-3 py-2.5">
              <div class="text-xs font-semibold">Claude Code</div>
              <div class="mono text-[10px] text-[var(--text-muted)] mt-0.5">/v1/messages</div>
            </div>
            <div class="rounded-xl bg-[var(--bg-elevated)] border border-[var(--border-subtle)] px-3 py-2.5">
              <div class="text-xs font-semibold">Codex</div>
              <div class="mono text-[10px] text-[var(--text-muted)] mt-0.5">/v1/responses</div>
            </div>
          </div>
          <p class="mt-3 text-[11px] text-[var(--text-muted)]">
            客户端的档位模型名由网关按下表改写，上游模型零配置：
          </p>
          <div class="mt-2 overflow-auto">
            <table class="w-full border-collapse text-xs">
              <thead>
                <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
                  <th class="px-3 py-2 text-left">客户端档位</th>
                  <th class="px-3 py-2 text-left">映射上游模型</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="!gatewayRoutes.length">
                  <td colspan="2" class="px-3 py-3 text-[var(--text-muted)]">无路由</td>
                </tr>
                <tr v-for="[k, v] in gatewayRoutes" :key="k" class="border-b border-slate-800/50">
                  <td class="px-3 py-2 mono">{{ k }}</td>
                  <td class="px-3 py-2 mono text-indigo-400">{{ v }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="mt-3 text-[11px] text-[var(--text-muted)]">
            路由可用网关环境变量 <span class="mono">WB_MODEL_ROUTES</span>（JSON）覆盖，如
            <span class="mono">{"opus":"hy4free"}</span>；上游模型：<span class="mono">{{ gatewayModels }}</span>
          </p>
        </div>
      </div>

      <!-- 右列：签到与成长日志 -->
      <div class="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-card)] overflow-hidden">
        <div
          class="px-5 py-4 border-b border-[var(--border-subtle)] flex items-center justify-between gap-3"
        >
          <div class="flex items-center gap-2 text-sm font-semibold">
            <ScrollText class="w-4 h-4 text-[var(--green)]" />
            <span>签到与成长日志</span>
          </div>
          <span class="text-[11px] text-[var(--text-muted)]">最近 50 条</span>
        </div>
        <div class="max-h-[760px] overflow-auto">
          <table class="w-full border-collapse text-xs">
            <thead class="sticky top-0 bg-[var(--bg-card)] z-10">
              <tr class="border-b border-[var(--border-subtle)] text-[11px] uppercase text-[var(--text-muted)]">
                <th class="px-4 py-2.5 text-left">时间</th>
                <th class="px-4 py-2.5 text-left">账号</th>
                <th class="px-4 py-2.5 text-left">结果</th>
                <th class="px-4 py-2.5 text-left">详情</th>
                <th class="px-4 py-2.5 text-right">奖励</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!recentLogs.length">
                <td colspan="5" class="px-4 py-6 text-center text-[var(--text-muted)]">暂无日志</td>
              </tr>
              <tr
                v-for="(l, idx) in recentLogs"
                :key="l.id ?? idx"
                class="border-b border-slate-800/50"
              >
                <td class="px-4 py-2.5 mono text-[var(--text-muted)] whitespace-nowrap">
                  {{ fmtEventTime(l.created_at) }}
                </td>
                <td class="px-4 py-2.5 whitespace-nowrap">{{ l.account_name || l.account_id }}</td>
                <td class="px-4 py-2.5 whitespace-nowrap">
                  <span
                    class="inline-flex items-center gap-1"
                    :class="okBool(l) ? 'text-[var(--green)]' : 'text-[var(--red)]'"
                  >
                    <span
                      v-if="isGrowth(l)"
                      class="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-400/12 border border-emerald-400/25"
                    >
                      🌱 成长
                    </span>
                    {{ okBool(l) ? '✔ 成功' : '✘ 失败' }}
                  </span>
                </td>
                <td class="px-4 py-2.5 text-[var(--text-muted)]" :title="l.message || ''">
                  {{ logMessage(l) }}
                </td>
                <td class="px-4 py-2.5 mono text-right whitespace-nowrap">{{ l.reward || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>
