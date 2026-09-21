<script setup lang="ts">
import { computed } from 'vue'
import { Modal, Button } from 'ant-design-vue'
import { formatToolArgs } from '@/utils/toolDisplay'
import type { PendingApproval } from '@/types/chat-ui'

const props = defineProps<{
  pending: PendingApproval | null
}>()

defineEmits<{
  decide: [decision: 'allow' | 'deny']
}>()

const preview = computed(() => props.pending?.preview || null)
const isShell = computed(() => props.pending?.tool === 'run_shell')
const isPolicyDenied = computed(() => props.pending?.reasonCode?.includes('deny') || false)

/** 不从 raw args 重建敏感值；优先展示后端已脱敏的 preview 字段 */
const commandSummary = computed(() => {
  const p = preview.value
  if (!p) return ''
  const raw = p.normalized_command || p.target_summary || ''
  if (raw.length > 240) return `${raw.slice(0, 240)}…`
  return raw
})

const redactedArgs = computed(() => {
  if (!props.pending) return ''
  // 若有结构化 preview，不再 dump 可能含 secret 的完整 args
  if (preview.value?.normalized_command || preview.value?.target_summary) {
    return ''
  }
  return formatToolArgs(props.pending.args)
})
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

    <div v-if="preview" class="approval-preview">
      <p v-if="preview.target_summary && !isShell">
        目标：{{ preview.target_summary }}
      </p>
      <p v-if="isShell && commandSummary" class="approval-cmd">
        命令：<code>{{ commandSummary }}</code>
      </p>
      <p v-if="preview.cwd">工作目录：{{ preview.cwd }}</p>
      <p v-if="preview.allowlist_matched != null">
        白名单：{{ preview.allowlist_matched ? '已匹配' : '未匹配' }}
      </p>
      <p v-if="preview.capabilities?.length">
        能力：{{ preview.capabilities.join(', ') }}
      </p>
      <p v-if="preview.timeout_seconds != null">超时：{{ preview.timeout_seconds }}s</p>
      <p v-if="preview.snapshot_status">快照：{{ preview.snapshot_status }}</p>
      <p v-if="preview.risk_reason" class="approval-risk">原因：{{ preview.risk_reason }}</p>
      <p v-if="preview.impact_summary">影响：{{ preview.impact_summary }}</p>
    </div>

    <pre v-if="redactedArgs" class="approval-args">{{ redactedArgs }}</pre>
    <p v-if="isPolicyDenied" class="approval-policy-denied">策略拒绝预览（不会执行）</p>

    <div class="approval-actions">
      <Button danger @click="$emit('decide', 'deny')">拒绝</Button>
      <Button type="primary" @click="$emit('decide', 'allow')">允许执行</Button>
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

.approval-preview {
  margin: 8px 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  font-size: 13px;
  line-height: 1.45;
}

.approval-preview p {
  margin: 0 0 6px;
}

.approval-preview p:last-child {
  margin-bottom: 0;
}

.approval-cmd code {
  font-size: 12px;
  word-break: break-all;
}

.approval-risk {
  color: var(--color-warning, #ad6800);
}

.approval-policy-denied {
  color: var(--color-danger, #cf1322);
  font-size: 12px;
}
</style>
