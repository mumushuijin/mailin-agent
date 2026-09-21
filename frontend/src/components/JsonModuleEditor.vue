<script setup lang="ts">
import { computed } from 'vue'
import { Button, Input, Space, Tag, Alert } from 'ant-design-vue'
import {
  FormatPainterOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SaveOutlined,
} from '@ant-design/icons-vue'
import type { ConfigFieldError } from '@/api/config'

const props = withDefaults(
  defineProps<{
    title: string
    subtitle?: string
    text: string
    loading?: boolean
    saving?: boolean
    dirty?: boolean
    revision?: number | null
    errors?: ConfigFieldError[]
    warnings?: string[]
    conflictMessage?: string | null
    disabled?: boolean
  }>(),
  {
    subtitle: '',
    loading: false,
    saving: false,
    dirty: false,
    revision: null,
    errors: () => [],
    warnings: () => [],
    conflictMessage: null,
    disabled: false,
  },
)

const emit = defineEmits<{
  'update:text': [value: string]
  save: []
  format: []
  restore: []
  reload: []
  merge: []
}>()

const hasErrors = computed(() => props.errors.length > 0)
</script>

<template>
  <div class="json-module-editor">
    <div class="editor-toolbar">
      <div class="editor-title-block">
        <div class="editor-title-row">
          <h2>{{ title }}</h2>
          <Tag v-if="dirty" color="orange">未保存</Tag>
          <Tag v-if="revision != null" color="default">revision {{ revision }}</Tag>
        </div>
        <p v-if="subtitle" class="editor-subtitle">{{ subtitle }}</p>
      </div>
      <Space wrap>
        <Button :disabled="disabled || loading" @click="emit('format')">
          <FormatPainterOutlined /> 格式化
        </Button>
        <Button :disabled="disabled || loading" @click="emit('restore')">
          <RollbackOutlined /> 恢复最近有效
        </Button>
        <Button :disabled="loading" @click="emit('reload')">
          <ReloadOutlined /> 重新加载
        </Button>
        <Button v-if="conflictMessage" @click="emit('merge')">合并服务端版本</Button>
        <Button
          type="primary"
          :loading="saving"
          :disabled="disabled || loading || hasErrors"
          @click="emit('save')"
        >
          <SaveOutlined /> 保存
        </Button>
      </Space>
    </div>

    <Alert
      v-if="conflictMessage"
      type="warning"
      show-icon
      class="editor-alert"
      :message="conflictMessage"
    />
    <Alert
      v-for="(warn, idx) in warnings"
      :key="`w-${idx}`"
      type="info"
      show-icon
      class="editor-alert"
      :message="warn"
    />
    <Alert
      v-for="(err, idx) in errors"
      :key="`e-${idx}`"
      type="error"
      show-icon
      class="editor-alert"
      :message="err.path ? `${err.path} — ${err.message}` : err.message"
    />

    <Input.TextArea
      class="editor-textarea"
      :value="text"
      :disabled="disabled || loading"
      :auto-size="{ minRows: 18, maxRows: 36 }"
      spellcheck="false"
      @update:value="emit('update:text', $event)"
    />
  </div>
</template>

<style scoped>
.json-module-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

.editor-toolbar {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.editor-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.editor-title-row h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

.editor-subtitle {
  margin: 4px 0 0;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.editor-alert {
  margin: 0;
}

.editor-textarea {
  flex: 1;
  width: 100%;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 13px;
  line-height: 1.6;
  resize: none;
}
</style>
