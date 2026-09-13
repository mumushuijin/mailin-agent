<script setup lang="ts">
import { RouterView } from 'vue-router'
import { ConfigProvider } from 'ant-design-vue'
import TitleBar from '@/components/TitleBar.vue'
import MaintenanceNotifier from '@/components/MaintenanceNotifier.vue'
import ProjectSidebar from '@/components/ProjectSidebar.vue'
import { mailinTheme } from '@/theme/mailin'
import { isElectronApp } from '@/composables/useElectron'

const isElectron = isElectronApp()
</script>

<template>
  <ConfigProvider :theme="{ token: mailinTheme.token }">
    <div class="app-shell" :class="{ 'is-electron': isElectron }">
      <TitleBar v-if="isElectron" />
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
</style>
