<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Card, List, Input, Button, message, Empty, Tag, Modal, Checkbox } from 'ant-design-vue'
import {
  configApi,
  MARKDOWN_CONFIG_NAMES,
  type ConfigFile,
  type MarkdownConfigName,
} from '@/api/config'
import { SaveOutlined, FileTextOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { useUnsavedDraftGuard } from '@/composables/useUnsavedDraftGuard'

const router = useRouter()

const configs = ref<MarkdownConfigName[]>([...MARKDOWN_CONFIG_NAMES])
const selectedConfig = ref<ConfigFile | null>(null)
const editingContent = ref('')
const savedContent = ref('')
const loading = ref(false)
const saving = ref(false)
const resetting = ref(false)
const showResetModal = ref(false)
const resetOptions = ref({
  reset_sessions: true,
  reset_memory: true,
  reset_global_config: false,
})

const dirty = computed(
  () => !!selectedConfig.value && editingContent.value !== savedContent.value,
)
useUnsavedDraftGuard(dirty, '设定文件有未保存的修改，确定离开吗？')

const configDescriptions: Record<string, string> = {
  USER: '用户信息',
  SOUL: '人格模板',
  MEMORY: '长期记忆',
  HEARTBEAT: '心跳任务',
}

const loadConfigs = async () => {
  loading.value = true
  try {
    const res = await configApi.list()
    const markdown = res.configs.filter((name): name is MarkdownConfigName =>
      (MARKDOWN_CONFIG_NAMES as readonly string[]).includes(name),
    )
    configs.value = markdown.length ? markdown : [...MARKDOWN_CONFIG_NAMES]
  } catch {
    message.error('加载设定列表失败')
  } finally {
    loading.value = false
  }
}

const selectConfig = async (name: string) => {
  if (dirty.value) {
    const ok = await new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: '未保存的草稿',
        content: '当前设定文件有未保存修改，切换将丢失本地草稿。',
        okText: '切换',
        cancelText: '取消',
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      })
    })
    if (!ok) return
  }
  try {
    const res = await configApi.get(name)
    selectedConfig.value = res
    editingContent.value = res.content
    savedContent.value = res.content
  } catch {
    message.error('加载设定失败')
  }
}

const saveConfig = async () => {
  if (!selectedConfig.value) return
  saving.value = true
  try {
    await configApi.update(selectedConfig.value.name, editingContent.value)
    savedContent.value = editingContent.value
    message.success('保存成功')
  } catch (error: any) {
    const errorMsg = error?.response?.data?.detail || error?.message || '保存失败'
    message.error(typeof errorMsg === 'string' ? errorMsg : '保存失败')
  } finally {
    saving.value = false
  }
}

const confirmReset = () => {
  resetOptions.value = {
    reset_sessions: true,
    reset_memory: true,
    reset_global_config: false,
  }
  showResetModal.value = true
}

const handleReset = async () => {
  resetting.value = true
  try {
    const res = await configApi.reset(resetOptions.value)
    message.success(res.message)
    showResetModal.value = false
    selectedConfig.value = null
    editingContent.value = ''
    savedContent.value = ''

    if (resetOptions.value.reset_sessions) {
      localStorage.removeItem('mailin.lastSessionId')
    }

    await loadConfigs()
    router.push({ name: 'chat', query: { refresh: Date.now().toString() } })
  } catch {
    message.error('重置失败')
  } finally {
    resetting.value = false
  }
}

onMounted(() => {
  loadConfigs()
})
</script>

<template>
  <div class="config-view markdown-settings-view">
    <div class="config-header">
      <h1>设定</h1>
      <p>编辑 Agent 人格与记忆相关的 Markdown 文件</p>
    </div>

    <div class="config-content">
      <div class="config-list">
        <Card :loading="loading" class="list-card">
          <template #title>
            <FileTextOutlined /> 设定文件
          </template>
          <template #extra>
            <button class="reset-btn" title="重置为初始模板" @click="confirmReset">
              <ReloadOutlined /> 初始化
            </button>
          </template>
          <List :data-source="configs" :locale="{ emptyText: '暂无设定文件' }">
            <template #renderItem="{ item }">
              <List.Item
                :class="['config-item', { active: selectedConfig?.name === item }]"
                @click="selectConfig(item)"
              >
                <div class="config-item-content">
                  <span class="config-name">{{ item }}</span>
                  <Tag color="blue" v-if="configDescriptions[item]">
                    {{ configDescriptions[item] }}
                  </Tag>
                </div>
              </List.Item>
            </template>
          </List>
        </Card>
      </div>

      <div class="config-editor">
        <Card v-if="selectedConfig" class="editor-card">
          <template #title>
            <span>{{ selectedConfig.name }}</span>
            <Tag color="green" style="margin-left: 8px">.md</Tag>
            <Tag v-if="dirty" color="orange" style="margin-left: 8px">未保存</Tag>
          </template>
          <template #extra>
            <Button type="primary" :loading="saving" @click="saveConfig">
              <SaveOutlined /> 保存
            </Button>
          </template>
          <Input.TextArea
            v-model:value="editingContent"
            :auto-size="{ minRows: 18, maxRows: 30 }"
            class="editor-textarea"
          />
        </Card>

        <Card v-else class="empty-card">
          <Empty description="请从左侧选择一个设定文件" :image-style="{ height: '80px' }" />
        </Card>
      </div>
    </div>

    <Modal
      v-model:open="showResetModal"
      title="确认初始化"
      :confirm-loading="resetting"
      ok-text="确认初始化"
      cancel-text="取消"
      ok-type="danger"
      @ok="handleReset"
    >
      <div class="reset-warning">
        <p style="color: var(--color-danger); font-weight: 500">⚠️ 警告：此操作不可撤销！</p>
        <p>初始化将把设定文件恢复为默认模板，包括：</p>
        <ul>
          <li>SOUL.md - 人格与身份</li>
          <li>USER.md - 用户信息</li>
          <li>MEMORY.md - 长期记忆</li>
          <li>HEARTBEAT.md - 心跳任务</li>
        </ul>

        <div class="reset-options">
          <p style="font-weight: 500; margin-bottom: 8px">额外清除选项：</p>
          <Checkbox v-model:checked="resetOptions.reset_sessions">清除所有会话历史</Checkbox>
          <Checkbox v-model:checked="resetOptions.reset_memory">清除每日记忆文件</Checkbox>
          <Checkbox v-model:checked="resetOptions.reset_global_config">
            重置全局配置（LLM、Agent 设置，以及 CONFIG.json 中的 mcp / mcp_servers）
          </Checkbox>
        </div>

        <p style="margin-top: 12px; color: var(--color-text-secondary); font-size: 13px">
          仅清除会话时不会改动 MCP 配置；勾选「重置全局配置」才会恢复默认 CONFIG（含 MCP）。
        </p>
        <p style="margin-top: 16px">您确定要继续吗？</p>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.config-view {
  min-height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 24px;
  box-sizing: border-box;
}

.config-header {
  flex-shrink: 0;
  margin-bottom: 24px;
}

.config-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
}

.config-header p {
  margin: 0;
  color: #999;
}

.config-content {
  display: flex;
  gap: 24px;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.config-list {
  width: 280px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}

.list-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.list-card :deep(.ant-card-body) {
  flex: 1;
  padding: 0;
  overflow-y: auto;
}

.config-item {
  cursor: pointer;
  padding: 12px 16px;
  transition: all 0.2s;
  border-bottom: 1px solid var(--color-border);
}

.config-item:hover {
  background-color: var(--color-background-warm);
}

.config-item.active {
  background-color: var(--color-selected-bg);
  border-left: 3px solid var(--color-selected-border);
}

.config-item-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.config-name {
  font-weight: 500;
}

.config-editor {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.editor-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.editor-card :deep(.ant-card-head) {
  flex-shrink: 0;
}

.editor-card :deep(.ant-card-body) {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.editor-textarea {
  flex: 1;
  width: 100%;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 13px;
  line-height: 1.6;
  resize: none;
}

.empty-card {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.reset-btn {
  padding: 4px 12px;
  font-size: 13px;
  border: none;
  border-radius: 6px;
  background: var(--color-danger);
  color: #fff;
  cursor: pointer;
  transition: all 0.2s ease;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.reset-btn:hover {
  background: var(--color-danger-hover);
}

.reset-warning {
  padding: 8px 0;
}

.reset-warning ul {
  margin: 12px 0;
  padding-left: 24px;
}

.reset-warning li {
  margin: 4px 0;
  color: #666;
}

.reset-options {
  margin-top: 16px;
  padding: 12px;
  background: #fafafa;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
</style>
