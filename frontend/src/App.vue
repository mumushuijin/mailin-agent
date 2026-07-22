<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { Menu, ConfigProvider } from 'ant-design-vue'
import { MessageOutlined, SettingOutlined, HistoryOutlined, BookOutlined } from '@ant-design/icons-vue'
import MailinLogo from '@/components/MailinLogo.vue'
import TitleBar from '@/components/TitleBar.vue'
import MaintenanceNotifier from '@/components/MaintenanceNotifier.vue'
import { mailinTheme } from '@/theme/mailin'
import { isElectronApp } from '@/composables/useElectron'

const route = useRoute()
const isElectron = isElectronApp()
</script>

<template>
  <ConfigProvider :theme="{ token: mailinTheme.token }">
    <div class="app-shell" :class="{ 'is-electron': isElectron }">
      <TitleBar v-if="isElectron" />
      <div class="app-container">
      <aside class="sidebar">
        <div class="logo">
          <MailinLogo :size="40" logo-class="logo-icon" />
          <div class="logo-text-group">
            <span class="logo-text">麦林</span>
            <span class="logo-subtitle">Mailin</span>
          </div>
        </div>
        <Menu
          mode="inline"
          :selected-keys="[route.name as string]"
          class="sidebar-menu"
        >
          <Menu.Item key="chat">
            <RouterLink to="/">
              <MessageOutlined />
              <span>聊天</span>
            </RouterLink>
          </Menu.Item>
          <Menu.Item key="sessions">
            <RouterLink to="/sessions">
              <HistoryOutlined />
              <span>会话</span>
            </RouterLink>
          </Menu.Item>
          <Menu.Item key="memory">
            <RouterLink to="/memory">
              <BookOutlined />
              <span>记忆</span>
            </RouterLink>
          </Menu.Item>
          <Menu.Item key="config">
            <RouterLink to="/config">
              <SettingOutlined />
              <span>配置</span>
            </RouterLink>
          </Menu.Item>
        </Menu>
        <div class="sidebar-footer">
          <span class="footer-text">麦田 · 智林</span>
        </div>
      </aside>

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

.sidebar {
  width: 220px;
  background: linear-gradient(180deg, var(--color-sidebar) 0%, #f3efe4 100%);
  border-right: 1px solid var(--color-border);
  box-shadow: var(--shadow-sidebar);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}

.logo {
  padding: 20px 18px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--color-border);
}

.logo-icon {
  border-radius: 10px;
  box-shadow: var(--shadow-soft);
}

.logo-text-group {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.logo-text {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text);
  letter-spacing: 2px;
}

.logo-subtitle {
  font-size: 11px;
  color: var(--color-text-secondary);
  letter-spacing: 1px;
  text-transform: uppercase;
}

.sidebar-menu {
  flex: 1;
  border-right: none;
  padding-top: 8px;
  background: transparent;
}

.sidebar-menu :deep(.ant-menu-item) {
  margin: 4px 8px;
  border-radius: 8px;
  width: calc(100% - 16px);
}

.sidebar-menu :deep(.ant-menu-item-selected) {
  font-weight: 500;
}

.sidebar-footer {
  padding: 16px 18px;
  border-top: 1px solid var(--color-border);
}

.footer-text {
  font-size: 11px;
  color: var(--color-text-secondary);
  letter-spacing: 1px;
}

.main-content {
  flex: 1;
  background-color: var(--color-background);
  overflow: auto;
  min-height: 0;
}
</style>
