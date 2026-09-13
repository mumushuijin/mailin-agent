<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button, Card, Empty, Input, List, Radio, Space, Switch, Tag, message } from 'ant-design-vue'
import {
  AppstoreOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons-vue'
import { skillsApi, type SkillDetail, type SkillEntry, type SkillSource } from '@/api/skills'
import { useProjectNavigator } from '@/composables/useProjectNavigator'

type SourceFilter = 'all' | SkillSource

const skills = ref<SkillEntry[]>([])
const selectedSkill = ref<SkillEntry | null>(null)
const selectedDetail = ref<SkillDetail | null>(null)
const sourceFilter = ref<SourceFilter>('all')
const searchText = ref('')
const loading = ref(false)
const refreshing = ref(false)
const detailLoading = ref(false)
const toggling = ref<string | null>(null)
const { currentProjectPath } = useProjectNavigator()

const selectedKey = computed(() => selectedSkill.value ? `${selectedSkill.value.source}:${selectedSkill.value.id}` : '')
const projectHint = computed(() => currentProjectPath.value || '未选择项目，当前仅显示全局 skill')

const filteredSkills = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return skills.value.filter((skill) => {
    if (sourceFilter.value !== 'all' && skill.source !== sourceFilter.value) {
      return false
    }
    if (!keyword) return true
    const blob = [
      skill.id,
      skill.name,
      skill.description,
      skill.summary,
      skill.source,
      skill.health,
      ...skill.tags,
      ...skill.triggers,
    ].join(' ').toLowerCase()
    return blob.includes(keyword)
  })
})

const counts = computed(() => {
  return {
    total: skills.value.length,
    global: skills.value.filter((skill) => skill.source === 'global').length,
    project: skills.value.filter((skill) => skill.source === 'project').length,
    errors: skills.value.filter((skill) => skill.health === 'error').length,
  }
})

const healthColor = (health: SkillEntry['health']) => {
  if (health === 'ok') return 'success'
  if (health === 'disabled') return 'default'
  if (health === 'shadowed') return 'warning'
  return 'error'
}

const healthText = (health: SkillEntry['health']) => {
  const labels: Record<SkillEntry['health'], string> = {
    ok: '可用',
    disabled: '已禁用',
    error: '错误',
    shadowed: '已覆盖',
  }
  return labels[health]
}

const sourceColor = (source: SkillSource) => source === 'project' ? 'blue' : 'green'
const sourceText = (source: SkillSource) => source === 'project' ? '项目' : '全局'

const loadSkills = async () => {
  loading.value = true
  try {
    const res = await skillsApi.list(null, currentProjectPath.value)
    skills.value = res.skills
    if (selectedSkill.value) {
      const stillThere = res.skills.find((skill) => (
        skill.id === selectedSkill.value?.id && skill.source === selectedSkill.value?.source
      ))
      selectedSkill.value = stillThere ?? null
      if (!stillThere) selectedDetail.value = null
    }
  } catch (error) {
    message.error('加载技能列表失败')
  } finally {
    loading.value = false
  }
}

const refreshSkills = async () => {
  refreshing.value = true
  try {
    const res = await skillsApi.refresh(currentProjectPath.value)
    message.success(`已刷新 ${res.total} 个技能`)
    await loadSkills()
  } catch (error) {
    message.error('刷新技能失败')
  } finally {
    refreshing.value = false
  }
}

const selectSkill = async (skill: SkillEntry) => {
  selectedSkill.value = skill
  selectedDetail.value = null
  detailLoading.value = true
  try {
    selectedDetail.value = await skillsApi.get(skill.id, skill.source, false, currentProjectPath.value)
  } catch (error) {
    message.error('加载技能详情失败')
  } finally {
    detailLoading.value = false
  }
}

const toggleSkill = async (skill: SkillEntry, checked: boolean) => {
  toggling.value = `${skill.source}:${skill.id}`
  try {
    const res = checked
      ? await skillsApi.enable(skill.id, skill.source, currentProjectPath.value)
      : await skillsApi.disable(skill.id, skill.source, currentProjectPath.value)
    message.success(checked ? '已启用技能' : '已禁用技能')
    await loadSkills()
    if (selectedSkill.value?.id === skill.id && selectedSkill.value.source === skill.source) {
      selectedSkill.value = res.skill
      selectedDetail.value = await skillsApi.get(skill.id, skill.source, false, currentProjectPath.value)
    }
  } catch (error: any) {
    const errorMsg = error?.response?.data?.detail || '更新技能状态失败'
    message.error(errorMsg)
  } finally {
    toggling.value = null
  }
}

const toggleSelectedSkill = (checked: boolean | string | number) => {
  if (!selectedSkill.value) return
  toggleSkill(selectedSkill.value, Boolean(checked))
}

onMounted(() => {
  loadSkills()
})
</script>

<template>
  <div class="skills-view">
    <div class="skill-header">
      <h1>技能管理</h1>
      <p>管理全局与项目级 skill，默认只注入轻量目录，按需读取完整说明</p>
    </div>

    <div class="skill-toolbar">
      <Radio.Group v-model:value="sourceFilter" button-style="solid">
        <Radio.Button value="all">全部 {{ counts.total }}</Radio.Button>
        <Radio.Button value="global">全局 {{ counts.global }}</Radio.Button>
        <Radio.Button value="project">项目 {{ counts.project }}</Radio.Button>
      </Radio.Group>
      <Input
        v-model:value="searchText"
        class="search-input"
        allow-clear
        placeholder="搜索技能名称、描述或标签"
      >
        <template #prefix>
          <SearchOutlined />
        </template>
      </Input>
      <Button :loading="refreshing" @click="refreshSkills">
        <ReloadOutlined /> 刷新
      </Button>
      <span class="project-hint">{{ projectHint }}</span>
    </div>

    <div class="skill-content">
      <div class="skill-list">
        <Card :loading="loading" class="list-card">
          <template #title>
            <AppstoreOutlined /> 技能目录
          </template>
          <template #extra>
            <Tag v-if="counts.errors" color="error">{{ counts.errors }} 个异常</Tag>
          </template>
          <List :data-source="filteredSkills" :locale="{ emptyText: '暂无技能' }">
            <template #renderItem="{ item }">
              <List.Item
                :class="['skill-item', { active: selectedKey === `${item.source}:${item.id}` }]"
                @click="selectSkill(item)"
              >
                <div class="skill-item-content">
                  <div class="skill-title-row">
                    <span class="skill-name">{{ item.id }}</span>
                    <Tag :color="sourceColor(item.source)" size="small">{{ sourceText(item.source) }}</Tag>
                  </div>
                  <div class="skill-summary">{{ item.summary || item.description || '无描述' }}</div>
                  <div class="skill-meta-row">
                    <Tag :color="healthColor(item.health)" size="small">{{ healthText(item.health) }}</Tag>
                    <Tag v-if="item.version" color="default" size="small">v{{ item.version }}</Tag>
                    <Tag v-if="item.shadowed_by" color="warning" size="small">被 {{ item.shadowed_by }} 覆盖</Tag>
                  </div>
                </div>
              </List.Item>
            </template>
          </List>
        </Card>
      </div>

      <div class="skill-detail">
        <Card v-if="selectedSkill" class="detail-card" :loading="detailLoading">
          <template #title>
            <Space>
              <span>{{ selectedSkill.id }}</span>
              <Tag :color="sourceColor(selectedSkill.source)">{{ sourceText(selectedSkill.source) }}</Tag>
              <Tag :color="healthColor(selectedSkill.health)">{{ healthText(selectedSkill.health) }}</Tag>
            </Space>
          </template>
          <template #extra>
            <Space>
              <span class="switch-label">{{ selectedSkill.enabled ? '启用' : '禁用' }}</span>
              <Switch
                :checked="selectedSkill.enabled"
                :loading="toggling === `${selectedSkill.source}:${selectedSkill.id}`"
                @change="toggleSelectedSkill"
              />
            </Space>
          </template>

          <div v-if="selectedDetail" class="detail-body">
            <section class="detail-section">
              <h2>摘要</h2>
              <p>{{ selectedDetail.description || selectedDetail.summary || '无描述' }}</p>
              <div class="tags-row">
                <Tag v-for="tag in selectedDetail.tags" :key="`tag-${tag}`" color="default">{{ tag }}</Tag>
                <Tag v-for="trigger in selectedDetail.triggers" :key="`trigger-${trigger}`" color="processing">
                  {{ trigger }}
                </Tag>
              </div>
            </section>

            <section class="detail-section">
              <h2>路径</h2>
              <code class="path-text">{{ selectedDetail.path }}</code>
            </section>

            <section v-if="selectedDetail.errors.length" class="detail-section">
              <h2>状态说明</h2>
              <div class="status-list">
                <div v-for="error in selectedDetail.errors" :key="error" class="status-line">
                  <CloseCircleOutlined /> {{ error }}
                </div>
              </div>
            </section>

            <section v-if="selectedDetail.content" class="detail-section">
              <h2>SKILL.md</h2>
              <pre class="skill-content-text">{{ selectedDetail.content }}</pre>
            </section>

            <section v-else class="detail-section empty-content">
              <CheckCircleOutlined v-if="selectedDetail.health === 'shadowed'" />
              <CloseCircleOutlined v-else />
              <span>当前状态下不会加载完整 SKILL.md</span>
            </section>
          </div>
        </Card>

        <Card v-else class="empty-card">
          <Empty description="请从左侧选择一个技能" :image-style="{ height: '80px' }" />
        </Card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.skills-view {
  min-height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 24px;
  box-sizing: border-box;
}

.skill-header {
  flex-shrink: 0;
  margin-bottom: 16px;
}

.skill-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
}

.skill-header p {
  margin: 0;
  color: var(--color-text-secondary);
}

.skill-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.search-input {
  width: 280px;
}

.project-hint {
  flex: 1;
  min-width: 220px;
  color: var(--color-text-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-content {
  display: flex;
  gap: 24px;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.skill-list {
  width: 340px;
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

.skill-item {
  cursor: pointer;
  padding: 14px 16px;
  transition: all 0.2s;
  border-bottom: 1px solid var(--color-border);
}

.skill-item:hover {
  background-color: var(--color-background-warm);
}

.skill-item.active {
  background-color: var(--color-selected-bg);
  border-left: 3px solid var(--color-selected-border);
}

.skill-item-content {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.skill-title-row,
.skill-meta-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.skill-name {
  font-weight: 600;
  color: var(--color-text);
}

.skill-summary {
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.skill-detail {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.detail-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.detail-card :deep(.ant-card-head) {
  flex-shrink: 0;
}

.detail-card :deep(.ant-card-body) {
  flex: 1;
  overflow-y: auto;
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.detail-section h2 {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
}

.detail-section p {
  margin: 0;
  color: var(--color-text-secondary);
}

.tags-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.path-text {
  display: block;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-background-warm);
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-all;
}

.status-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.status-line {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-danger);
}

.skill-content-text {
  max-height: 520px;
  margin: 0;
  padding: 14px;
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: #fbfaf7;
  color: var(--color-text);
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
}

.empty-content {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text-secondary);
}

.switch-label {
  color: var(--color-text-secondary);
}

.empty-card {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
