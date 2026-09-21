<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Button,
  Card,
  Empty,
  Input,
  List,
  Modal,
  Select,
  Space,
  Switch,
  Tag,
  message,
} from 'ant-design-vue'
import {
  ApiOutlined,
  CopyOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons-vue'
import {
  mcpApi,
  type McpServerDetail,
  type McpServerJsonDraft,
  type McpServerListItem,
  type McpState,
} from '@/api/mcp'
import type { ConfigFieldError } from '@/api/config'
import {
  canSubmitDraft,
  createJsonDraft,
  draftClientErrors,
  formatJson,
  markDraftSaved,
  parseJsonText,
  restoreLastValid,
  updateDraftText,
  type JsonDraftState,
} from '@/config/jsonDraft'
import { clonePlainJson } from '@/config/clonePlain'
import {
  copyServerDraft,
  detailToServerDraft,
  draftToServerWrite,
  emptyServerDraft,
  guardMcpServerDraft,
} from '@/config/mcpDraft'
import { mapPathErrors } from '@/config/jsonDraft'
import JsonModuleEditor from '@/components/JsonModuleEditor.vue'
import { useUnsavedDraftGuard } from '@/composables/useUnsavedDraftGuard'

const servers = ref<McpServerListItem[]>([])
const selectedId = ref<string | null>(null)
const detail = ref<McpServerDetail | null>(null)
const baselineDraft = ref<McpServerJsonDraft | null>(null)
const loading = ref(false)
const detailLoading = ref(false)
const saving = ref(false)
const testing = ref(false)
const refreshing = ref(false)
const searchText = ref('')
const stateFilter = ref<'all' | McpState>('all')
const isCreating = ref(false)
const createId = ref('')
const legacyShadowed = ref(false)
const configRevision = ref(0)
const testResult = ref<string | null>(null)
const draft = ref<JsonDraftState>(createJsonDraft(emptyServerDraft()))
const serverErrors = ref<ConfigFieldError[]>([])

const dirty = computed(() => draft.value.dirty)
useUnsavedDraftGuard(dirty, 'MCP Server 有未保存的草稿，确定离开吗？')

const filteredServers = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return servers.value.filter((item) => {
    if (stateFilter.value !== 'all' && item.state !== stateFilter.value) return false
    if (!keyword) return true
    const blob = [item.id, item.display_name, item.transport, item.state, item.error_summary || '']
      .join(' ')
      .toLowerCase()
    return blob.includes(keyword)
  })
})

const editorErrors = computed(() => [
  ...draftClientErrors(draft.value),
  ...serverErrors.value,
])

const editorTitle = computed(() => {
  if (isCreating.value) return '新增 MCP Server'
  return selectedId.value || 'MCP Server'
})

const stateColor = (state: McpState) => {
  const map: Record<McpState, string> = {
    ready: 'success',
    degraded: 'warning',
    stale: 'warning',
    connecting: 'processing',
    disabled: 'default',
    not_configured: 'default',
    error: 'error',
  }
  return map[state]
}

const stateText = (state: McpState) => {
  const map: Record<McpState, string> = {
    ready: '就绪',
    degraded: '降级',
    stale: '过期',
    connecting: '连接中',
    disabled: '已禁用',
    not_configured: '未配置',
    error: '错误',
  }
  return map[state]
}

const applyDraftValue = (value: McpServerJsonDraft) => {
  const plain = clonePlainJson(value)
  baselineDraft.value = plain
  draft.value = createJsonDraft(plain as unknown as Record<string, unknown>)
  serverErrors.value = []
  testResult.value = null
}

const confirmLeaveDraft = async (): Promise<boolean> => {
  if (!dirty.value) return true
  return new Promise((resolve) => {
    Modal.confirm({
      title: '未保存的草稿',
      content: '当前 MCP 草稿未保存，切换将丢失本地修改（不会写入配置模块）。',
      okText: '切换',
      cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

const loadList = async () => {
  loading.value = true
  try {
    const res = await mcpApi.list()
    servers.value = res.servers
    configRevision.value = res.config_revision
    legacyShadowed.value = res.legacy_shadowed
    if (selectedId.value && !res.servers.some((s) => s.id === selectedId.value)) {
      selectedId.value = null
      detail.value = null
      isCreating.value = false
    }
  } catch {
    message.error('加载 MCP 列表失败')
  } finally {
    loading.value = false
  }
}

const selectServer = async (item: McpServerListItem) => {
  if (!(await confirmLeaveDraft())) return
  selectedId.value = item.id
  isCreating.value = false
  createId.value = ''
  detailLoading.value = true
  testResult.value = null
  try {
    detail.value = await mcpApi.get(item.id)
    applyDraftValue(detailToServerDraft(detail.value))
  } catch {
    message.error('加载 MCP 详情失败')
  } finally {
    detailLoading.value = false
  }
}

const startCreate = async () => {
  if (!(await confirmLeaveDraft())) return
  selectedId.value = null
  detail.value = null
  isCreating.value = true
  createId.value = ''
  applyDraftValue(emptyServerDraft())
}

const startCopy = async () => {
  if (!detail.value) return
  if (!(await confirmLeaveDraft())) return
  const source = detailToServerDraft(detail.value)
  const copied = copyServerDraft(source)
  copied.display_name = `${detail.value.display_name} 副本`
  isCreating.value = true
  selectedId.value = null
  createId.value = `${detail.value.id}_copy`
  applyDraftValue(copied)
}

const onTextChange = (text: string) => {
  serverErrors.value = []
  let next = updateDraftText(draft.value, text)
  const parsed = parseJsonText(text)
  if (parsed.ok) {
    const guarded = guardMcpServerDraft(parsed.value)
    if (!guarded.ok) {
      next = {
        ...next,
        typeErrors: mapPathErrors(guarded.errors),
      }
    }
  }
  draft.value = next
}

const formatDraft = () => {
  const parsed = parseJsonText(draft.value.text)
  if (!parsed.ok) {
    message.error(parsed.error.message)
    return
  }
  onTextChange(formatJson(parsed.value))
}

const restoreDraft = () => {
  draft.value = restoreLastValid(draft.value)
  serverErrors.value = []
  message.success('已恢复最近有效版本')
}

const reloadSelected = async () => {
  if (isCreating.value || !selectedId.value) {
    message.info('新建草稿请直接编辑或清空后重新添加')
    return
  }
  try {
    detail.value = await mcpApi.get(selectedId.value)
    if (dirty.value) {
      Modal.confirm({
        title: '重新加载',
        content: '用服务端版本覆盖本地 MCP 草稿？',
        okText: '覆盖',
        cancelText: '取消',
        onOk: () => applyDraftValue(detailToServerDraft(detail.value!)),
      })
    } else {
      applyDraftValue(detailToServerDraft(detail.value))
      message.success('已重新加载')
    }
  } catch {
    message.error('重新加载失败')
  }
}

const parseCurrentDraft = (): McpServerJsonDraft | null => {
  if (!canSubmitDraft(draft.value)) return null
  const parsed = parseJsonText(draft.value.text)
  if (!parsed.ok) return null
  const guarded = guardMcpServerDraft(parsed.value)
  if (!guarded.ok) {
    serverErrors.value = mapPathErrors(guarded.errors)
    return null
  }
  return guarded.value
}

const saveServer = async () => {
  const id = (isCreating.value ? createId.value : selectedId.value || '').trim()
  if (!id) {
    message.error('请填写稳定 Server ID')
    return
  }
  const current = parseCurrentDraft()
  if (!current) {
    message.error('请先修复 JSON 错误')
    return
  }
  saving.value = true
  try {
    const body = draftToServerWrite(current, isCreating.value ? null : baselineDraft.value)
    const res = await mcpApi.upsert(id, body)
    message.success('已保存 MCP Server')
    configRevision.value = res.config_revision
    isCreating.value = false
    selectedId.value = id
    createId.value = ''
    await loadList()
    detail.value = res.server
    applyDraftValue(detailToServerDraft(res.server))
    draft.value = markDraftSaved(
      draft.value,
      detailToServerDraft(res.server) as unknown as Record<string, unknown>,
    )
  } catch (error: any) {
    const detailBody = error?.response?.data?.detail
    const validation =
      detailBody?.validation_errors?.join('; ') ||
      error?.response?.data?.validation_errors?.join('; ')
    const errors = error?.response?.data?.errors
    if (Array.isArray(errors)) {
      serverErrors.value = mapPathErrors(errors)
    }
    message.error(validation || (typeof detailBody === 'string' ? detailBody : null) || '保存失败')
  } finally {
    saving.value = false
  }
}

const toggleEnabled = async (checked: boolean | string | number) => {
  const enabled = Boolean(checked)
  if (isCreating.value || !selectedId.value) {
    const current = parseCurrentDraft() || emptyServerDraft()
    current.enabled = enabled
    onTextChange(formatJson(current))
    return
  }
  try {
    const res = enabled
      ? await mcpApi.enable(selectedId.value)
      : await mcpApi.disable(selectedId.value)
    message.success(enabled ? '已启用' : '已禁用')
    detail.value = res.server
    applyDraftValue(detailToServerDraft(res.server))
    await loadList()
  } catch (error: any) {
    message.error(error?.response?.data?.detail || '更新启用状态失败')
  }
}

const removeServer = () => {
  if (!selectedId.value) return
  Modal.confirm({
    title: '删除 MCP Server',
    content: `确认删除 ${selectedId.value}？将关闭连接并移除其工具。`,
    okType: 'danger',
    onOk: async () => {
      await mcpApi.remove(selectedId.value!)
      message.success('已删除')
      selectedId.value = null
      detail.value = null
      isCreating.value = false
      draft.value = createJsonDraft(emptyServerDraft())
      await loadList()
    },
  })
}

const testConnection = async () => {
  const current = parseCurrentDraft()
  if (!current) {
    message.error('请先修复 JSON 错误再测试')
    return
  }
  testing.value = true
  testResult.value = null
  try {
    const body = {
      ...draftToServerWrite(current, isCreating.value ? null : baselineDraft.value),
      id: (isCreating.value ? createId.value : selectedId.value) || 'draft',
    }
    // draft test 不落盘
    const res = await mcpApi.test(body)
    testResult.value = res.ok
      ? `成功（${res.tool_count} 个工具，${Math.round(res.duration_ms)}ms）`
      : `失败：${res.error || res.error_code || res.state}`
    if (res.ok) message.success('连接测试成功（未保存）')
    else message.warning('连接测试失败')
  } catch (error: any) {
    testResult.value = error?.response?.data?.detail || '测试失败'
    message.error('连接测试失败')
  } finally {
    testing.value = false
  }
}

const refreshConnections = async () => {
  refreshing.value = true
  try {
    const res = await mcpApi.refresh()
    message.success(`已刷新 ${res.results.length} 个 Server`)
    configRevision.value = res.config_revision
    await loadList()
    if (selectedId.value && !isCreating.value) {
      detail.value = await mcpApi.get(selectedId.value)
      if (!dirty.value) {
        applyDraftValue(detailToServerDraft(detail.value))
      }
    }
  } catch {
    message.error('刷新连接失败')
  } finally {
    refreshing.value = false
  }
}

const enabledChecked = computed(() => {
  const parsed = parseJsonText(draft.value.text)
  if (parsed.ok && typeof parsed.value === 'object' && parsed.value && 'enabled' in (parsed.value as object)) {
    return Boolean((parsed.value as { enabled?: boolean }).enabled)
  }
  return true
})

onMounted(() => {
  loadList()
})
</script>

<template>
  <div class="mcp-view">
    <div class="mcp-header">
      <h1>MCP 管理</h1>
      <p>管理外部 MCP Server：一 Server 一 JSON、启停、测试连接与刷新工具目录</p>
    </div>

    <div class="mcp-toolbar">
      <Select v-model:value="stateFilter" style="width: 140px">
        <Select.Option value="all">全部状态</Select.Option>
        <Select.Option value="ready">就绪</Select.Option>
        <Select.Option value="stale">过期</Select.Option>
        <Select.Option value="error">错误</Select.Option>
        <Select.Option value="disabled">已禁用</Select.Option>
        <Select.Option value="connecting">连接中</Select.Option>
        <Select.Option value="degraded">降级</Select.Option>
        <Select.Option value="not_configured">未配置</Select.Option>
      </Select>
      <Input
        v-model:value="searchText"
        class="search-input"
        allow-clear
        placeholder="搜索 id、名称或传输类型"
      >
        <template #prefix>
          <SearchOutlined />
        </template>
      </Input>
      <Button type="primary" @click="startCreate">
        <PlusOutlined /> 添加
      </Button>
      <Button :loading="refreshing" @click="refreshConnections">
        <ReloadOutlined /> 刷新连接
      </Button>
      <span class="revision-hint">revision {{ configRevision }}</span>
    </div>

    <div v-if="legacyShadowed" class="legacy-banner">
      检测到同时存在规范 `mcp` 与旧 `mcp_servers` 配置；当前以 `mcp` 为准，旧段未被合并。
    </div>

    <div class="mcp-content">
      <div class="mcp-list">
        <Card :loading="loading" class="list-card">
          <template #title>
            <ApiOutlined /> MCP Server
          </template>
          <Empty
            v-if="!loading && filteredServers.length === 0"
            description="暂无 MCP Server"
          >
            <Button type="primary" @click="startCreate">添加 Server</Button>
          </Empty>
          <List v-else :data-source="filteredServers">
            <template #renderItem="{ item }">
              <List.Item
                :class="['mcp-item', { active: selectedId === item.id && !isCreating }]"
                @click="selectServer(item)"
              >
                <div class="mcp-item-content">
                  <div class="mcp-title-row">
                    <span class="mcp-name">{{ item.display_name || item.id }}</span>
                    <Tag :color="stateColor(item.state)" size="small">{{ stateText(item.state) }}</Tag>
                  </div>
                  <div class="mcp-summary">{{ item.id }} · {{ item.transport }} · {{ item.tool_count }} 工具</div>
                  <div v-if="item.error_summary" class="mcp-error">{{ item.error_summary }}</div>
                </div>
              </List.Item>
            </template>
          </List>
        </Card>
      </div>

      <div class="mcp-detail">
        <Card v-if="isCreating || detail" class="detail-card" :loading="detailLoading">
          <div class="detail-meta">
            <Space wrap>
              <template v-if="isCreating">
                <span class="meta-label">稳定 ID</span>
                <Input
                  v-model:value="createId"
                  style="width: 220px"
                  placeholder="例如 linear"
                />
              </template>
              <Tag v-if="detail && !isCreating" :color="stateColor(detail.state)">
                {{ stateText(detail.state) }}
              </Tag>
              <span class="switch-label">{{ enabledChecked ? '启用' : '禁用' }}</span>
              <Switch :checked="enabledChecked" @change="toggleEnabled" />
            </Space>
            <Space wrap>
              <Button :loading="testing" @click="testConnection">测试连接</Button>
              <Button v-if="detail && !isCreating" @click="startCopy">
                <CopyOutlined /> 复制
              </Button>
              <Button v-if="detail && !isCreating" danger @click="removeServer">
                <DeleteOutlined /> 删除
              </Button>
            </Space>
          </div>

          <p class="secret-hint">
            敏感字段显示为 <code>********</code>；原样保留表示不修改，删除键并保存即清除（clear_*）。
            测试连接不会保存草稿。
          </p>
          <div v-if="testResult" class="test-result">{{ testResult }}</div>

          <JsonModuleEditor
            :title="editorTitle"
            subtitle="单个 Server 的规范 JSON 草稿"
            :text="draft.text"
            :loading="detailLoading"
            :saving="saving"
            :dirty="dirty"
            :revision="configRevision"
            :errors="editorErrors"
            @update:text="onTextChange"
            @save="saveServer"
            @format="formatDraft"
            @restore="restoreDraft"
            @reload="reloadSelected"
          />
        </Card>
        <Card v-else class="detail-card">
          <Empty description="选择左侧 Server，或点击添加" />
        </Card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mcp-view {
  min-height: 100%;
  padding: 0 24px 24px;
}

.mcp-header h1 {
  margin: 0 0 8px;
  font-size: 22px;
}

.mcp-header p {
  margin: 0;
  color: var(--color-text-secondary);
}

.mcp-toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 16px 0;
  flex-wrap: wrap;
}

.search-input {
  width: 280px;
}

.revision-hint {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.legacy-banner {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  background: color-mix(in srgb, var(--color-warning, #d48806) 12%, transparent);
  border-radius: 8px;
}

.mcp-content {
  display: grid;
  grid-template-columns: minmax(280px, 360px) 1fr;
  gap: 16px;
  min-height: 520px;
}

.list-card,
.detail-card {
  height: 100%;
}

.detail-card :deep(.ant-card-body) {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-meta {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}

.meta-label {
  color: var(--color-text-secondary);
}

.secret-hint {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.mcp-item {
  cursor: pointer;
  border-radius: 8px;
  padding: 8px !important;
}

.mcp-item.active,
.mcp-item:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
}

.mcp-title-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: center;
}

.mcp-name {
  font-weight: 600;
}

.mcp-summary {
  margin-top: 4px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.mcp-error {
  margin-top: 4px;
  color: var(--color-danger, #cf1322);
  font-size: 12px;
}

.switch-label {
  color: var(--color-text-secondary);
}

.test-result {
  padding: 8px 10px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
}

@media (max-width: 960px) {
  .mcp-content {
    grid-template-columns: 1fr;
  }
}
</style>
