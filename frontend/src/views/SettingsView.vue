<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

const route = useRoute()
const activeTab = computed(() => {
  if (route.path.includes('/memory')) return 'memory'
  if (route.path.includes('/skills')) return 'skills'
  return 'config'
})
</script>

<template>
  <div class="settings-view">
    <header class="settings-header">
      <h1>设置</h1>
      <p>全局 · Agent 自有空间，不随项目工作区切换</p>
    </header>
    <nav class="settings-tabs" aria-label="设置分类">
      <RouterLink
        class="tab"
        :class="{ active: activeTab === 'config' }"
        to="/settings/config"
      >
        配置
      </RouterLink>
      <RouterLink
        class="tab"
        :class="{ active: activeTab === 'memory' }"
        to="/settings/memory"
      >
        记忆
      </RouterLink>
      <RouterLink
        class="tab"
        :class="{ active: activeTab === 'skills' }"
        to="/settings/skills"
      >
        技能
      </RouterLink>
    </nav>
    <div class="settings-body">
      <RouterView />
    </div>
  </div>
</template>

<style scoped>
.settings-view {
  min-height: 100%;
  display: flex;
  flex-direction: column;
}

.settings-header {
  padding: 24px 24px 0;
}

.settings-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
}

.settings-header p {
  margin: 0;
  color: var(--color-text-secondary);
}

.settings-tabs {
  display: flex;
  gap: 8px;
  padding: 16px 24px 0;
  border-bottom: 1px solid var(--color-border);
}

.tab {
  padding: 8px 12px 12px;
  color: var(--color-text-secondary);
  border-bottom: 2px solid transparent;
  transition: color 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.tab:hover,
.tab.active {
  color: var(--color-text);
}

.tab.active {
  border-bottom-color: var(--color-primary);
  font-weight: 600;
}

.tab:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.settings-body {
  flex: 1;
  min-height: 0;
}

.settings-body :deep(.config-header),
.settings-body :deep(.memory-header),
.settings-body :deep(.skill-header) {
  display: none;
}

.settings-body :deep(.config-view),
.settings-body :deep(.memory-view),
.settings-body :deep(.skills-view) {
  padding-top: 16px;
}
</style>
