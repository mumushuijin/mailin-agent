import { ref, onMounted, onUnmounted } from 'vue'
import { chatWs } from '@/api/ws'

export type MaintenanceStatus = 'started' | 'done' | 'failed' | 'skipped'

export interface MaintenanceNotice {
  id: string
  kind: string
  status: MaintenanceStatus
  message: string
  success: boolean
  taskId?: string
  createdAt: number
}

const notices = ref<MaintenanceNotice[]>([])
const dismissTimers = new Map<string, ReturnType<typeof setTimeout>>()
const staleTimers = new Map<string, ReturnType<typeof setTimeout>>()

/** started 超过此时间未收到终态则标记失败，避免 UI 永久 loading */
const STALE_STARTED_MS = 35_000
const TERMINAL_DISMISS_MS = 6000

const KIND_LABELS: Record<string, string> = {
  sediment: '记忆沉淀',
  memory_nudge: '会话记忆',
  mcp_health: 'MCP 服务',
  memory: '记忆整理',
  config: '配置',
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? '后台维护'
}

function scheduleDismiss(id: string, ms = TERMINAL_DISMISS_MS) {
  const existing = dismissTimers.get(id)
  if (existing) clearTimeout(existing)
  dismissTimers.set(
    id,
    setTimeout(() => {
      notices.value = notices.value.filter((n) => n.id !== id)
      dismissTimers.delete(id)
      staleTimers.delete(id)
    }, ms),
  )
}

function clearStaleWatch(id: string) {
  const timer = staleTimers.get(id)
  if (timer) {
    clearTimeout(timer)
    staleTimers.delete(id)
  }
}

function scheduleStaleWatch(id: string) {
  clearStaleWatch(id)
  staleTimers.set(
    id,
    setTimeout(() => {
      const idx = notices.value.findIndex((n) => n.id === id)
      if (idx < 0) return
      const notice = notices.value[idx]
      if (notice?.status !== 'started') return
      notices.value[idx] = {
        ...notice,
        status: 'failed',
        success: false,
        message: `${notice.message}（超时未完成）`,
      }
      scheduleDismiss(id)
    }, STALE_STARTED_MS),
  )
}

function handleMaintenance(data: {
  kind: string
  status?: MaintenanceStatus
  message: string
  success?: boolean
  task_id?: string
}) {
  const status = data.status ?? 'done'
  if (status === 'skipped') return

  const taskId = data.task_id
  const existingIdx = taskId
    ? notices.value.findIndex((n) => n.taskId === taskId)
    : -1

  const notice: MaintenanceNotice = {
    id: taskId ?? `${data.kind}-${Date.now()}`,
    kind: data.kind,
    status,
    message: data.message,
    success: data.success ?? true,
    taskId,
    createdAt: Date.now(),
  }

  if (existingIdx >= 0) {
    notices.value[existingIdx] = notice
  } else {
    notices.value.push(notice)
    if (notices.value.length > 5) {
      notices.value.shift()
    }
  }

  if (status === 'started') {
    scheduleStaleWatch(notice.id)
  } else {
    clearStaleWatch(notice.id)
    if (status === 'done' || status === 'failed') {
      scheduleDismiss(notice.id)
    }
  }
}

let unsub: (() => void) | null = null
let connected = false

export function useMaintenance() {
  onMounted(() => {
    if (connected) return
    connected = true
    chatWs.connect().catch(() => {})
    unsub = chatWs.onBackground((data) => {
      handleMaintenance(data as Parameters<typeof handleMaintenance>[0])
    })
  })

  onUnmounted(() => {
    unsub?.()
    unsub = null
    connected = false
    for (const timer of dismissTimers.values()) clearTimeout(timer)
    dismissTimers.clear()
    for (const timer of staleTimers.values()) clearTimeout(timer)
    staleTimers.clear()
  })

  const dismiss = (id: string) => {
    notices.value = notices.value.filter((n) => n.id !== id)
    const timer = dismissTimers.get(id)
    if (timer) {
      clearTimeout(timer)
      dismissTimers.delete(id)
    }
    clearStaleWatch(id)
  }

  return { notices, kindLabel, dismiss }
}

/** 供单元测试使用 */
export function __resetMaintenanceForTests() {
  notices.value = []
  for (const timer of dismissTimers.values()) clearTimeout(timer)
  dismissTimers.clear()
  for (const timer of staleTimers.values()) clearTimeout(timer)
  staleTimers.clear()
}

export function __handleMaintenanceForTests(
  data: Parameters<typeof handleMaintenance>[0],
) {
  handleMaintenance(data)
}
