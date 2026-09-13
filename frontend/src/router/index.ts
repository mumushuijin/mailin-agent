import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'

const isElectron = import.meta.env.VITE_ELECTRON === 'true'

const router = createRouter({
  history: isElectron
    ? createWebHashHistory(import.meta.env.BASE_URL)
    : createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'chat',
      component: () => import('../views/ChatView.vue'),
    },
    {
      path: '/settings',
      component: () => import('../views/SettingsView.vue'),
      redirect: '/settings/config',
      children: [
        {
          path: 'config',
          name: 'settings-config',
          component: () => import('../views/ConfigView.vue'),
        },
        {
          path: 'memory',
          name: 'settings-memory',
          component: () => import('../views/MemoryView.vue'),
        },
        {
          path: 'skills',
          name: 'settings-skills',
          component: () => import('../views/SkillsView.vue'),
        },
      ],
    },
    {
      path: '/sessions',
      redirect: (to) => ({ path: '/', query: to.query }),
    },
    {
      path: '/config',
      redirect: '/settings/config',
    },
    {
      path: '/memory',
      redirect: '/settings/memory',
    },
    {
      path: '/skills',
      redirect: '/settings/skills',
    },
  ],
})

export default router
