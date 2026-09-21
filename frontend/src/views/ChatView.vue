<script setup lang="ts">
import { ref, watch, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { Input, Button, message, Tag } from 'ant-design-vue'
import { SendOutlined, PlusOutlined, LoadingOutlined, FolderOpenOutlined } from '@ant-design/icons-vue'
import { useRouter, useRoute } from 'vue-router'
import { sessionApi, type ChatMessage as ApiChatMessage, type Session } from '@/api/session'
import { chatApi, type ApiUsage, type ContextUsage } from '@/api/chat'
import { chatWs } from '@/api/ws'
import { configApi } from '@/api/config'
import { formatMessageTime } from '@/utils/markdown'
import {
  getToolActionDisplay,
  getToolConfig,
  formatToolArgs,
  formatToolResult,
  toolErrorPreview,
  toolResultLooksLikeError,
  toolResultPreview,
} from '@/utils/toolDisplay'
import MailinLogo from '@/components/MailinLogo.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import ToolApprovalModal from '@/components/ToolApprovalModal.vue'
import AskUserModal from '@/components/AskUserModal.vue'
import ContextUsageRing from '@/components/ContextUsageRing.vue'
import { createChatStreamEventBatcher } from '@/composables/useChatStream'
import { getLastWorkspacePath, openDirectory, pickDirectory, saveLastWorkspacePath } from '@/composables/useWorkspaceFolder'
import { getLastSessionId, saveLastSessionId, useProjectNavigator } from '@/composables/useProjectNavigator'
import type {
  ChatUiMessage,
  MessageSegment,
  PendingApproval,
  PendingAskUser,
  RunProgressSummary,
  SessionTodoItem,
  ToolSegment,
} from '@/types/chat-ui'

const { refreshSessions, setCurrentSession, currentProjectPath } = useProjectNavigator()

const assistantName = ref('麦林')

type Message = ChatUiMessage

interface MessageGroup {
  role: 'user' | 'assistant'
  messages: Message[]
}

const router = useRouter()
const route = useRoute()
const inputMessage = ref('')
const messages = ref<Message[]>([])
const loading = ref(false)
const currentSessionId = ref<string | null>(null)
const messagesContainer = ref<HTMLElement | null>(null)
const abortController = ref<AbortController | null>(null)
const initializing = ref(true)
const historyLoading = ref(false)
const expandedTools = ref<Set<number>>(new Set())
const contextUsage = ref<ContextUsage | null>(null)
const apiUsage = ref<ApiUsage | null>(null)
const contextCompressing = ref(false)
const activeStage = ref<string | null>(null)
const activeRunId = ref<string | null>(null)
const historyNextCursor = ref<string | null>(null)
const hasMoreHistory = ref(false)
const wsConnectionState = ref<'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected'>('idle')
const runStartedAt = ref<number | null>(null)
const runClockNow = ref(Date.now())
const lastRunSummary = ref<RunProgressSummary | null>(null)
const terminalRunStatus = ref<'done' | 'cancelled' | 'error' | 'handoff' | null>(null)
let runTimer: ReturnType<typeof setInterval> | null = null

// 工具审批（WebSocket interrupt）
const pendingApproval = ref<PendingApproval | null>(null)
const pendingAskUser = ref<PendingAskUser | null>(null)
const sessionTodos = ref<SessionTodoItem[]>([])
const boundWorkspacePath = ref<string | null>(null)
const folderDraft = ref(getLastWorkspacePath())
const bindingFolder = ref(false)

let unsubConfig: (() => void) | null = null
let unsubConnection: (() => void) | null = null

// 窗口化渲染：初始只渲染最近 RENDER_WINDOW 条消息，向上滚动时分批补载，
// 避免一次性把全部历史塞进 DOM 导致打开会话卡顿。
const RENDER_WINDOW = 30
const RENDER_BATCH = 30
const renderLimit = ref(RENDER_WINDOW)
const loadingMore = ref(false)

// 当前实际参与渲染的消息（最近 renderLimit 条）
const visibleMessages = computed(() => {
  const all = messages.value
  if (all.length <= renderLimit.value) return all
  return all.slice(all.length - renderLimit.value)
})

// 是否还有更早的消息未渲染
const hasMoreToRender = computed(() => messages.value.length > renderLimit.value || hasMoreHistory.value)

// 消息分组（Slack 风格）
const messageGroups = computed<MessageGroup[]>(() => {
  const groups: MessageGroup[] = []

  for (const msg of visibleMessages.value) {
    const lastGroup = groups[groups.length - 1]

    if (lastGroup && lastGroup.role === msg.role) {
      lastGroup.messages.push(msg)
    } else {
      groups.push({
        role: msg.role,
        messages: [msg]
      })
    }
  }

  return groups
})

// 是否应该显示加载指示器（底部的独立指示器）
// 仅当尚无助手消息组、或助手组不可见时才在底部显示
const shouldShowLoadingIndicator = computed(() => {
  if (!loading.value) return false

  const groups = messageGroups.value
  if (groups.length === 0) return true

  const lastIndex = groups.length - 1
  const lastGroup = groups[lastIndex]!
  if (shouldShowGroupThinking(lastGroup, lastIndex)) return false

  const lastMsg = messages.value[messages.value.length - 1]
  if (lastMsg?.role !== 'assistant') return true

  return !hasVisibleContent(lastMsg)
})

// 检查消息组是否有可见内容
const hasGroupVisibleContent = (group: MessageGroup): boolean => {
  for (const msg of group.messages) {
    if (hasVisibleContent(msg)) {
      return true
    }
  }
  return false
}

// 检查消息组是否有文本内容
const hasGroupTextContent = (group: MessageGroup): boolean => {
  for (const msg of group.messages) {
    if (hasTextContent(msg)) {
      return true
    }
  }
  return false
}

// 检查消息组是否有正在执行或已完成的工具（但还没有文本回复）
const hasGroupToolWithoutText = (group: MessageGroup): boolean => {
  if (group.role !== 'assistant') return false
  let hasTool = false
  let hasText = false
  for (const msg of group.messages) {
    if (!msg.segments) continue
    for (const segment of msg.segments) {
      if (segment.type === 'tool' && !getToolConfig(segment.tool).hidden) {
        hasTool = true
      }
      if (segment.type === 'text' && segment.content && segment.content.trim()) {
        hasText = true
      }
    }
  }
  return hasTool && !hasText
}

const isLastMessageGroup = (groupIndex: number) => groupIndex === messageGroups.value.length - 1

// 最后一组助手消息仍在生成中、尚无文本回复时显示思考提示
const shouldShowGroupThinking = (group: MessageGroup, groupIndex: number) => {
  if (!loading.value || group.role !== 'assistant') return false
  if (!isLastMessageGroup(groupIndex)) return false
  return !hasGroupTextContent(group)
}

const progressStageLabel = computed(() => {
  if (historyLoading.value) return '加载历史中…'
  if (wsConnectionState.value === 'connecting') return '连接中…'
  if (wsConnectionState.value === 'reconnecting') return '重连中…'
  if (contextCompressing.value) return '整理上下文中…'
  if (activeStage.value === 'history_load') return '加载历史中…'
  if (activeStage.value === 'connecting') return '连接中…'
  if (activeStage.value === 'accepted') return '已接收，准备中…'
  if (activeStage.value === 'context_prepare') return '准备上下文中…'
  if (activeStage.value === 'thinking') return 'Agent 正在处理…'
  if (activeStage.value === 'model_stream') return 'Agent 正在处理…'
  if (activeStage.value === 'tool_batch') return '执行工具中…'
  if (activeStage.value === 'usage_snapshot') return '更新用量中…'
  if (activeStage.value === 'waiting_user') return '等待确认中…'
  if (activeStage.value === 'postprocess') return '收尾处理中…'
  const groups = messageGroups.value
  const lastGroup = groups[groups.length - 1]
  if (lastGroup && hasGroupToolWithoutText(lastGroup)) {
    return '处理工具结果中…'
  }
  return loading.value ? 'Agent 正在处理…' : '准备开始…'
})

const thinkingLabel = computed(() => progressStageLabel.value)

const completedTodoCount = computed(() => (
  sessionTodos.value.filter((item) => item.status === 'completed').length
))

const formatElapsed = (elapsedMs: number): string => {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

const currentElapsedMs = computed(() => (
  runStartedAt.value ? Math.max(0, runClockNow.value - runStartedAt.value) : 0
))

const runProgressSummary = computed<RunProgressSummary | null>(() => {
  if (loading.value && runStartedAt.value) {
    return {
      visible: true,
      active: true,
      stageLabel: progressStageLabel.value,
      elapsedLabel: formatElapsed(currentElapsedMs.value),
      todoCompleted: completedTodoCount.value,
      todoTotal: sessionTodos.value.length,
    }
  }
  return lastRunSummary.value
})

const startRunClock = () => {
  if (runTimer) clearInterval(runTimer)
  runClockNow.value = Date.now()
  runTimer = setInterval(() => {
    runClockNow.value = Date.now()
  }, 1000)
}

const stopRunClock = () => {
  if (runTimer) {
    clearInterval(runTimer)
    runTimer = null
  }
}

const markRunTerminal = (status: 'done' | 'cancelled' | 'error' | 'handoff') => {
  terminalRunStatus.value = status
}

const finalizeRunSummary = (fallbackStatus: 'done' | 'cancelled' | 'error' | 'handoff' = 'done') => {
  const status = terminalRunStatus.value || fallbackStatus
  const elapsedMs = runStartedAt.value
    ? Math.max(0, Date.now() - runStartedAt.value)
    : currentElapsedMs.value

  const label =
    status === 'done'
      ? '本轮完成'
      : status === 'cancelled'
        ? '本轮已取消'
        : status === 'handoff'
          ? '已交还用户'
          : '本轮失败'
  lastRunSummary.value = {
    visible: true,
    active: false,
    stageLabel: label,
    elapsedLabel: formatElapsed(elapsedMs),
    todoCompleted: completedTodoCount.value,
    todoTotal: sessionTodos.value.length,
    terminalLabel:
      status === 'done'
        ? '已完成'
        : status === 'cancelled'
          ? '已取消'
          : status === 'handoff'
            ? '已交还'
            : '执行失败',
  }
  stopRunClock()
  runStartedAt.value = null
}

const saveCurrentSession = (sessionId: string) => {
  saveLastSessionId(sessionId)
}

const getLastSession = (): string | null => {
  const current = getLastSessionId()
  if (current) return current

  const legacy = localStorage.getItem('helloclaw.lastSessionId')
  if (legacy) {
    saveLastSessionId(legacy)
    localStorage.removeItem('helloclaw.lastSessionId')
    return legacy
  }

  return null
}

// 根据后端时间戳或会话时间范围估算消息时间（兼容旧数据）
const resolveMessageTimestamp = (
  backendTs: number | undefined,
  index: number,
  total: number,
  sessionCreatedAt: number,
  sessionUpdatedAt: number,
): Date => {
  if (backendTs) return new Date(backendTs * 1000)
  if (total <= 1) return new Date(sessionUpdatedAt * 1000)
  const start = sessionCreatedAt * 1000
  const end = sessionUpdatedAt * 1000
  const ratio = index / (total - 1)
  return new Date(start + (end - start) * ratio)
}

let historyLoadSeq = 0

interface ToolResultDisplay {
  content?: string
  ref?: string | null
  preview?: string | null
  truncated?: boolean
}

const safeJsonArgs = (raw: string): Record<string, unknown> => {
  try {
    const parsed = JSON.parse(raw || '{}')
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : {}
  } catch {
    return {}
  }
}

const applySessionMetadata = (sessionRes: Session) => {
  boundWorkspacePath.value = sessionRes.workspace_path || null
  if (sessionRes.workspace_path) {
    folderDraft.value = sessionRes.workspace_path
    saveLastWorkspacePath(sessionRes.workspace_path)
  }
  setCurrentSession(sessionRes)
}

const buildDisplayMessages = (
  rawMessages: ApiChatMessage[],
  sessionCreatedAt: number,
  sessionUpdatedAt: number,
): Message[] => {
  const now = Date.now()
  const toolResults: Map<string, ToolResultDisplay> = new Map()

  for (const msg of rawMessages) {
    if (msg.role === 'tool' && msg.tool_call_id) {
      toolResults.set(msg.tool_call_id, {
        content: msg.content,
        ref: msg.tool_result_ref,
        preview: msg.tool_result_preview,
        truncated: Boolean(msg.tool_result_truncated),
      })
    }
  }

  const displayMessages: Message[] = []
  let pendingAssistant: Message | null = null

  for (let i = 0; i < rawMessages.length; i++) {
    const msg = rawMessages[i]!

    if (msg.role === 'user') {
      if (pendingAssistant) {
        displayMessages.push(pendingAssistant)
        pendingAssistant = null
      }
      displayMessages.push({
        id: now + i,
        role: 'user',
        content: msg.content || '',
        timestamp: resolveMessageTimestamp(
          msg.timestamp,
          i,
          rawMessages.length,
          sessionCreatedAt,
          sessionUpdatedAt,
        ),
      })
    } else if (msg.role === 'assistant') {
      if (msg.tool_calls && msg.tool_calls.length > 0) {
        const segments: MessageSegment[] = []

        msg.tool_calls.forEach((tc, tcIndex) => {
          const result = toolResults.get(tc.id)
          segments.push({
            type: 'tool',
            id: now + i * 1000 + tcIndex,
            tool: tc.function.name,
            args: safeJsonArgs(tc.function.arguments),
            result: result?.content,
            toolResultRef: result?.ref,
            toolResultTruncated: result?.truncated,
            status: result?.content && toolResultLooksLikeError(result.content) ? 'error' : 'done',
          })
        })

        const nextMsg = rawMessages[i + 1]
        if (nextMsg && nextMsg.role === 'assistant' && !nextMsg.tool_calls && nextMsg.content) {
          segments.push({
            type: 'text',
            id: now + i * 1000 + 100,
            content: nextMsg.content
          })
          i++
        }

        pendingAssistant = {
          id: now + i,
          role: 'assistant',
          content: '',
          timestamp: resolveMessageTimestamp(
            msg.timestamp,
            i,
            rawMessages.length,
            sessionCreatedAt,
            sessionUpdatedAt,
          ),
          segments
        }
      } else if (msg.content) {
        if (pendingAssistant) {
          if (!pendingAssistant.segments) pendingAssistant.segments = []
          pendingAssistant.segments.push({
            type: 'text',
            id: now + i,
            content: msg.content
          })
        } else {
          displayMessages.push({
            id: now + i,
            role: 'assistant',
            content: msg.content,
            timestamp: resolveMessageTimestamp(
              msg.timestamp,
              i,
              rawMessages.length,
              sessionCreatedAt,
              sessionUpdatedAt,
            ),
          })
        }
      }
    }
  }

  if (pendingAssistant) displayMessages.push(pendingAssistant)
  return displayMessages
}

// 加载最近一页会话历史（按照 OpenAI 标准格式解析）
const loadSessionHistory = async (sessionId: string) => {
  const seq = ++historyLoadSeq
  historyLoading.value = true
  lastRunSummary.value = null
  terminalRunStatus.value = null
  stopRunClock()
  runStartedAt.value = null
  messages.value = []
  contextUsage.value = null
  apiUsage.value = null
  contextCompressing.value = false
  activeStage.value = 'history_load'
  activeRunId.value = null
  historyNextCursor.value = null
  hasMoreHistory.value = false
  renderLimit.value = RENDER_WINDOW
  try {
    const historyRes = await sessionApi.getHistoryPage(sessionId, { limit: RENDER_WINDOW })
    const sessionRes = historyRes.session
    const rawMessages = historyRes.messages
    sessionTodos.value = (historyRes.todos || []) as SessionTodoItem[]
    const sessionCreatedAt = sessionRes.created_at
    const sessionUpdatedAt = sessionRes.updated_at
    if (seq !== historyLoadSeq) return
    applySessionMetadata(sessionRes)
    messages.value = buildDisplayMessages(rawMessages, sessionCreatedAt, sessionUpdatedAt)
    historyNextCursor.value = historyRes.next_cursor || null
    hasMoreHistory.value = historyRes.has_more
    if (historyRes.context_usage) {
      contextUsage.value = historyRes.context_usage
    }
    if (historyRes.api_usage) {
      apiUsage.value = historyRes.api_usage
    }
  } catch (error) {
    if (seq !== historyLoadSeq) return
    // 会话不存在或加载失败，清空消息
    messages.value = []
    boundWorkspacePath.value = null
    setCurrentSession(null)
  } finally {
    if (seq === historyLoadSeq) {
      historyLoading.value = false
      activeStage.value = null
    }
  }
}

// 初始化会话
const initSession = async () => {
  // 获取助手名字
  try {
    const agentInfo = await configApi.getAgentInfo()
    if (agentInfo.name) {
      assistantName.value = agentInfo.name
    }
  } catch (error) {
    // 获取失败时使用默认名字
    console.warn('获取助手名字失败:', error)
  }

  const urlSession = route.query.session as string | undefined

  if (urlSession) {
    // URL 中有 session 参数，使用它
    currentSessionId.value = urlSession
    saveCurrentSession(urlSession)
    await loadSessionHistory(urlSession)
    initializing.value = false
    await scrollToBottom(true, true)
  } else {
    // URL 中没有 session 参数，尝试从 localStorage 读取
    const lastSession = getLastSession()
    if (lastSession) {
      // 有上次会话，设置 session 并加载历史，然后更新 URL
      currentSessionId.value = lastSession
      saveCurrentSession(lastSession)
      await loadSessionHistory(lastSession)
      // 使用 replace 更新 URL（不触发导航）
      window.history.replaceState({}, '', `/?session=${lastSession}`)
      initializing.value = false
      await scrollToBottom(true, true)
    } else {
      boundWorkspacePath.value = null
      setCurrentSession(null)
      initializing.value = false
    }
  }
}

// 监听 session 参数变化（处理从其他地方跳转过来的情况）
watch(
  () => route.query.session,
  async (newSession, oldSession) => {
    // 如果正在初始化，跳过
    if (initializing.value) return

    // 如果 session 没有实际变化，跳过
    if (newSession === oldSession) return

    const sessionId = (newSession as string) || null

    // 如果新 session 为空，不做处理（应该由 initSession 处理）
    if (!sessionId) return

    // 切换到新会话
    if (loading.value) {
      chatApi.cancelGeneration()
      if (abortController.value) {
        abortController.value.abort()
        abortController.value = null
      }
      loading.value = false
      pendingApproval.value = null
      pendingAskUser.value = null
      activeStage.value = null
      activeRunId.value = null
    }
    currentSessionId.value = sessionId
    saveCurrentSession(sessionId)
    inputMessage.value = ''
    messages.value = []
    historyLoading.value = true
    await loadSessionHistory(sessionId)
    await scrollToBottom(true, true)
  }
)

// 监听 refresh 参数变化（处理从配置页面初始化后跳转的情况）
watch(
  () => route.query.refresh,
  async (newRefresh) => {
    if (newRefresh) {
      // 重新获取助手名字
      try {
        const agentInfo = await configApi.getAgentInfo()
        if (agentInfo.name) {
          assistantName.value = agentInfo.name
        }
      } catch (error) {
        console.warn('获取助手名字失败:', error)
      }

      // 清除 URL 中的 refresh 参数
      const currentQuery = { ...route.query }
      delete currentQuery.refresh
      router.replace({ query: currentQuery })
    }
  }
)

// 组件挂载时初始化会话
onMounted(async () => {
  unsubConnection = chatWs.onConnectionState((state) => {
    wsConnectionState.value = state
  })
  chatWs.connect().catch(() => {})
  unsubConfig = chatWs.onConfigUpdated(() => {
    configApi.getAgentInfo().then(agentInfo => {
      if (agentInfo.name) assistantName.value = agentInfo.name
    }).catch(() => {})
  })
  await initSession()
})

onUnmounted(() => {
  unsubConfig?.()
  unsubConnection?.()
  stopRunClock()
})

watch(currentSessionId, (sid) => {
  if (sid) chatWs.subscribeSession(sid)
}, { immediate: true })

const isNearBottom = (threshold = 120): boolean => {
  const el = messagesContainer.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
}

// 滚动到底部（打开历史/发送时可强制，流式输出默认尊重用户阅读位置）
const scrollToBottom = async (instant = false, force = false) => {
  const shouldScroll = force || isNearBottom()
  await nextTick()
  await nextTick()
  if (!shouldScroll) return

  const doScroll = () => {
    const el = messagesContainer.value
    if (!el) return
    el.scrollTo({
      top: el.scrollHeight,
      behavior: instant ? 'auto' : 'smooth',
    })
  }

  doScroll()
  const raf = typeof window !== 'undefined' && window.requestAnimationFrame
    ? window.requestAnimationFrame
    : (cb: FrameRequestCallback) => globalThis.setTimeout(cb, 16)
  raf(() => {
    doScroll()
    raf(doScroll)
  })
}

// 加载更早的消息（优先请求后端上一页），并维持当前滚动位置
const loadMoreMessages = async () => {
  const el = messagesContainer.value
  if (!el || loadingMore.value || !hasMoreToRender.value) return
  loadingMore.value = true
  const prevHeight = el.scrollHeight
  const prevTop = el.scrollTop
  try {
    if (hasMoreHistory.value && historyNextCursor.value && currentSessionId.value) {
      const page = await sessionApi.getHistoryPage(currentSessionId.value, {
        limit: RENDER_BATCH,
        before: historyNextCursor.value,
      })
      const older = buildDisplayMessages(
        page.messages,
        page.session.created_at,
        page.session.updated_at,
      )
      messages.value = [...older, ...messages.value]
      renderLimit.value = Math.min(messages.value.length, renderLimit.value + older.length)
      historyNextCursor.value = page.next_cursor || null
      hasMoreHistory.value = page.has_more
    } else {
      renderLimit.value = Math.min(messages.value.length, renderLimit.value + RENDER_BATCH)
    }
    await nextTick()
    // 补偿顶部新增内容的高度，避免视口跳动
    el.scrollTop = prevTop + (el.scrollHeight - prevHeight)
  } finally {
    loadingMore.value = false
  }
}

// 滚动到顶部附近时自动补载更早的消息
const handleScroll = () => {
  const el = messagesContainer.value
  if (!el) return
  if (el.scrollTop < 120 && hasMoreToRender.value && !loadingMore.value) {
    loadMoreMessages()
  }
}

const findToolSegment = (toolId: number): { msgIndex: number; segment: ToolSegment; segments: MessageSegment[] } | null => {
  for (let msgIndex = 0; msgIndex < messages.value.length; msgIndex++) {
    const segments = messages.value[msgIndex]?.segments
    if (!segments) continue
    const segment = segments.find((item): item is ToolSegment => item.type === 'tool' && item.id === toolId)
    if (segment) return { msgIndex, segment, segments }
  }
  return null
}

const loadFullToolResult = async (toolId: number) => {
  const found = findToolSegment(toolId)
  if (!found || !currentSessionId.value) return
  const { msgIndex, segment, segments } = found
  if (!segment.toolResultTruncated || !segment.toolResultRef || segment.fullResultLoading) return

  segment.fullResultLoading = true
  updateMessageSegments(msgIndex, segments)
  try {
    const payload = await sessionApi.getToolResult(currentSessionId.value, segment.toolResultRef)
    if (payload.available && payload.content != null) {
      segment.result = payload.content
      segment.toolResultTruncated = false
      segment.status = toolResultLooksLikeError(payload.content) ? 'error' : 'done'
    } else {
      segment.result = payload.error || '工具结果不可用'
      segment.status = 'error'
    }
  } catch (error) {
    segment.result = error instanceof Error ? error.message : '工具结果加载失败'
    segment.status = 'error'
  } finally {
    segment.fullResultLoading = false
    updateMessageSegments(msgIndex, segments)
  }
}

// 切换工具折叠状态
const toggleToolCollapse = (toolId: number) => {
  const next = new Set(expandedTools.value)
  const willExpand = !next.has(toolId)
  if (willExpand) next.add(toolId)
  else next.delete(toolId)
  expandedTools.value = next
  if (willExpand) {
    loadFullToolResult(toolId)
  }
}

// 检查工具是否展开（默认折叠，只有点击后才展开）
const isToolExpanded = (toolId: number): boolean => {
  return expandedTools.value.has(toolId)
}

// 检查消息是否有可见内容
const hasVisibleContent = (msg: Message): boolean => {
  if (!msg.segments || msg.segments.length === 0) {
    // 没有分段，检查普通内容
    return !!msg.content
  }

  // 有分段，检查是否有可见的段
  for (const segment of msg.segments) {
    if (segment.type === 'text' && segment.content) {
      return true
    }
    if (segment.type === 'tool' && !getToolConfig(segment.tool).hidden) {
      return true
    }
  }
  return false
}

// 检查消息是否有文本内容（用于决定是否显示加载指示器）
const hasTextContent = (msg: Message): boolean => {
  if (!msg.segments || msg.segments.length === 0) {
    return !!msg.content
  }
  // 只检查文本段
  for (const segment of msg.segments) {
    if (segment.type === 'text' && segment.content) {
      return true
    }
  }
  return false
}

// 检查消息组是否正在等待响应（用于隐藏 group-footer）
const isGroupWaiting = (group: MessageGroup): boolean => {
  if (group.role !== 'assistant' || !loading.value) return false
  // 检查组内所有消息是否都没有文本内容
  return group.messages.every(msg => !hasTextContent(msg))
}

// 结束所有运行中的工具段
const finalizeRunningToolSegments = (segments: MessageSegment[], cancelled = false) => {
  for (const seg of segments) {
    if (seg.type === 'tool' && seg.status === 'running') {
      seg.status = cancelled ? 'cancelled' : 'done'
      if (!seg.result) seg.result = cancelled ? '（已取消）' : '（已结束）'
    }
  }
}

const finalizeLastAssistantTools = (cancelled = false) => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const msg = messages.value[i]
    if (msg?.role === 'assistant' && msg.segments?.length) {
      finalizeRunningToolSegments(msg.segments, cancelled)
      updateMessageSegments(i, msg.segments)
      break
    }
  }
  contextCompressing.value = false
  activeStage.value = null
}

// 停止生成
const stopGeneration = () => {
  chatApi.cancelGeneration()
  if (abortController.value) {
    abortController.value.abort()
    abortController.value = null
  }
  markRunTerminal('cancelled')
  finalizeLastAssistantTools(true)
  loading.value = false
  pendingApproval.value = null
  pendingAskUser.value = null
  activeStage.value = null
  activeRunId.value = null
}

const handleApproval = (decision: 'allow' | 'deny') => {
  if (!pendingApproval.value) return
  chatApi.approveTool(pendingApproval.value.runId, decision, {
    toolCallId: pendingApproval.value.toolCallId,
    sessionId: currentSessionId.value || undefined,
  })
  pendingApproval.value = null
}

const handleAskUser = (answer: string | string[]) => {
  if (!pendingAskUser.value) return
  const pending = pendingAskUser.value
  chatApi.answerAskUser(pending.runId, answer, {
    interruptId: pending.interruptId,
    toolCallId: pending.toolCallId,
    mode: pending.mode,
    sessionId: currentSessionId.value || undefined,
  })
  pendingAskUser.value = null
}

const handleHandoffAck = () => {
  if (!pendingAskUser.value) return
  const pending = pendingAskUser.value
  chatApi.ackHandoff(pending.runId, {
    interruptId: pending.interruptId,
    toolCallId: pending.toolCallId,
    sessionId: currentSessionId.value || undefined,
  })
  pendingAskUser.value = null
}

const requestUndo = (snapshotId: string) => {
  if (loading.value) return
  inputMessage.value = `请立即调用 undo_file_change，snapshot_id="${snapshotId}"，不要做其他操作。`
  void sendMessage()
}

// 更新消息段（触发 Vue 响应性）
const updateMessageSegments = (msgIndex: number, segments: MessageSegment[]) => {
  if (msgIndex >= 0 && msgIndex < messages.value.length) {
    const existingMsg = messages.value[msgIndex]!
    messages.value[msgIndex] = {
      id: existingMsg.id,
      role: existingMsg.role,
      content: existingMsg.content,
      timestamp: existingMsg.timestamp,
      segments: [...segments]
    }
  }
}

const sendMessage = async () => {
  if (!inputMessage.value.trim()) return
  if (!boundWorkspacePath.value || !currentSessionId.value) {
    message.warning('请先选择项目文件夹')
    return
  }

  const userMessage = inputMessage.value
  const userMsg: Message = {
    id: Date.now(),
    role: 'user',
    content: userMessage,
    timestamp: new Date()
  }

  messages.value.push(userMsg)
  inputMessage.value = ''
  lastRunSummary.value = null
  terminalRunStatus.value = null
  runStartedAt.value = Date.now()
  startRunClock()
  loading.value = true
  contextCompressing.value = false
  activeStage.value = chatWs.isConnected ? 'thinking' : 'connecting'
  activeRunId.value = null

  abortController.value = new AbortController()

  const startedSessionId = currentSessionId.value
  await scrollToBottom(false, true)

  const streamCtx = {
    messages,
    currentSessionId,
    contextUsage,
    apiUsage,
    contextCompressing,
    activeStage,
    pendingApproval,
    pendingAskUser,
    sessionTodos,
    assistantName,
    saveCurrentSession,
    updateMessageSegments,
    finalizeRunningToolSegments,
    markRunTerminal,
    scrollToBottom: () => { scrollToBottom() },
  }
  const batcher = createChatStreamEventBatcher(streamCtx)

  try {
    await chatApi.sendMessageWs(
      userMessage,
      currentSessionId.value || undefined,
      (event) => {
        if (startedSessionId && currentSessionId.value && currentSessionId.value !== startedSessionId) return
        if (event.run_id) {
          if (activeRunId.value && activeRunId.value !== event.run_id) return
          activeRunId.value = event.run_id
        }
        batcher.handle(event)
      },
      abortController.value.signal
    )

    batcher.flush()
    await scrollToBottom(false, true)
  } catch (error: unknown) {
    if (error instanceof Error && error.name === 'AbortError') {
      markRunTerminal('cancelled')
      console.log('用户取消了请求')
    } else {
      console.error('发送消息失败:', error)
      markRunTerminal('error')
      message.error('发送消息失败')
      messages.value.pop()
    }
  } finally {
    batcher.flush()
    if (currentSessionId.value === startedSessionId) {
      finalizeRunSummary('done')
    } else {
      stopRunClock()
      runStartedAt.value = null
    }
    loading.value = false
    abortController.value = null
    activeStage.value = null
    activeRunId.value = null
    refreshSessions().catch(() => {})
  }
}

const applyFolderPath = async (path: string, mode: 'create' | 'rebind') => {
  const trimmed = path.trim()
  if (!trimmed) {
    message.warning('请输入已存在的项目文件夹路径')
    return
  }
  bindingFolder.value = true
  try {
    if (mode === 'rebind' && currentSessionId.value) {
      const session = await sessionApi.rebind(currentSessionId.value, trimmed)
      boundWorkspacePath.value = session.workspace_path || trimmed
    } else {
      const res = await sessionApi.create(trimmed)
      saveCurrentSession(res.session_id)
      currentSessionId.value = res.session_id
      boundWorkspacePath.value = trimmed
      await router.replace({ name: 'chat', query: { session: res.session_id } })
      await loadSessionHistory(res.session_id)
    }
    folderDraft.value = boundWorkspacePath.value || trimmed
    saveLastWorkspacePath(folderDraft.value)
    await refreshSessions()
    if (currentSessionId.value) {
      const latest = await sessionApi.get(currentSessionId.value)
      setCurrentSession(latest)
    }
  } catch (error: unknown) {
    const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail || (mode === 'rebind' ? '更换文件夹失败' : '绑定文件夹失败'))
  } finally {
    bindingFolder.value = false
  }
}

const browseFolder = async () => {
  const selected = await pickDirectory(folderDraft.value || boundWorkspacePath.value || '')
  if (selected) folderDraft.value = selected
}

const confirmFolder = async () => {
  await applyFolderPath(folderDraft.value, currentSessionId.value ? 'rebind' : 'create')
}

const createNewSession = async () => {
  const path = (
    currentProjectPath.value ||
    folderDraft.value ||
    boundWorkspacePath.value ||
    getLastWorkspacePath()
  ).trim()
  if (!path) {
    message.warning('请先选择项目文件夹')
    return
  }
  await applyFolderPath(path, 'create')
}

const openWorkspaceFolder = async () => {
  const path = boundWorkspacePath.value?.trim()
  if (!path) {
    await browseFolder()
    if (folderDraft.value) {
      await applyFolderPath(folderDraft.value, currentSessionId.value ? 'rebind' : 'create')
    }
    return
  }
  const result = await openDirectory(path)
  if (result === 'copied') {
    message.success('已复制路径')
  } else if (result === 'failed') {
    message.error('无法打开文件夹')
  }
}
</script>

<template>
  <div class="chat-view">
    <div class="workspace-bar">
      <span class="workspace-label">工作区</span>
      <span v-if="boundWorkspacePath" class="workspace-path" :title="boundWorkspacePath">{{ boundWorkspacePath }}</span>
      <span v-else class="workspace-path muted">未绑定项目文件夹</span>
      <Button size="small" type="link" :loading="bindingFolder" @click="openWorkspaceFolder">
        {{ boundWorkspacePath ? '打开' : '选择' }}
      </Button>
    </div>
    <!-- 消息区域 -->
    <div class="chat-messages" ref="messagesContainer" @scroll="handleScroll">
      <!-- 初始化加载状态 -->
      <div v-if="initializing || historyLoading" class="empty-state">
        <MailinLogo :size="100" logo-class="empty-icon loading" />
        <p class="empty-hint">{{ thinkingLabel }}</p>
      </div>
      <template v-else-if="messages.length > 0">
        <!-- 加载更早消息入口 -->
        <div v-if="hasMoreToRender" class="load-more" @click="loadMoreMessages">
          <LoadingOutlined v-if="loadingMore" />
          <span>{{ loadingMore ? '加载中...' : '加载更早的消息' }}</span>
        </div>
        <div
          v-if="runProgressSummary"
          :class="['run-progress', { active: runProgressSummary.active }]"
          aria-live="polite"
        >
          <span class="run-progress-indicator" aria-hidden="true"></span>
          <span class="run-progress-stage">{{ runProgressSummary.stageLabel }}</span>
          <code class="inline-code">用时 {{ runProgressSummary.elapsedLabel }}</code>
          <code
            v-if="runProgressSummary.todoTotal > 0"
            class="inline-code"
          >{{ runProgressSummary.todoCompleted }}/{{ runProgressSummary.todoTotal }} 个任务完成</code>
          <span v-if="runProgressSummary.terminalLabel" class="run-progress-terminal">
            {{ runProgressSummary.terminalLabel }}
          </span>
        </div>
        <div
          v-for="(group, groupIndex) in messageGroups"
          :key="groupIndex"
          v-show="group.role !== 'assistant' || hasGroupVisibleContent(group) || shouldShowGroupThinking(group, groupIndex)"
          :class="['message-group', group.role]"
        >
          <!-- 头像 -->
          <div class="group-avatar">
            <MailinLogo v-if="group.role === 'assistant'" :size="36" />
            <div v-else class="user-avatar">你</div>
          </div>

          <!-- 消息内容 -->
          <div class="group-content">
            <!-- 遍历每条消息 -->
            <template v-for="(msg, msgIndex) in group.messages" :key="msg.id">
              <!-- 如果有分段，按分段显示 -->
              <template v-if="msg.segments && msg.segments.length > 0">
                <template v-for="segment in msg.segments" :key="segment.id">
                  <!-- 文本段 -->
                  <div v-if="segment.type === 'text' && segment.content" class="message-bubble agent-bubble">
                    <div class="message-text">
                      <MarkdownContent
                        :content="segment.content"
                        :streaming="loading && group.role === 'assistant' && isLastMessageGroup(groupIndex)"
                      />
                    </div>
                  </div>
                  <!-- 工具调用段 - 只显示非隐藏的工具 -->
                  <div
                    v-if="segment.type === 'tool' && !getToolConfig(segment.tool).hidden"
                    :class="['tool-card', 'system-action-node', segment.status]"
                    data-origin="system"
                  >
                    <div
                      class="tool-header"
                      @click="segment.status !== 'running' && toggleToolCollapse(segment.id)"
                    >
                      <span class="tool-icon" aria-hidden="true">{{ getToolConfig(segment.tool).icon }}</span>
                      <span class="tool-name">
                        <span class="tool-action-verb">
                          {{ getToolActionDisplay(segment.tool, segment.args, segment.status).verb }}
                          {{ getToolActionDisplay(segment.tool, segment.args, segment.status).name }}
                        </span>
                        <code
                          v-if="getToolActionDisplay(segment.tool, segment.args, segment.status).target"
                          class="inline-code tool-inline-entity"
                          :title="getToolActionDisplay(segment.tool, segment.args, segment.status).target"
                        >{{ getToolActionDisplay(segment.tool, segment.args, segment.status).target }}</code>
                      </span>
                      <Tag v-if="segment.status === 'running'" color="processing" class="tool-tag">
                        <LoadingOutlined /> 执行中
                      </Tag>
                      <Tag v-else-if="segment.fullResultLoading" color="processing" class="tool-tag">
                        <LoadingOutlined /> 载入中
                      </Tag>
                      <Tag v-else-if="segment.status === 'error' || toolResultLooksLikeError(segment.result)" color="error" class="tool-tag">失败</Tag>
                      <Tag v-else-if="segment.status === 'policy_denied'" color="warning" class="tool-tag">策略拒绝</Tag>
                      <Tag v-else-if="segment.status === 'cancelled'" color="warning" class="tool-tag">已取消</Tag>
                      <Tag v-else-if="segment.status === 'done'" color="success" class="tool-tag">完成</Tag>
                      <span
                        v-if="segment.status !== 'running'"
                        class="collapse-indicator"
                      >
                        {{ isToolExpanded(segment.id) ? '▼' : '▶' }}
                      </span>
                    </div>
                    <p
                      v-if="!isToolExpanded(segment.id) && segment.result && !toolResultLooksLikeError(segment.result)"
                      class="tool-result-summary"
                    >
                      <span class="system-label">系统返回</span>
                      <span>{{ toolResultPreview(segment.result) }}</span>
                    </p>
                    <div
                      v-if="segment.snapshotId && segment.status === 'done'"
                      class="tool-undo-row"
                    >
                      <Button
                        v-if="segment.undoStatus === 'available'"
                        size="small"
                        @click.stop="requestUndo(segment.snapshotId!)"
                      >
                        撤销此变更
                      </Button>
                      <span v-else-if="segment.undoStatus === 'conflict'" class="undo-status">无法撤销：目标已变化</span>
                      <span v-else-if="segment.undoStatus === 'expired'" class="undo-status">快照已过期</span>
                      <span v-else-if="segment.undoStatus === 'cross_session'" class="undo-status">跨会话拒绝</span>
                      <span v-else-if="segment.undoStatus === 'unavailable'" class="undo-status">快照不可用</span>
                    </div>
                    <p
                      v-if="!isToolExpanded(segment.id) && toolResultLooksLikeError(segment.result)"
                      class="tool-error-preview"
                    ><span class="system-label">系统错误</span>{{ toolErrorPreview(segment.result) }}</p>
                    <!-- 展开后显示入参和结果 -->
                    <div v-if="isToolExpanded(segment.id)" class="tool-details">
                      <!-- 入参 -->
                      <div v-if="segment.args && Object.keys(segment.args).length > 0" class="tool-args">
                        <div class="tool-detail-label">工具输入</div>
                        <pre class="tool-detail-content">{{ formatToolArgs(segment.args) }}</pre>
                      </div>
                      <!-- 结果 -->
                      <div v-if="segment.result" class="tool-result-wrapper">
                        <div class="tool-detail-label">{{ segment.toolResultTruncated ? '系统返回预览' : '系统返回' }}</div>
                        <pre class="tool-detail-content">{{ formatToolResult(segment.result) }}</pre>
                      </div>
                    </div>
                  </div>
                </template>
              </template>
              <!-- 如果没有分段，显示普通内容（历史消息） -->
              <div v-else-if="msg.content" class="message-bubble agent-bubble">
                <div class="message-text">
                  <MarkdownContent
                    :content="msg.content"
                    :streaming="loading && group.role === 'assistant' && isLastMessageGroup(groupIndex)"
                  />
                </div>
              </div>
            </template>

            <!-- 助手回复进行中：尚无文本时显示思考提示 -->
            <div v-if="shouldShowGroupThinking(group, groupIndex)" class="message-bubble thinking-bubble">
              <div class="thinking-indicator">
                <div class="loading-dots" aria-hidden="true">
                  <span></span>
                  <span></span>
                  <span></span>
                </div>
                <span class="thinking-label">{{ thinkingLabel }}</span>
              </div>
            </div>

            <!-- 组底部：名称和时间（加载等待时隐藏） -->
            <div v-if="!isGroupWaiting(group)" class="group-footer">
              <span class="group-name">{{ group.role === 'user' ? '你' : assistantName }}</span>
              <span class="group-time">{{ formatMessageTime(group.messages[0]!.timestamp) }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- 空状态 -->
      <div v-else class="empty-state">
        <MailinLogo :size="100" logo-class="empty-icon" />
        <p class="empty-hint">{{ boundWorkspacePath ? '发送消息开始对话' : '先选择一个项目文件夹，再开始对话' }}</p>
        <div v-if="!boundWorkspacePath" class="folder-picker">
          <Input
            v-model:value="folderDraft"
            placeholder="项目文件夹的绝对路径"
            @press-enter="confirmFolder"
          />
          <Button @click="browseFolder">
            <FolderOpenOutlined />
            浏览
          </Button>
          <Button type="primary" :loading="bindingFolder" @click="confirmFolder">
            开始
          </Button>
        </div>
      </div>

      <!-- 加载指示器（助手消息组样式）- 等待响应时显示 -->
      <div v-if="loading && shouldShowLoadingIndicator" class="message-group assistant loading-group">
        <div class="group-avatar">
          <MailinLogo :size="36" />
        </div>
        <div class="group-content">
          <div class="message-bubble thinking-bubble">
            <div class="thinking-indicator">
              <div class="loading-dots" aria-hidden="true">
                <span></span>
                <span></span>
                <span></span>
              </div>
              <span class="thinking-label">{{ thinkingLabel }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <ToolApprovalModal :pending="pendingApproval" @decide="handleApproval" />
    <AskUserModal
      :pending="pendingAskUser"
      @answer="handleAskUser"
      @handoff-ack="handleHandoffAck"
    />

    <!-- 输入区域 -->
    <div class="chat-input-wrapper">
      <div
        v-if="sessionTodos.length && !runProgressSummary?.active"
        class="todo-summary"
        aria-live="polite"
      >
        <span>当前计划</span>
        <code class="inline-code">{{ completedTodoCount }}/{{ sessionTodos.length }} 个任务完成</code>
      </div>
      <ul v-if="sessionTodos.length" class="session-todos" aria-label="当前待办">
        <li
          v-for="item in sessionTodos"
          :key="item.id"
          class="session-todo"
          :data-status="item.status"
        >
          <span class="todo-status">{{
            item.status === 'completed' ? '完成' : item.status === 'in_progress' ? '进行中' : '待办'
          }}</span>
          <span class="todo-content">{{ item.content }}</span>
        </li>
      </ul>
      <div class="chat-input">
        <ContextUsageRing
          v-if="contextUsage"
          :context-usage="contextUsage"
          :api-usage="apiUsage"
          :compressing="contextCompressing"
        />
        <!-- 输入框 -->
        <Input.TextArea
          v-model:value="inputMessage"
          :placeholder="boundWorkspacePath ? '输入消息... (Enter 发送, Shift+Enter 换行)' : '请先选择项目文件夹'"
          :disabled="!boundWorkspacePath"
          :auto-size="{ minRows: 1, maxRows: 4 }"
          @press-enter="(e: KeyboardEvent) => { if (!e.shiftKey) { e.preventDefault(); sendMessage() } }"
        />
        <!-- 按钮区域（固定宽度） -->
        <div class="input-actions">
          <!-- 新建会话按钮 -->
          <Button
            class="icon-btn"
            @click="createNewSession"
            title="新建会话"
          >
            <template #icon>
              <PlusOutlined />
            </template>
          </Button>
          <!-- 停止按钮（loading 时显示） -->
          <button
            v-if="loading"
            class="stop-btn"
            @click="stopGeneration"
            title="停止生成"
          >
            <div class="stop-icon"></div>
          </button>
          <!-- 发送按钮（有文字时显示） -->
          <button
            v-else-if="inputMessage.trim()"
            class="send-btn active"
            @click="sendMessage"
            title="发送消息"
          >
            <SendOutlined />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  box-sizing: border-box;
  background-color: var(--color-background);
}

.workspace-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 20px 0;
  min-height: 36px;
}

.workspace-label {
  font-size: 12px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.workspace-path {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workspace-path.muted {
  color: var(--color-text-secondary);
}

.folder-picker {
  display: flex;
  gap: 8px;
  width: min(560px, 90%);
  margin-top: 8px;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 加载更早消息入口 */
.load-more {
  align-self: center;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  margin-bottom: 4px;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 14px;
  cursor: pointer;
  user-select: none;
  transition: all 0.2s ease;
}

.load-more:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

/* 当前回合的轻量进度上下文 */
.run-progress {
  align-self: center;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  max-width: min(800px, 100%);
  padding: 7px 12px;
  margin-bottom: 4px;
  color: var(--color-text-secondary);
  background: color-mix(in srgb, var(--color-surface) 88%, var(--color-primary-light));
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 12px;
}

.run-progress.active {
  border-color: var(--color-primary-border);
}

.run-progress-indicator {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  background: var(--color-text-secondary);
}

.run-progress.active .run-progress-indicator {
  background: var(--color-primary);
  animation: progress-pulse 1.4s ease-in-out infinite;
}

.run-progress-stage {
  color: var(--color-text);
  font-weight: 600;
}

.run-progress-terminal {
  color: var(--color-text-secondary);
}

@keyframes progress-pulse {
  0%, 100% { opacity: 0.45; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}

/* 消息组样式 */
.message-group {
  display: flex;
  gap: 12px;
  max-width: 85%;
}

.message-group.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.message-group.assistant {
  align-self: flex-start;
}

/* 头像 */
.group-avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
}

.group-avatar img,
.group-avatar :deep(.mailin-logo) {
  width: 36px;
  height: 36px;
  border-radius: 8px;
}

.user-avatar {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--color-accent) 0%, #3d6b4d 100%);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* 消息组内容 */
.group-content {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

/* 消息气泡 */
.message-bubble {
  display: inline-block;
  max-width: 100%;
}

.agent-bubble .message-text {
  border-left: 2px solid color-mix(in srgb, var(--color-primary) 45%, transparent);
}

.message-text {
  padding: 10px 14px;
  border-radius: 12px;
  background-color: var(--color-surface);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
  line-height: 1.6;
  word-wrap: break-word;
}

.message-group.user .message-text {
  background-color: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
}

/* 组底部 */
.group-footer {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 4px;
  padding-left: 4px;
}

.group-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}

.group-time {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* 空状态 */
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
}

.empty-state :deep(.empty-icon) {
  opacity: 0.5;
}

.empty-state :deep(.empty-icon.loading) {
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% {
    opacity: 0.3;
    transform: scale(0.95);
  }
  50% {
    opacity: 0.6;
    transform: scale(1);
  }
}

.empty-hint {
  color: var(--color-text-secondary);
  font-size: 14px;
}

/* 加载指示器 */
.loading-group .message-bubble,
.message-bubble:has(.loading-dots),
.thinking-bubble {
  padding: 14px 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.thinking-indicator {
  display: flex;
  align-items: center;
  gap: 10px;
}

.thinking-label {
  color: var(--color-text-secondary);
  font-size: 13px;
  animation: thinking-fade 1.6s ease-in-out infinite;
}

@keyframes thinking-fade {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 1; }
}

.loading-dots {
  display: flex;
  gap: 4px;
  align-items: center;
}

.loading-dots span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: var(--color-primary);
  animation: loading-pulse 1.4s ease-in-out infinite;
}

.loading-dots span:nth-child(2) {
  animation-delay: 0.2s;
}

.loading-dots span:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes loading-pulse {
  0%, 100% {
    opacity: 0.4;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1);
  }
}

/* 输入区域 */
.chat-input-wrapper {
  padding: 16px 24px 32px;
  background-color: var(--color-surface);
  border-top: 1px solid var(--color-border);
}

.session-todos {
  list-style: none;
  margin: 0 auto 12px;
  padding: 0;
  max-width: 800px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.todo-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  max-width: 800px;
  margin: 0 auto 8px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.session-todo {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text);
}

.todo-status {
  flex: none;
  min-width: 48px;
  font-size: 11px;
  letter-spacing: 0.02em;
  color: var(--color-text-muted, #8a8680);
}

.session-todo[data-status='in_progress'] .todo-status {
  color: var(--color-primary, #1677ff);
}

.session-todo[data-status='completed'] .todo-content {
  text-decoration: line-through;
  opacity: 0.7;
}

.chat-input {
  display: flex;
  gap: 12px;
  align-items: center;
  max-width: 800px;
  margin: 0 auto;
}

.chat-input :deep(.ant-input) {
  flex: 1;
  border-radius: 16px;
  padding: 10px 16px;
  resize: none;
}

/* 按钮区域（固定宽度，防止输入框抖动） */
.input-actions {
  flex-shrink: 0;
  display: flex;
  gap: 12px;
  align-items: center;
  width: 92px;
}

/* 新建会话按钮 */
.input-actions .icon-btn {
  width: 40px;
  height: 40px;
  padding: 0;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
}

.input-actions .icon-btn:hover {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

/* 发送按钮 - 白底 + 黑色图标，输入后红底 + 白色图标 */
.input-actions .send-btn {
  width: 40px;
  height: 40px;
  padding: 0;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.2s ease;
  color: #333;
}

.input-actions .send-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

/* 输入文字后：红底 + 白色图标 */
.input-actions .send-btn.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}

.input-actions .send-btn.active:hover {
  background: var(--color-primary-hover);
  border-color: var(--color-primary-hover);
}

/* 停止按钮 - 红底 + 白色圆角方块图标 */
.input-actions .stop-btn {
  width: 40px;
  height: 40px;
  padding: 0;
  border: none;
  border-radius: 8px;
  background: var(--color-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.2s ease;
}

.input-actions .stop-btn:hover {
  background: var(--color-primary-hover);
}

.stop-icon {
  width: 14px;
  height: 14px;
  background: #fff;
  border-radius: 3px;
}

/* 工具调用卡片 */
.tool-calls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

.tool-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  transition: all 0.2s ease;
}

.system-action-node {
  box-shadow: none;
}

/* 执行中状态 - 龙虾红主题 */
.tool-card.running {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.tool-card.running .tool-icon,
.tool-card.running .tool-name {
  color: var(--color-primary);
}

/* 完成状态 - 灰色调 */
.tool-card.done {
  border-color: var(--color-border);
  background: var(--color-surface);
}

/* 失败状态 - 红色调 */
.tool-card.error {
  border-color: var(--color-primary);
  background: var(--color-danger-bg);
}

.tool-card.error .tool-icon,
.tool-card.error .tool-name {
  color: var(--color-primary);
}

.tool-card.cancelled {
  border-color: var(--color-border);
  background: color-mix(in srgb, var(--color-surface) 88%, #f0ad4e);
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.tool-header:hover {
  opacity: 0.8;
}

.tool-name {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.tool-action-verb {
  white-space: nowrap;
}

.inline-code {
  display: inline-block;
  max-width: 100%;
  padding: 1px 5px;
  color: var(--color-text);
  background: rgba(0, 0, 0, 0.05);
  border-radius: 4px;
  font-family: ui-monospace, 'SF Mono', Monaco, 'Andale Mono', monospace;
  font-size: 0.92em;
  overflow-wrap: anywhere;
  vertical-align: baseline;
}

.tool-inline-entity {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.system-label {
  margin-right: 6px;
  color: var(--color-text-secondary);
  font-size: 11px;
  font-weight: 600;
}

.tool-result-summary {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin: 6px 0 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.tool-undo-row {
  margin-top: 6px;
}

.undo-status {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.tool-error-preview {
  margin: 6px 0 0;
  padding: 0 2px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-primary);
  overflow-wrap: anywhere;
}

.tool-icon {
  font-size: 14px;
  line-height: 1;
}

.tool-name {
  font-weight: 500;
  color: var(--color-text);
  flex: 1;
}

.tool-tag {
  font-size: 11px;
  padding: 0 6px;
  line-height: 18px;
  border-radius: 4px;
}

.collapse-indicator {
  font-size: 10px;
  color: var(--color-text-secondary);
  margin-left: auto;
  transition: transform 0.2s ease;
}

/* 工具详情区域 */
.tool-details {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border);
}

.tool-args,
.tool-result-wrapper {
  margin-bottom: 8px;
}

.tool-result-wrapper:last-child {
  margin-bottom: 0;
}

.tool-detail-label {
  font-size: 11px;
  color: var(--color-text-secondary);
  margin-bottom: 4px;
  font-weight: 500;
}

.tool-detail-content {
  margin: 0;
  padding: 8px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 4px;
  font-size: 12px;
  color: var(--color-text);
  max-height: 150px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, 'SF Mono', Monaco, 'Andale Mono', monospace;
}

.step-info {
  color: var(--color-text-secondary);
  font-size: 11px;
}
</style>

<style>
/* Tooltip 挂载到 body，需全局样式保证分行 */
.context-usage-tooltip .ant-tooltip-inner {
  min-width: 160px;
  max-width: 280px;
}

.context-usage-tooltip .context-usage-tooltip-lines {
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: left;
}

.context-usage-tooltip .context-usage-tooltip-line {
  line-height: 1.5;
  white-space: normal;
  word-break: break-word;
  font-size: 13px;
}
</style>
