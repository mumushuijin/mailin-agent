<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Card, List, Empty, Tag, Modal, message } from 'ant-design-vue'
import { SettingOutlined } from '@ant-design/icons-vue'
import {
  configApi,
  type ConfigFieldError,
  type ConfigModuleSummary,
} from '@/api/config'
import { clonePlainJson } from '@/config/clonePlain'
import { isUserVisibleModuleKey } from '@/contracts/configModules'
import {
  canSubmitDraft,
  createJsonDraft,
  draftClientErrors,
  formatJson,
  markDraftSaved,
  parseJsonText,
  reloadDraftFromServer,
  restoreLastValid,
  updateDraftText,
  type JsonDraftState,
} from '@/config/jsonDraft'
import { parseConfigApiError } from '@/config/apiErrors'
import JsonModuleEditor from '@/components/JsonModuleEditor.vue'
import { useUnsavedDraftGuard } from '@/composables/useUnsavedDraftGuard'

const modules = ref<ConfigModuleSummary[]>([])
const revision = ref(0)
const globalWarnings = ref<string[]>([])
const selectedKey = ref<string | null>(null)
const loading = ref(false)
const saving = ref(false)
const draft = ref<JsonDraftState>(createJsonDraft({}))
const serverErrors = ref<ConfigFieldError[]>([])
const conflictMessage = ref<string | null>(null)
const conflictCurrent = ref<Record<string, unknown> | null>(null)

const dirty = computed(() => draft.value.dirty)
useUnsavedDraftGuard(dirty, '配置模块有未保存的草稿，确定离开吗？')

const selectedModule = computed(
  () => modules.value.find((m) => m.key === selectedKey.value) || null,
)

const editorErrors = computed(() => [
  ...draftClientErrors(draft.value),
  ...serverErrors.value,
])

const loadModules = async (preferKey?: string | null) => {
  loading.value = true
  try {
    const res = await configApi.listModules()
    modules.value = res.modules.filter((m) => isUserVisibleModuleKey(m.key))
    revision.value = res.revision
    globalWarnings.value = res.warnings || []
    const nextKey =
      preferKey && modules.value.some((m) => m.key === preferKey)
        ? preferKey
        : selectedKey.value && modules.value.some((m) => m.key === selectedKey.value)
          ? selectedKey.value
          : modules.value[0]?.key || null
    if (nextKey) {
      await selectModule(nextKey, { force: true, keepDraft: false })
    } else {
      selectedKey.value = null
    }
  } catch (error) {
    message.error(parseConfigApiError(error).message || '加载配置模块失败')
  } finally {
    loading.value = false
  }
}

const applyModuleValue = (mod: ConfigModuleSummary) => {
  const plain = clonePlainJson(mod.value || {})
  draft.value = createJsonDraft(plain)
  serverErrors.value = [...(mod.errors || [])]
  conflictMessage.value = null
  conflictCurrent.value = null
}

const selectModule = async (
  key: string,
  options: { force?: boolean; keepDraft?: boolean } = {},
) => {
  if (!options.force && key === selectedKey.value) return
  if (!options.force && dirty.value) {
    const ok = await new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: '未保存的草稿',
        content: '当前模块有未保存草稿，切换将丢失本地修改（不会提交到其他模块）。',
        okText: '切换',
        cancelText: '取消',
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      })
    })
    if (!ok) return
  }
  const mod = modules.value.find((m) => m.key === key)
  if (!mod) return
  selectedKey.value = key
  if (options.keepDraft && dirty.value) {
    draft.value = reloadDraftFromServer(draft.value, mod.value || {}, true)
  } else {
    applyModuleValue(mod)
  }
}

const onTextChange = (text: string) => {
  const key = selectedKey.value
  serverErrors.value = []
  conflictMessage.value = null
  if (key && isUserVisibleModuleKey(key)) {
    draft.value = updateDraftText(draft.value, text, key)
  } else {
    draft.value = updateDraftText(draft.value, text)
  }
}

const formatDraft = () => {
  const parsed = parseJsonText(draft.value.text)
  if (!parsed.ok) {
    message.error(parsed.error.message)
    return
  }
  draft.value = updateDraftText(
    draft.value,
    formatJson(parsed.value),
    selectedKey.value && isUserVisibleModuleKey(selectedKey.value)
      ? selectedKey.value
      : undefined,
  )
}

const restoreDraft = () => {
  draft.value = restoreLastValid(draft.value)
  serverErrors.value = []
  conflictMessage.value = null
  message.success('已恢复最近有效版本')
}

const reloadSelected = async () => {
  if (!selectedKey.value) return
  try {
    const res = await configApi.listModules()
    modules.value = res.modules.filter((m) => isUserVisibleModuleKey(m.key))
    revision.value = res.revision
    const mod = modules.value.find((m) => m.key === selectedKey.value)
    if (!mod) {
      message.warning('模块已不存在')
      return
    }
    if (dirty.value) {
      Modal.confirm({
        title: '重新加载',
        content: '用服务端版本覆盖本地草稿，还是仅刷新基线并保留草稿？',
        okText: '覆盖本地',
        cancelText: '保留草稿',
        onOk: () => applyModuleValue(mod),
        onCancel: () => {
          draft.value = reloadDraftFromServer(draft.value, mod.value || {}, true)
          message.info('已刷新服务端基线，本地草稿仍保留')
        },
      })
    } else {
      applyModuleValue(mod)
      message.success('已重新加载')
    }
  } catch (error) {
    message.error(parseConfigApiError(error).message)
  }
}

const mergeConflict = () => {
  if (!conflictCurrent.value) {
    message.warning('没有可合并的服务端内容')
    return
  }
  // 保留本地草稿文本，只更新 lastValid 基线与 revision 感知
  draft.value = reloadDraftFromServer(draft.value, conflictCurrent.value, true)
  conflictMessage.value =
    '已载入服务端最新版本作为合并基线。请检查本地草稿后再次保存（将使用最新 revision）。'
  conflictCurrent.value = null
}

const saveModule = async () => {
  const key = selectedKey.value
  if (!key) return
  if (!canSubmitDraft(draft.value)) {
    message.error('请先修复 JSON 语法或类型错误')
    return
  }
  const parsed = parseJsonText(draft.value.text)
  if (!parsed.ok || typeof parsed.value !== 'object' || parsed.value === null || Array.isArray(parsed.value)) {
    message.error('模块值必须是 JSON 对象')
    return
  }

  saving.value = true
  serverErrors.value = []
  conflictMessage.value = null
  try {
    // 只提交当前目标模块
    const res = await configApi.updateModule(key, {
      base_revision: revision.value,
      value: parsed.value as Record<string, unknown>,
    })
    revision.value = res.revision
    draft.value = markDraftSaved(draft.value, res.value)
    const listing = await configApi.listModules()
    modules.value = listing.modules.filter((m) => isUserVisibleModuleKey(m.key))
    revision.value = listing.revision
    message.success(`已保存模块 ${key}`)
  } catch (error) {
    const parsedErr = parseConfigApiError(error)
    if (parsedErr.kind === 'conflict') {
      conflictMessage.value = parsedErr.message
      conflictCurrent.value = parsedErr.current ?? null
      if (typeof parsedErr.revision === 'number') {
        revision.value = parsedErr.revision
      }
      message.warning('版本冲突：本地草稿已保留，请重新加载或合并后保存')
    } else if (parsedErr.kind === 'validation') {
      serverErrors.value = parsedErr.errors
      message.error(parsedErr.message)
    } else {
      message.error(parsedErr.message || '保存失败')
    }
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadModules()
})
</script>

<template>
  <div class="config-view config-modules-view">
    <div class="config-header">
      <h1>配置</h1>
      <p>编辑 Agent / 工具等用户配置（系统内部模块不在此展示）</p>
    </div>

    <div v-if="globalWarnings.length" class="global-warnings">
      <div v-for="(w, i) in globalWarnings" :key="i" class="warn-item">{{ w }}</div>
    </div>

    <div class="config-content">
      <div class="config-list">
        <Card :loading="loading" class="list-card">
          <template #title>
            <SettingOutlined /> 配置模块
          </template>
          <template #extra>
            <span class="revision-hint">revision {{ revision }}</span>
          </template>
          <Empty
            v-if="!loading && modules.length === 0"
            description="暂无配置模块"
          />
          <List v-else :data-source="modules">
            <template #renderItem="{ item }">
              <List.Item
                :class="['config-item', { active: selectedKey === item.key }]"
                @click="selectModule(item.key)"
              >
                <div class="config-item-content">
                  <div class="config-title-row">
                    <span class="config-name">{{ item.display_name || item.key }}</span>
                    <Tag v-if="item.has_draft_error" color="error" size="small">错误</Tag>
                    <Tag v-else-if="selectedKey === item.key && dirty" color="orange" size="small">
                      草稿
                    </Tag>
                  </div>
                  <div class="config-summary">{{ item.key }} · {{ item.source }}</div>
                </div>
              </List.Item>
            </template>
          </List>
        </Card>
      </div>

      <div class="config-editor">
        <Card v-if="selectedModule" class="editor-card">
          <JsonModuleEditor
            :title="selectedModule.display_name || selectedModule.key"
            :subtitle="`模块键 ${selectedModule.key}`"
            :text="draft.text"
            :loading="loading"
            :saving="saving"
            :dirty="dirty"
            :revision="revision"
            :errors="editorErrors"
            :warnings="selectedModule.warnings"
            :conflict-message="conflictMessage"
            @update:text="onTextChange"
            @save="saveModule"
            @format="formatDraft"
            @restore="restoreDraft"
            @reload="reloadSelected"
            @merge="mergeConflict"
          />
        </Card>
        <Card v-else class="empty-card">
          <Empty description="请从左侧选择一个配置模块" :image-style="{ height: '80px' }" />
        </Card>
      </div>
    </div>
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
  margin-bottom: 16px;
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

.global-warnings {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  background: color-mix(in srgb, var(--color-warning, #d48806) 12%, transparent);
  border-radius: 8px;
}

.warn-item {
  font-size: 13px;
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

.revision-hint {
  color: var(--color-text-secondary);
  font-size: 12px;
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
  width: 100%;
}

.config-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.config-name {
  font-weight: 500;
}

.config-summary {
  color: var(--color-text-secondary);
  font-size: 12px;
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

.editor-card :deep(.ant-card-body) {
  flex: 1;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

.empty-card {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
