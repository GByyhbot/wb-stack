import { onUnmounted, ref } from 'vue'
import { oauthApi, type OAuthPollResponse, type OAuthStartPayload } from '@/api'

const POLL_INTERVAL_MS = 2500

/**
 * 官方 OAuth 设备授权流程：
 * start → 打开官方授权页 → 每 2.5s 轮询 poll → ok/error/expired 终态后自动停止。
 * 组件卸载时自动清理轮询定时器。
 */
export function useOAuthLogin(onSuccess?: () => void) {
  const running = ref(false)
  const statusText = ref('')
  const authUrl = ref('')

  let state: string | null = null
  let timer: number | null = null

  function stopTimer() {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }

  function reset() {
    stopTimer()
    state = null
    running.value = false
  }

  function render(text: string) {
    statusText.value = text
  }

  async function tick() {
    if (!state) return
    let r: OAuthPollResponse | null = null
    try {
      r = await oauthApi.poll(state)
    } catch (e) {
      render(`⏳ 网络重试中：${e instanceof Error ? e.message : e}`)
      return
    }
    if (r.status === 'pending') {
      render(`⏳ ${r.message || '等待授权…'}`)
      return
    }
    // 终态：ok / error / expired / cancelled
    reset()
    if (r.status === 'ok') {
      const x = r.import_result
      render(
        `✅ ${r.message || '授权成功'}：${x?.name || ''}（面板 ${x?.panel || '-'} · 代理池 ${
          x?.gateway || '-'
        }），已即时生效`,
      )
      onSuccess?.()
    } else {
      render(`❌ ${r.message || '授权未完成'}`)
    }
  }

  async function start(payload: OAuthStartPayload) {
    reset()
    running.value = true
    try {
      const r = await oauthApi.start(payload)
      state = r.state
      authUrl.value = r.authUrl
      render('⏳ 等待浏览器完成授权…（10 分钟内有效，链接如上，可复制到手机扫码）')
      window.open(r.authUrl, '_blank', 'noopener')
      timer = window.setInterval(tick, POLL_INTERVAL_MS)
      tick()
    } catch (e) {
      reset()
      const msg = e instanceof Error ? e.message : String(e)
      render(`❌ 授权启动失败：${msg}`)
      throw new Error(msg)
    }
  }

  async function cancel() {
    const s = state
    reset()
    render('已取消授权')
    if (s) {
      try {
        await oauthApi.cancel(s)
      } catch {
        // 取消失败不阻塞界面
      }
    }
  }

  onUnmounted(() => {
    // 仅清理本地定时器；授权会话由服务端 10 分钟超时兜底
    stopTimer()
  })

  return { running, statusText, authUrl, start, cancel }
}
