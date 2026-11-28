<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useDocumentVisibility, useIntervalFn } from '@vueuse/core'
import {
  BarChart3,
  Bot,
  Compass,
  Gauge,
  LayoutDashboard,
  SlidersHorizontal,
} from 'lucide-vue-next'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const store = useAppStore()
const visibility = useDocumentVisibility()

const navItems = [
  { name: 'dashboard', to: '/', label: '总览', icon: LayoutDashboard },
  { name: 'recommend', to: '/recommend', label: '用量推荐', icon: Compass },
  { name: 'usage', to: '/usage', label: '用量统计', icon: BarChart3 },
  { name: 'manage', to: '/manage', label: '账号与设置', icon: SlidersHorizontal },
  { name: 'quota', to: '/quota', label: '额度监控', icon: Gauge },
] as const

const healthPill = computed(() => {
  const d = store.diagnostics
  if (!d) {
    return {
      text: 'Checking',
      cls: 'bg-slate-400/10 text-[var(--text-muted)] border-[var(--border-subtle)]',
      title: '正在获取双进程诊断状态',
    }
  }
  if (d.overall === 'ok') {
    return {
      text: 'Healthy',
      cls: 'bg-emerald-400/10 text-[var(--green)] border-emerald-400/20',
      title: `Manager: ${d.manager.status} · Rotator: ${d.rotator.status}`,
    }
  }
  return {
    text: 'Degraded',
    cls: 'bg-amber-400/15 text-[var(--amber)] border-amber-400/30',
    title: `Manager: ${d.manager.status} · Rotator: ${d.rotator.status}`,
  }
})

const { pause, resume } = useIntervalFn(
  () => {
    store.refreshCore()
  },
  60000,
  { immediate: false },
)

watch(visibility, (state) => {
  if (state === 'visible') {
    store.refreshCore()
    resume()
  } else {
    pause()
  }
})

onMounted(() => {
  store.refreshCore()
  if (visibility.value === 'visible') resume()
})
</script>

<template>
  <div class="flex min-h-screen flex-col lg:flex-row">
    <!-- 侧边栏 -->
    <aside
      class="w-full lg:w-64 shrink-0 sticky top-0 z-20 lg:h-screen bg-[var(--bg-sidebar)] border-b lg:border-b-0 lg:border-r border-[var(--border-subtle)] flex flex-row lg:flex-col justify-between items-center lg:items-stretch"
    >
      <div class="flex lg:flex-col items-center lg:items-stretch flex-1 min-w-0">
        <!-- 品牌区 -->
        <div class="px-3.5 py-2.5 lg:p-5 lg:pb-4 lg:border-b border-[var(--border-subtle)] shrink-0">
          <div class="flex items-center gap-3">
            <div
              class="w-9 h-9 lg:w-10 lg:h-10 rounded-xl shrink-0 text-white flex items-center justify-center bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-indigo-500/25"
            >
              <Bot class="w-5 h-5" />
            </div>
            <div>
              <div class="text-[15px] font-bold tracking-tight">WorkBuddy</div>
              <div class="hidden lg:block text-xs text-[var(--text-secondary)] mt-0.5">多账号管理</div>
            </div>
          </div>
          <div class="hidden lg:flex items-center gap-2 mt-3 text-[11px] text-[var(--text-muted)]">
            <span
              class="w-1.5 h-1.5 rounded-full shrink-0 animate-pulse-soft"
              :class="store.diagnostics?.overall === 'degraded' ? 'bg-[var(--amber)]' : 'bg-[var(--green)]'"
            />
            <span>签到 · 积分 · 轮换网关 :8310</span>
          </div>
        </div>

        <!-- 导航 -->
        <nav class="flex lg:flex-col gap-1 p-2 lg:p-3 overflow-x-auto flex-1">
          <RouterLink
            v-for="item in navItems"
            :key="item.name"
            :to="item.to"
            class="flex items-center justify-between gap-2 px-3 py-2 lg:px-3.5 lg:py-2.5 rounded-xl text-[13px] font-medium whitespace-nowrap transition-all"
            :class="
              route.name === item.name
                ? 'bg-[var(--accent-soft)] text-indigo-400 shadow-[inset_0_0_0_1px_rgba(99,102,241,0.35)]'
                : 'text-[var(--text-secondary)] hover:bg-slate-400/8 hover:text-[var(--text-primary)]'
            "
          >
            <span class="flex items-center gap-2.5">
              <component :is="item.icon" class="w-4 h-4 shrink-0" />
              <span>{{ item.label }}</span>
            </span>

            <span
              v-if="item.name === 'recommend' && store.riskCount > 0"
              class="text-[11px] font-semibold px-1.5 py-0.5 rounded-full bg-rose-400/20 text-rose-400 border border-rose-400/30"
            >
              {{ store.riskCount }}
            </span>

            <span
              v-if="item.name === 'quota'"
              class="mono text-[10px] uppercase px-1.5 py-0.5 rounded-md border"
              :class="
                store.quotaModeBadge === 'DOWN'
                  ? 'bg-rose-400/15 text-rose-400 border-rose-400/25'
                  : store.quotaModeBadge === 'ENF'
                    ? 'bg-emerald-400/15 text-emerald-400 border-emerald-400/25'
                    : 'bg-amber-400/15 text-amber-400 border-amber-400/25'
              "
            >
              {{ store.quotaModeBadge }}
            </span>
          </RouterLink>
        </nav>
      </div>

      <!-- 底部真实诊断状态 -->
      <div
        class="hidden lg:flex items-center justify-between gap-2 p-4 border-t border-[var(--border-subtle)]"
      >
        <div>
          <div class="text-xs font-medium">wb-manager</div>
          <div class="mono text-[10px] text-[var(--text-muted)] mt-0.5">
            wb-web v2.3
          </div>
        </div>
        <span
          class="mono text-[10px] px-2 py-0.5 rounded-md border whitespace-nowrap"
          :class="healthPill.cls"
          :title="healthPill.title"
        >
          {{ healthPill.text }}
        </span>
      </div>
    </aside>

    <!-- 主内容区 -->
    <main class="flex-1 min-w-0 max-w-[1440px] w-full mx-auto p-4 lg:p-8">
      <RouterView />
    </main>
  </div>
</template>
