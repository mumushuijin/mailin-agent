<script setup lang="ts">
import { Modal, Button } from 'ant-design-vue'
import { formatToolArgs } from '@/utils/toolDisplay'
import type { PendingApproval } from '@/types/chat-ui'

defineProps<{
  pending: PendingApproval | null
}>()

const emit = defineEmits<{
  decide: [decision: 'allow' | 'deny']
}>()
</script>

<template>
  <Modal
    :open="!!pending"
    title="工具执行确认"
    :closable="false"
    :mask-closable="false"
    :footer="null"
    centered
  >
    <p v-if="pending">{{ pending.reason }}</p>
    <p v-if="pending" class="approval-tool-name">
      工具：<strong>{{ pending.tool }}</strong>
    </p>
    <pre v-if="pending && Object.keys(pending.args).length" class="approval-args">{{ formatToolArgs(pending.args) }}</pre>
    <div class="approval-actions">
      <Button danger @click="emit('decide', 'deny')">拒绝</Button>
      <Button type="primary" @click="emit('decide', 'allow')">允许执行</Button>
    </div>
  </Modal>
</template>

<style scoped>
.approval-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.approval-args {
  background: var(--color-surface);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
  max-height: 120px;
  overflow: auto;
  border: 1px solid var(--color-border);
}

.approval-tool-name {
  margin: 8px 0;
}
</style>
