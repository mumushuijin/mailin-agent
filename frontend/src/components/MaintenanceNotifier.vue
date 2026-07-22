<script setup lang="ts">
import { LoadingOutlined, CheckCircleOutlined, CloseCircleOutlined, ToolOutlined } from '@ant-design/icons-vue'
import { useMaintenance } from '@/composables/useMaintenance'

const { notices, kindLabel, dismiss } = useMaintenance()

const iconFor = (notice: { status: string; success: boolean }) => {
  if (notice.status === 'started') return LoadingOutlined
  if (!notice.success) return CloseCircleOutlined
  return CheckCircleOutlined
}
</script>

<template>
  <div v-if="notices.length" class="maintenance-stack">
    <div
      v-for="notice in notices"
      :key="notice.id"
      :class="['maintenance-item', notice.status, { failed: !notice.success }]"
      @click="dismiss(notice.id)"
    >
      <component :is="iconFor(notice)" class="maintenance-icon" />
      <div class="maintenance-body">
        <span class="maintenance-kind">{{ kindLabel(notice.kind) }}</span>
        <span class="maintenance-message">{{ notice.message }}</span>
      </div>
      <ToolOutlined class="maintenance-hint" />
    </div>
  </div>
</template>

<style scoped>
.maintenance-stack {
  position: fixed;
  top: 12px;
  right: 16px;
  z-index: 1100;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 360px;
  pointer-events: none;
}

.maintenance-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  box-shadow: var(--shadow-soft);
  cursor: pointer;
  pointer-events: auto;
  animation: slide-in 0.25s ease;
}

.maintenance-item.started {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.maintenance-item.failed {
  border-color: var(--color-danger, #ff4d4f);
  background: var(--color-danger-bg, #fff2f0);
}

.maintenance-icon {
  margin-top: 2px;
  color: var(--color-primary);
  flex-shrink: 0;
}

.maintenance-item.failed .maintenance-icon {
  color: var(--color-danger, #ff4d4f);
}

.maintenance-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.maintenance-kind {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  letter-spacing: 0.5px;
}

.maintenance-message {
  font-size: 13px;
  color: var(--color-text);
  line-height: 1.4;
}

.maintenance-hint {
  opacity: 0.3;
  font-size: 12px;
  flex-shrink: 0;
}

@keyframes slide-in {
  from {
    opacity: 0;
    transform: translateX(12px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}
</style>
