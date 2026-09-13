import { computed, ref } from 'vue'
import { sessionApi, type Session } from '@/api/session'
import { getLastWorkspacePath, saveLastWorkspacePath } from '@/composables/useWorkspaceFolder'
import {
  groupSessionsByProject,
  normalizeProjectKey,
  UNBOUND_GROUP_KEY,
  type ProjectGroup,
} from '@/utils/projectGroups'

const SESSION_STORAGE_KEY = 'mailin.lastSessionId'

const sessions = ref<Session[]>([])
const listLoading = ref(false)
const currentSessionId = ref<string | null>(null)
const currentProjectPath = ref<string | null>(getLastWorkspacePath() || null)
const expandedKeys = ref<Set<string>>(new Set())

export function getLastSessionId(): string | null {
  return localStorage.getItem(SESSION_STORAGE_KEY)
}

export function saveLastSessionId(id: string) {
  localStorage.setItem(SESSION_STORAGE_KEY, id)
}

const projectGroups = computed(() => groupSessionsByProject(sessions.value))

const currentProjectKey = computed(() => normalizeProjectKey(currentProjectPath.value))

const currentGroup = computed<ProjectGroup | null>(() => {
  const key = currentProjectKey.value
  if (!key) return null
  return projectGroups.value.find((group) => group.key === key) || null
})

function expandKey(key: string) {
  const next = new Set(expandedKeys.value)
  next.add(key)
  expandedKeys.value = next
}

function setCurrentProject(path: string | null) {
  currentProjectPath.value = path
  if (path) {
    saveLastWorkspacePath(path)
    const key = normalizeProjectKey(path)
    if (key) expandKey(key)
  }
}

function setCurrentSession(session: Session | null) {
  currentSessionId.value = session?.id || null
  if (session?.id) {
    saveLastSessionId(session.id)
  }
  if (session?.workspace_path) {
    setCurrentProject(session.workspace_path)
  } else if (session && !session.workspace_path) {
    currentProjectPath.value = null
    expandKey(UNBOUND_GROUP_KEY)
  }
}

async function refreshSessions() {
  listLoading.value = true
  try {
    const res = await sessionApi.list()
    sessions.value = res.sessions.map((item) => ({
      ...item,
      title: item.title?.trim() || '新会话',
    }))
    const keys = new Set(expandedKeys.value)
    for (const group of groupSessionsByProject(res.sessions)) {
      keys.add(group.key)
    }
    expandedKeys.value = keys
  } finally {
    listLoading.value = false
  }
}

export function useProjectNavigator() {
  return {
    sessions,
    listLoading,
    currentSessionId,
    currentProjectPath,
    currentProjectKey,
    currentGroup,
    expandedKeys,
    projectGroups,
    refreshSessions,
    setCurrentProject,
    setCurrentSession,
    expandKey,
    toggleExpanded(key: string) {
      const next = new Set(expandedKeys.value)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      expandedKeys.value = next
    },
  }
}
