<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { RouterView } from 'vue-router'
import { ConfigProvider } from 'ant-design-vue'
import TitleBar from '@/components/TitleBar.vue'
import MaintenanceNotifier from '@/components/MaintenanceNotifier.vue'
import ProjectSidebar from '@/components/ProjectSidebar.vue'
import { mailinTheme } from '@/theme/mailin'
import { isElectronApp } from '@/composables/useElectron'

const isElectron = isElectronApp()
type BackendState = Awaited<ReturnType<NonNullable<NonNullable<Window['mailin']>['backend']>['getState']>>
const backendState = ref<BackendState | null>(null)
let poll: ReturnType<typeof setInterval> | null = null
const refresh = async () => {
  if (window.mailin?.backend) backendState.value = await window.mailin.backend.getState()
}
const retry = async () => {
  if (!window.mailin?.backend) return
  backendState.value = await window.mailin.backend.retry() as BackendState
}
const chooseRoot = async () => {
  if (!window.mailin?.backend) return
  backendState.value = await window.mailin.backend.chooseRuntimeRoot() as BackendState
}
const openLogs = async () => {
  if (backendState.value?.paths?.log) await window.mailin?.shell?.openPath(backendState.value.paths.log)
}
onMounted(() => {
  void refresh()
  if (window.mailin?.backend) poll = setInterval(() => void refresh(), 2000)
})
onUnmounted(() => { if (poll) clearInterval(poll) })
</script>

<template>
  <ConfigProvider :theme="{ token: mailinTheme.token }">
    <div class="app-shell" :class="{ 'is-electron': isElectron }">
      <TitleBar v-if="isElectron" />
      <div v-if="backendState && !['ready', 'external'].includes(backendState.status)" class="backend-banner" role="status">
        <span>后端{{ backendState.status === 'starting' ? '启动中' : '不可用' }}：{{ backendState.error || '正在等待服务就绪' }}</span>
        <button v-if="backendState.status !== 'starting'" @click="retry">重试</button>
        <button v-if="backendState.status !== 'starting'" @click="chooseRoot">选择运行根</button>
        <button v-if="backendState.paths?.log" @click="openLogs">打开日志</button>
      </div>
      <div class="app-container">
        <ProjectSidebar />
        <main class="main-content">
          <RouterView />
        </main>
        <MaintenanceNotifier />
      </div>
    </div>
  </ConfigProvider>
</template>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

.app-container {
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.main-content {
  flex: 1;
  background-color: var(--color-background);
  overflow: auto;
  min-height: 0;
}
.backend-banner { display: flex; align-items: center; gap: 12px; padding: 9px 16px; background: #fff3d5; color: #4a3415; font-size: 13px; }
.backend-banner span { flex: 1; overflow-wrap: anywhere; }
.backend-banner button { border: 1px solid currentColor; border-radius: 4px; background: transparent; color: inherit; cursor: pointer; padding: 3px 8px; white-space: nowrap; }
</style>
