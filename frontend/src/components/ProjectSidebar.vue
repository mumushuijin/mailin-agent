<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, Dropdown, Input, Menu, MenuItem, Modal, message } from 'ant-design-vue'
import {
  DeleteOutlined,
  EditOutlined,
  FolderOpenOutlined,
  MoreOutlined,
  PlusOutlined,
  SettingOutlined,
} from '@ant-design/icons-vue'
import MailinLogo from '@/components/MailinLogo.vue'
import { sessionApi, type Session } from '@/api/session'
import { useProjectNavigator } from '@/composables/useProjectNavigator'
import { pickDirectory } from '@/composables/useWorkspaceFolder'
import {
  normalizeProjectKey,
  UNBOUND_GROUP_KEY,
  type ProjectGroup,
} from '@/utils/projectGroups'

const route = useRoute()
const router = useRouter()
const {
  projectGroups,
  listLoading,
  currentSessionId,
  currentProjectKey,
  expandedKeys,
  currentProjectPath,
  refreshSessions,
  setCurrentProject,
  setCurrentSession,
  toggleExpanded,
  expandKey,
} = useProjectNavigator()

const creating = ref(false)
const renameOpen = ref(false)
const renameDraft = ref('')
const renameTarget = ref<Session | null>(null)
const renaming = ref(false)

const isSettings = computed(() => route.path.startsWith('/settings'))

onMounted(() => {
  refreshSessions().catch(() => {
    message.error('加载会话列表失败')
  })
})

const openSession = async (session: Session) => {
  setCurrentSession(session)
  await router.push({ name: 'chat', query: { session: session.id } })
}

const selectProject = async (path: string | null, key: string) => {
  expandKey(key)
  if (path) {
    setCurrentProject(path)
  }
  const group = projectGroups.value.find((item) => item.key === key)
  const latest = group?.sessions[0]
  if (latest) {
    await openSession(latest)
  } else {
    await router.push({ name: 'chat' })
  }
}

const createBoundSession = async (path: string) => {
  creating.value = true
  try {
    const res = await sessionApi.create(path)
    await refreshSessions()
    const created = await sessionApi.get(res.session_id)
    setCurrentSession(created)
    await router.push({ name: 'chat', query: { session: res.session_id } })
  } catch (error: unknown) {
    const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail || '创建会话失败')
  } finally {
    creating.value = false
  }
}

const newChat = async () => {
  const path = currentProjectPath.value?.trim()
  if (path) {
    await createBoundSession(path)
    return
  }
  const selected = await pickDirectory()
  if (!selected) return
  await createBoundSession(selected)
}

const addProject = async () => {
  const selected = await pickDirectory(currentProjectPath.value || '')
  if (!selected) return
  await createBoundSession(selected)
}

const confirmRename = async () => {
  if (!renameTarget.value) return
  const title = renameDraft.value.trim()
  if (!title) {
    message.warning('请输入会话标题')
    return
  }
  renaming.value = true
  try {
    const updated = await sessionApi.rename(renameTarget.value.id, title)
    await refreshSessions()
    if (currentSessionId.value === updated.id) {
      setCurrentSession(updated)
    }
    renameOpen.value = false
  } catch (error: unknown) {
    const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail || '重命名失败')
  } finally {
    renaming.value = false
  }
}

const startRename = (session: Session) => {
  renameTarget.value = session
  renameDraft.value = session.title
  renameOpen.value = true
}

const deleteSession = (session: Session) => {
  Modal.confirm({
    title: '删除会话',
    content: `确定删除「${session.title}」？此操作不能撤销。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      await sessionApi.delete(session.id)
      const wasCurrent = currentSessionId.value === session.id
      await refreshSessions()
      if (!wasCurrent) return
      const groupKey = normalizeProjectKey(session.workspace_path) ?? UNBOUND_GROUP_KEY
      const group = projectGroups.value.find((item) => item.key === groupKey)
      const next = group?.sessions[0]
      if (next) {
        await openSession(next)
      } else {
        setCurrentSession(null)
        await router.push({ name: 'chat' })
      }
    },
  })
}

const popupContainer = () => document.body

const clearGroupSessions = (group: ProjectGroup) => {
  if (!group.sessions.length) return
  const count = group.sessions.length
  Modal.confirm({
    title: '清空该项目的会话',
    content: group.path
      ? `将删除「${group.label}」下的 ${count} 个会话，不会删除文件夹：${group.path}`
      : `将删除未绑定组中的 ${count} 个会话，不会删除任何项目文件夹。`,
    okText: '清空会话',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      const ids = group.sessions.map((item) => item.id)
      const wasCurrent = ids.includes(currentSessionId.value || '')
      for (const id of ids) {
        await sessionApi.delete(id)
      }
      await refreshSessions()
      if (!wasCurrent) return
      setCurrentSession(null)
      await router.push({ name: 'chat' })
    },
  })
}
</script>

<template>
  <aside class="sidebar">
    <div class="logo">
      <MailinLogo :size="36" logo-class="logo-icon" />
      <div class="logo-text-group">
        <span class="logo-text">麦林</span>
        <span class="logo-subtitle">项目与会话</span>
      </div>
    </div>

    <div class="sidebar-actions">
      <Button type="primary" block :loading="creating" @click="newChat">
        <PlusOutlined />
        新对话
      </Button>
      <Button block :loading="creating" @click="addProject">
        <FolderOpenOutlined />
        添加项目
      </Button>
    </div>

    <div class="project-list" :class="{ loading: listLoading }">
      <div v-if="!listLoading && projectGroups.length === 0" class="empty-nav">
        <p>还没有项目工作区</p>
        <p class="empty-hint">添加一个本机文件夹，会话会出现在这里。</p>
      </div>

      <section
        v-for="group in projectGroups"
        :key="group.key"
        class="project-group"
        :class="{
          active: group.key === currentProjectKey,
          unbound: group.key === UNBOUND_GROUP_KEY,
        }"
      >
        <div class="project-row">
          <button
            class="project-header"
            type="button"
            :title="group.path || '未绑定项目文件夹'"
            @click="selectProject(group.path, group.key)"
          >
            <span
              class="chevron"
              :class="{ open: expandedKeys.has(group.key) }"
              @click.stop="toggleExpanded(group.key)"
            >▸</span>
            <span class="project-name">{{ group.label }}</span>
            <span class="session-count">{{ group.sessions.length }}</span>
          </button>
          <Dropdown
            :trigger="['click']"
            placement="bottomRight"
            overlay-class-name="sidebar-dropdown"
            :get-popup-container="popupContainer"
          >
            <button class="row-more" type="button" title="项目操作">
              <MoreOutlined />
            </button>
            <template #overlay>
              <Menu>
                <MenuItem :disabled="group.sessions.length === 0" @click="() => clearGroupSessions(group)">
                  <DeleteOutlined />
                  清空该项目的会话
                </MenuItem>
              </Menu>
            </template>
          </Dropdown>
        </div>
        <ul v-show="expandedKeys.has(group.key)" class="session-list">
          <li
            v-for="session in group.sessions"
            :key="session.id"
            :class="['session-row', { active: session.id === currentSessionId }]"
          >
            <Dropdown
              :trigger="['contextmenu']"
              overlay-class-name="sidebar-dropdown"
              :get-popup-container="popupContainer"
            >
              <button
                class="session-main"
                type="button"
                :title="session.title"
                @click="openSession(session)"
                @dblclick.stop="startRename(session)"
              >
                {{ session.title }}
              </button>
              <template #overlay>
                <Menu>
                  <MenuItem @click="() => startRename(session)">
                    <EditOutlined />
                    重命名
                  </MenuItem>
                  <MenuItem @click="() => deleteSession(session)">
                    <DeleteOutlined />
                    删除
                  </MenuItem>
                </Menu>
              </template>
            </Dropdown>
            <Dropdown
              :trigger="['click']"
              placement="bottomRight"
              overlay-class-name="sidebar-dropdown"
              :get-popup-container="popupContainer"
            >
              <button class="row-more" type="button" title="更多">
                <MoreOutlined />
              </button>
              <template #overlay>
                <Menu>
                  <MenuItem @click="() => startRename(session)">
                    <EditOutlined />
                    重命名
                  </MenuItem>
                  <MenuItem @click="() => deleteSession(session)">
                    <DeleteOutlined />
                    删除
                  </MenuItem>
                </Menu>
              </template>
            </Dropdown>
          </li>
        </ul>
      </section>
    </div>

    <div class="sidebar-footer">
      <RouterLink
        class="settings-link"
        :class="{ active: isSettings }"
        to="/settings"
      >
        <SettingOutlined />
        设置
      </RouterLink>
    </div>

    <Modal
      v-model:open="renameOpen"
      title="重命名会话"
      ok-text="保存更改"
      cancel-text="取消"
      :confirm-loading="renaming"
      @ok="confirmRename"
    >
      <Input
        v-model:value="renameDraft"
        placeholder="会话标题"
        @press-enter="confirmRename"
      />
    </Modal>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 272px;
  background: linear-gradient(180deg, var(--color-sidebar) 0%, #f3efe4 100%);
  border-right: 1px solid var(--color-border);
  box-shadow: var(--shadow-sidebar);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.logo {
  padding: 16px 16px 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo-icon {
  border-radius: 10px;
  box-shadow: var(--shadow-soft);
}

.logo-text-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
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
  letter-spacing: 0.4px;
}

.sidebar-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 16px 12px;
}

.project-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 8px 8px;
}

.project-list.loading {
  opacity: 0.7;
}

.empty-nav {
  padding: 24px 12px;
  text-align: center;
  color: var(--color-text);
}

.empty-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.project-group {
  margin-bottom: 4px;
}

.project-row {
  display: flex;
  align-items: center;
  min-width: 0;
  border-radius: 8px;
}

.project-row:hover,
.project-group.active > .project-row {
  background: var(--color-selected-bg);
}

.project-header {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
  border: none;
  background: transparent;
  border-radius: 8px;
  color: var(--color-text);
  cursor: pointer;
  text-align: left;
  transition: background-color 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.project-header:hover {
  background: transparent;
}

.project-group.active > .project-header {
  background: transparent;
}

.project-header:focus-visible,
.session-main:focus-visible,
.row-more:focus-visible,
.settings-link:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.chevron {
  display: inline-flex;
  width: 16px;
  color: var(--color-text-secondary);
  transform: rotate(0deg);
  transition: transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.chevron.open {
  transform: rotate(90deg);
}

.project-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-count {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.session-list {
  list-style: none;
  margin: 0 0 4px;
  padding: 0 0 0 16px;
}

.session-row {
  display: flex;
  align-items: center;
  min-width: 0;
  border-radius: 8px;
  transition: background-color 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.session-row:hover {
  background: rgba(44, 62, 45, 0.04);
}

.session-row.active {
  background: var(--color-selected-bg);
}

.session-main {
  flex: 1;
  min-width: 0;
  padding: 8px;
  border: none;
  background: transparent;
  color: var(--color-text);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-more {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  margin-right: 4px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  border-radius: 6px;
  cursor: pointer;
}

.row-more:hover {
  background: rgba(44, 62, 45, 0.08);
  color: var(--color-text);
}

.sidebar-footer {
  padding: 12px 16px 16px;
  border-top: 1px solid var(--color-border);
}

.settings-link {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 8px;
  color: var(--color-text);
  font-size: 13px;
  transition: background-color 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.settings-link:hover,
.settings-link.active {
  background: var(--color-selected-bg);
}
</style>

<style>
.sidebar-dropdown {
  z-index: 1200;
  min-width: 160px;
}
.sidebar-dropdown .ant-dropdown-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
