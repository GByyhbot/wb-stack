import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  scrollBehavior: () => ({ top: 0 }),
  routes: [
    {
      path: '/',
      component: MainLayout,
      children: [
        {
          path: '',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
        },
        {
          path: 'recommend',
          name: 'recommend',
          component: () => import('@/views/RecommendView.vue'),
        },
        {
          path: 'usage',
          name: 'usage',
          component: () => import('@/views/UsageView.vue'),
        },
        {
          path: 'manage',
          name: 'manage',
          component: () => import('@/views/ManageView.vue'),
        },
        {
          path: 'quota',
          name: 'quota',
          component: () => import('@/views/QuotaView.vue'),
        },
      ],
    },
  ],
})
