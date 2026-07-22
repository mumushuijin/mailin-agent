<script setup lang="ts">
import { ref, watch, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { Input, Button, message, Tag, Tooltip, Modal } from 'ant-design-vue'
import { SendOutlined, PlusOutlined, StopOutlined, LoadingOutlined } from '@ant-design/icons-vue'
import { useRouter, useRoute } from 'vue-router'
import { sessionApi } from '@/api/session'
import { chatApi, type ApiUsage, type ContextUsage } from '@/api/chat'
import { chatWs } from '@/api/ws'
import { configApi } from '@/api/config'
import { renderMarkdown, formatMessageTime } from '@/utils/markdown'
import { getToolConfig, formatToolArgs, formatToolResult } from '@/utils/toolDisplay'
import MailinLogo from '@/components/MailinLogo.vue'

// localStorage key for saving current session
const SESSION_STORAGE_KEY = 'mailin.lastSessionId'

// 助手名字（从后端获取）
const assistantName = ref('麦林')

// 消息段类型
interface TextSegment {
  type: 'text'
  id: number
  content: string
}

interface ToolSegment {
  type: 'tool'
  id: number
  tool: string
  args: Record<string, unknown>
  result?: string
  status: 'running' | 'done' | 'error'
}

type MessageSegment = TextSegment | ToolSegment

interface Message {
  id: number
  role: 'user' | 'assistant'
  content: string  // 用于从历史加载的消息
  timestamp: Date
  segments?: MessageSegment[]  // 用于流式消息的分段
}

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
const collapsedTools = ref<Set<number>>(new Set())
const expandedTools = ref<Set<number>>(new Set())
const contextUsage = ref<ContextUsage | null>(null)
const apiUsage = ref<ApiUsage | null>(null)
const contextCompressing = ref(false)

// 工具审批（WebSocket interrupt）
const pendingApproval = ref<{
  runId: string
  tool: string
  args: Record<string, unknown>
  reason: string
} | null>(null)

let unsubConfig: (() => void) | null = null

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
const hasMoreToRender = computed(() => messages.value.length > renderLimit.value)

const contextUsagePercent = computed(() => {
  if (!contextUsage.value) return 0
  return Math.min(100, Math.round(contextUsage.value.ratio * 100))
})

const contextUsageLevel = computed(() => {
  const ratio = contextUsage.value?.ratio ?? 0
  if (ratio > 0.8) return 'danger'
  if (ratio > 0.6) return 'warning'
  return 'normal'
})

const contextUsageLines = computed(() => {
  if (!contextUsage.value) return ['上下文窗口占用']
  const u = contextUsage.value
  const max = u.max_tokens.toLocaleString()
  const total = u.total_tokens.toLocaleString()
  const hasApi = (apiUsage.value?.prompt_tokens ?? 0) > 0
  const lines = [`上下文窗口 ${contextUsagePercent.value}%`]

  if (hasApi) {
    lines.push(`有效 ${total} / ${max}（API+增量）`)
    const api = apiUsage.value!
    const prompt = (api.agent?.prompt_tokens ?? api.prompt_tokens).toLocaleString()
    const completion = api.completion_tokens.toLocaleString()
    lines.push(`API ${prompt} + ${completion} out`)
    if (u.estimated_tokens != null) {
      lines.push(`粗估 ${u.estimated_tokens.toLocaleString()}`)
    }
  } else {
    lines.push(`粗估 ${total} / ${max}`)
  }

  const api = apiUsage.value
  if (api?.prompt_cache_hit_tokens && api.prompt_cache_hit_tokens > 0) {
    lines.push(`缓存 ${api.prompt_cache_hit_tokens.toLocaleString()}`)
  }

  if (contextCompressing.value) lines.push('正在整理上下文…')
  else if (u.warning) lines.push(u.warning)
  return lines
})

const contextUsageTooltip = computed(() => contextUsageLines.value.join('\n'))

const RING_SIZE = 40
const RING_RADIUS = 16
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS

const contextRingOffset = computed(() => {
  const ratio = Math.min(1, contextUsage.value?.ratio ?? 0)
  return RING_CIRCUMFERENCE * (1 - ratio)
})

const contextRingColor = computed(() => {
  if (contextCompressing.value) return 'var(--color-primary)'
  const level = contextUsageLevel.value
  if (level === 'danger') return 'var(--color-primary)'
  if (level === 'warning') return '#faad14'
  return '#52c41a'
})

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

const thinkingLabel = computed(() => {
  if (contextCompressing.value) return '整理上下文中…'
  const groups = messageGroups.value
  const lastGroup = groups[groups.length - 1]
  if (lastGroup && hasGroupToolWithoutText(lastGroup)) {
    return '处理工具结果中…'
  }
  return '思考中…'
})

// 保存当前会话 ID 到 localStorage
const saveCurrentSession = (sessionId: string) => {
  localStorage.setItem(SESSION_STORAGE_KEY, sessionId)
}

// 从 localStorage 读取上次会话 ID（兼容旧版 key）
const getLastSession = (): string | null => {
  const current = localStorage.getItem(SESSION_STORAGE_KEY)
  if (current) return current

  const legacy = localStorage.getItem('helloclaw.lastSessionId')
  if (legacy) {
    localStorage.setItem(SESSION_STORAGE_KEY, legacy)
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

// 加载会话历史（按照 OpenAI 标准格式解析）
const loadSessionHistory = async (sessionId: string) => {
  contextUsage.value = null
  apiUsage.value = null
  contextCompressing.value = false
  renderLimit.value = RENDER_WINDOW
  try {
    const [historyRes, sessionRes] = await Promise.all([
      sessionApi.getHistory(sessionId),
      sessionApi.get(sessionId),
    ])
    const rawMessages = historyRes.messages
    const sessionCreatedAt = sessionRes.created_at
    const sessionUpdatedAt = sessionRes.updated_at

    // 用于存储工具调用结果（tool_call_id -> result）
    const toolResults: Map<string, string> = new Map()

    // 第一遍：收集所有 tool 消息的结果
    for (const msg of rawMessages) {
      if (msg.role === 'tool' && msg.tool_call_id && msg.content) {
        toolResults.set(msg.tool_call_id, msg.content)
      }
    }

    // 第二遍：构建显示消息
    const displayMessages: Message[] = []
    let pendingAssistant: Message | null = null

    for (let i = 0; i < rawMessages.length; i++) {
      const msg = rawMessages[i]!

      if (msg.role === 'user') {
        // 如果有待处理的 assistant 消息，先添加
        if (pendingAssistant) {
          displayMessages.push(pendingAssistant)
          pendingAssistant = null
        }
        // 添加 user 消息
        displayMessages.push({
          id: Date.now() + i,
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
      }
      else if (msg.role === 'assistant') {
        if (msg.tool_calls && msg.tool_calls.length > 0) {
          // 包含工具调用的 assistant 消息
          const segments: MessageSegment[] = []

          // 添加工具调用段
          msg.tool_calls.forEach((tc, tcIndex) => {
            const result = toolResults.get(tc.id)
            segments.push({
              type: 'tool',
              id: Date.now() + i * 1000 + tcIndex,
              tool: tc.function.name,
              args: JSON.parse(tc.function.arguments || '{}'),
              result: result,
              status: result?.startsWith('❌') ? 'error' : 'done'
            })
          })

          // 检查下一个消息是否是最终的 assistant 回答（没有 tool_calls）
          const nextMsg = rawMessages[i + 1]
          if (nextMsg && nextMsg.role === 'assistant' && !nextMsg.tool_calls && nextMsg.content) {
            // 有最终回答，添加文本段
            segments.push({
              type: 'text',
              id: Date.now() + i * 1000 + 100,
              content: nextMsg.content
            })
            i++ // 跳过下一个消息
          }

          pendingAssistant = {
            id: Date.now() + i,
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
          // 普通的 assistant 文本消息
          if (pendingAssistant) {
            // 追加到待处理的 assistant 消息
            if (!pendingAssistant.segments) {
              pendingAssistant.segments = []
            }
            pendingAssistant.segments.push({
              type: 'text',
              id: Date.now() + i,
              content: msg.content
            })
          } else {
            // 新的 assistant 消息
            displayMessages.push({
              id: Date.now() + i,
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
      // tool 消息在第一遍已经处理，跳过
    }

    // 添加最后的待处理消息
    if (pendingAssistant) {
      displayMessages.push(pendingAssistant)
    }

    messages.value = displayMessages
    if (historyRes.context_usage) {
      contextUsage.value = historyRes.context_usage
    }
    if (historyRes.api_usage) {
      apiUsage.value = historyRes.api_usage
    }
  } catch (error) {
    // 会话不存在或加载失败，清空消息
    messages.value = []
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
    await scrollToBottom(true)
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
      await scrollToBottom(true)
    } else {
      // 没有上次会话，创建新会话
      try {
        const res = await sessionApi.create()
        saveCurrentSession(res.session_id)
        currentSessionId.value = res.session_id
        await loadSessionHistory(res.session_id)
        // 使用 replace 更新 URL（不触发导航）
        window.history.replaceState({}, '', `/?session=${res.session_id}`)
        initializing.value = false
      } catch (error) {
        message.error('创建会话失败')
        initializing.value = false
      }
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
    currentSessionId.value = sessionId
    saveCurrentSession(sessionId)
    inputMessage.value = ''
    await loadSessionHistory(sessionId)
    await scrollToBottom(true)
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
})

watch(currentSessionId, (sid) => {
  if (sid) chatWs.subscribeSession(sid)
}, { immediate: true })

// 滚动到底部（打开历史时用 instant，确保先看到最新消息）
const scrollToBottom = async (instant = false) => {
  await nextTick()
  await nextTick()

  const doScroll = () => {
    const el = messagesContainer.value
    if (!el) return
    el.scrollTo({
      top: el.scrollHeight,
      behavior: instant ? 'auto' : 'smooth',
    })
  }

  doScroll()
  requestAnimationFrame(() => {
    doScroll()
    requestAnimationFrame(doScroll)
  })
}

// 加载更早的消息（扩大渲染窗口），并维持当前滚动位置
const loadMoreMessages = async () => {
  const el = messagesContainer.value
  if (!el || loadingMore.value || !hasMoreToRender.value) return
  loadingMore.value = true
  const prevHeight = el.scrollHeight
  const prevTop = el.scrollTop
  renderLimit.value = Math.min(messages.value.length, renderLimit.value + RENDER_BATCH)
  await nextTick()
  // 补偿顶部新增内容的高度，避免视口跳动
  el.scrollTop = prevTop + (el.scrollHeight - prevHeight)
  loadingMore.value = false
}

// 滚动到顶部附近时自动补载更早的消息
const handleScroll = () => {
  const el = messagesContainer.value
  if (!el) return
  if (el.scrollTop < 120 && hasMoreToRender.value && !loadingMore.value) {
    loadMoreMessages()
  }
}

// 切换工具折叠状态
const toggleToolCollapse = (toolId: number) => {
  if (expandedTools.value.has(toolId)) {
    expandedTools.value.delete(toolId)
  } else {
    expandedTools.value.add(toolId)
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

// 检查消息是否有可见的工具调用（用于决定是否显示工具卡片而非加载指示器）
const hasVisibleTools = (msg: Message): boolean => {
  if (!msg.segments) return false
  for (const segment of msg.segments) {
    if (segment.type === 'tool' && !getToolConfig(segment.tool).hidden) {
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
      seg.status = 'done'
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
}

// 停止生成
const stopGeneration = () => {
  chatApi.cancelGeneration()
  if (abortController.value) {
    abortController.value.abort()
    abortController.value = null
  }
  finalizeLastAssistantTools(true)
  loading.value = false
  pendingApproval.value = null
}

const handleApproval = (decision: 'allow' | 'deny') => {
  if (!pendingApproval.value) return
  chatApi.approveTool(pendingApproval.value.runId, decision)
  pendingApproval.value = null
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

  const userMessage = inputMessage.value
  const userMsg: Message = {
    id: Date.now(),
    role: 'user',
    content: userMessage,
    timestamp: new Date()
  }

  messages.value.push(userMsg)
  inputMessage.value = ''
  loading.value = true
  contextCompressing.value = false

  // 创建 AbortController
  abortController.value = new AbortController()

  // 助手消息的索引和段
  let assistantMsgIndex = -1
  let currentSegments: MessageSegment[] = []
  let currentTextSegmentId = -1

  await scrollToBottom()

  try {
    await chatApi.sendMessageWs(
      userMessage,
      currentSessionId.value || undefined,
      (event) => {
        if (event.type === 'session') {
          // 收到会话 ID
          if (event.session_id) {
            currentSessionId.value = event.session_id
            saveCurrentSession(event.session_id)
          }
        } else if (event.type === 'step_start') {
          // 新步骤开始：结束上一轮未闭合的工具状态
          for (const seg of currentSegments) {
            if (seg.type === 'tool' && seg.status === 'running') {
              seg.status = 'done'
              if (!seg.result) seg.result = '（已结束）'
            }
          }
          // 新步骤开始 - 创建新的文本段
          currentTextSegmentId = Date.now()
          currentSegments.push({
            type: 'text',
            id: currentTextSegmentId,
            content: ''
          })

          // 如果还没有助手消息，创建一个
          if (assistantMsgIndex === -1) {
            assistantMsgIndex = messages.value.length
            messages.value.push({
              id: Date.now(),
              role: 'assistant',
              content: '',
              timestamp: new Date(),
              segments: currentSegments
            })
          } else {
            updateMessageSegments(assistantMsgIndex, currentSegments)
          }
          scrollToBottom()
        } else if (event.type === 'chunk' && event.content) {
          // 更新当前文本段
          const textSegment = currentSegments.find(s => s.type === 'text' && s.id === currentTextSegmentId) as TextSegment | undefined
          if (textSegment) {
            textSegment.content += event.content
            updateMessageSegments(assistantMsgIndex, currentSegments)
          }
          scrollToBottom()
        } else if (event.type === 'tool_start') {
          // 工具调用开始 - 创建工具段
          currentSegments.push({
            type: 'tool',
            id: Date.now(),
            tool: event.tool || '',
            args: event.args || {},
            status: 'running'
          })

          // 如果还没有助手消息，创建一个
          if (assistantMsgIndex === -1) {
            assistantMsgIndex = messages.value.length
            messages.value.push({
              id: Date.now(),
              role: 'assistant',
              content: '',
              timestamp: new Date(),
              segments: currentSegments
            })
          } else {
            updateMessageSegments(assistantMsgIndex, currentSegments)
          }
          scrollToBottom()
        } else if (event.type === 'tool_finish') {
          // 工具调用结束 - 查找或创建工具段
          const lastToolSegment = [...currentSegments].reverse().find(s => s.type === 'tool' && s.status === 'running') as ToolSegment | undefined
          if (lastToolSegment) {
            // 更新现有的运行中工具
            lastToolSegment.result = event.result
            lastToolSegment.status = 'done'
          } else {
            // 没有对应的 tool_start，直接添加为完成的工具
            currentSegments.push({
              type: 'tool',
              id: Date.now(),
              tool: event.tool || '',
              args: {},
              result: event.result,
              status: 'done'
            })
          }
          updateMessageSegments(assistantMsgIndex, currentSegments)
          scrollToBottom()
        } else if (event.type === 'context_usage' && event.context_usage) {
          contextUsage.value = event.context_usage
          if (event.context_usage.compressing) {
            contextCompressing.value = true
          } else {
            contextCompressing.value = false
          }
        } else if (event.type === 'api_usage' && event.api_usage) {
          apiUsage.value = event.api_usage
        } else if (event.type === 'compression') {
          contextCompressing.value = event.compression?.status !== 'done'
        } else if (event.type === 'interrupt') {
          pendingApproval.value = {
            runId: event.run_id || '',
            tool: event.tool || '未知工具',
            args: event.args || {},
            reason: event.reason || '此工具需要您的确认',
          }
        } else if (event.type === 'error') {
          const cancelled = Boolean(event.error?.includes('已取消'))
          if (cancelled) {
            finalizeRunningToolSegments(currentSegments, true)
            if (assistantMsgIndex >= 0) {
              updateMessageSegments(assistantMsgIndex, currentSegments)
            }
            contextCompressing.value = false
            pendingApproval.value = null
          } else {
            message.error(event.error || '发送消息失败')
          }
        } else if (event.type === 'done') {
          finalizeRunningToolSegments(currentSegments)
          // 兜底：若流式期间未收到任何文本（如未触发逐字流式），用最终内容渲染回复，
          // 避免出现「思考完需刷新才看到回复」的问题。
          if (event.content) {
            const hasStreamedText = currentSegments.some(
              (s) => s.type === 'text' && s.content && s.content.trim()
            )
            if (!hasStreamedText) {
              const lastText = [...currentSegments].reverse().find(
                (s) => s.type === 'text'
              ) as TextSegment | undefined
              if (lastText) {
                lastText.content = event.content
              } else {
                currentSegments.push({ type: 'text', id: Date.now(), content: event.content })
              }
              if (assistantMsgIndex === -1) {
                assistantMsgIndex = messages.value.length
                messages.value.push({
                  id: Date.now(),
                  role: 'assistant',
                  content: '',
                  timestamp: new Date(),
                  segments: currentSegments,
                })
              }
            }
          }
          if (assistantMsgIndex >= 0) {
            updateMessageSegments(assistantMsgIndex, currentSegments)
          }
          contextCompressing.value = false
          // 完成
          if (event.session_id) {
            currentSessionId.value = event.session_id
          }
          // 对话结束后重新获取助手名字（可能在对话中更新了 IDENTITY.md）
          configApi.getAgentInfo().then(agentInfo => {
            if (agentInfo.name) {
              assistantName.value = agentInfo.name
            }
          }).catch(() => {
            // 忽略错误，保持当前名字
          })
          pendingApproval.value = null
        }
      },
      abortController.value.signal
    )

    await scrollToBottom()
  } catch (error: unknown) {
    // 如果是用户主动取消，不显示错误
    if (error instanceof Error && error.name === 'AbortError') {
      console.log('用户取消了请求')
      // 取消时保留已生成的内容
    } else {
      console.error('发送消息失败:', error)
      message.error('发送消息失败')
      // 移除用户消息
      messages.value.pop()
    }
  } finally {
    loading.value = false
    abortController.value = null
  }
}

const createNewSession = async () => {
  try {
    const res = await sessionApi.create()
    saveCurrentSession(res.session_id)
    router.push({ name: 'chat', query: { session: res.session_id } })
  } catch (error) {
    message.error('新建会话失败')
  }
}
</script>

<template>
  <div class="chat-view">
    <!-- 消息区域 -->
    <div class="chat-messages" ref="messagesContainer" @scroll="handleScroll">
      <!-- 初始化加载状态 -->
      <div v-if="initializing" class="empty-state">
        <MailinLogo :size="100" logo-class="empty-icon loading" />
        <p class="empty-hint">加载中...</p>
      </div>
      <template v-else-if="messages.length > 0">
        <!-- 加载更早消息入口 -->
        <div v-if="hasMoreToRender" class="load-more" @click="loadMoreMessages">
          <LoadingOutlined v-if="loadingMore" />
          <span>{{ loadingMore ? '加载中...' : '加载更早的消息' }}</span>
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
                  <div v-if="segment.type === 'text' && segment.content" class="message-bubble">
                    <div
                      class="message-text"
                      v-html="renderMarkdown(segment.content)"
                    ></div>
                  </div>
                  <!-- 工具调用段 - 只显示非隐藏的工具 -->
                  <div
                    v-if="segment.type === 'tool' && !getToolConfig(segment.tool).hidden"
                    :class="['tool-card', segment.status]"
                  >
                    <div
                      class="tool-header"
                      @click="segment.status !== 'running' && toggleToolCollapse(segment.id)"
                    >
                      <span class="tool-icon">{{ getToolConfig(segment.tool).icon }}</span>
                      <span class="tool-name">
                        <template v-if="!isToolExpanded(segment.id)">使用了</template>
                        {{ getToolConfig(segment.tool).name }}
                      </span>
                      <Tag v-if="segment.status === 'running'" color="processing" class="tool-tag">
                        <LoadingOutlined /> 执行中
                      </Tag>
                      <Tag v-else-if="segment.status === 'done'" color="success" class="tool-tag">完成</Tag>
                      <Tag v-else-if="segment.status === 'error'" color="error" class="tool-tag">失败</Tag>
                      <span
                        v-if="segment.status !== 'running'"
                        class="collapse-indicator"
                      >
                        {{ isToolExpanded(segment.id) ? '▼' : '▶' }}
                      </span>
                    </div>
                    <!-- 展开后显示入参和结果 -->
                    <div v-if="isToolExpanded(segment.id)" class="tool-details">
                      <!-- 入参 -->
                      <div v-if="segment.args && Object.keys(segment.args).length > 0" class="tool-args">
                        <div class="tool-detail-label">入参</div>
                        <pre class="tool-detail-content">{{ formatToolArgs(segment.args) }}</pre>
                      </div>
                      <!-- 结果 -->
                      <div v-if="segment.result" class="tool-result-wrapper">
                        <div class="tool-detail-label">结果</div>
                        <pre class="tool-detail-content">{{ formatToolResult(segment.result) }}</pre>
                      </div>
                    </div>
                  </div>
                </template>
              </template>
              <!-- 如果没有分段，显示普通内容（历史消息） -->
              <div v-else-if="msg.content" class="message-bubble">
                <div
                  class="message-text"
                  v-html="renderMarkdown(msg.content)"
                ></div>
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
        <p class="empty-hint">发送消息开始对话</p>
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

    <!-- 工具审批弹窗 -->
    <Modal
      :open="!!pendingApproval"
      title="工具执行确认"
      :closable="false"
      :mask-closable="false"
      :footer="null"
      centered
    >
      <p v-if="pendingApproval">{{ pendingApproval.reason }}</p>
      <p v-if="pendingApproval" class="approval-tool-name">
        工具：<strong>{{ pendingApproval.tool }}</strong>
      </p>
      <pre v-if="pendingApproval && Object.keys(pendingApproval.args).length" class="approval-args">{{ formatToolArgs(pendingApproval.args) }}</pre>
      <div class="approval-actions">
        <Button danger @click="handleApproval('deny')">拒绝</Button>
        <Button type="primary" @click="handleApproval('allow')">允许执行</Button>
      </div>
    </Modal>

    <!-- 输入区域 -->
    <div class="chat-input-wrapper">
      <div class="chat-input">
        <!-- 上下文用量圆环 -->
        <Tooltip
          v-if="contextUsage"
          placement="top"
          overlay-class-name="context-usage-tooltip"
          :overlay-inner-style="{ textAlign: 'left', padding: '8px 12px', maxWidth: '280px' }"
        >
          <template #title>
            <div class="context-usage-tooltip-lines">
              <div
                v-for="(line, index) in contextUsageLines"
                :key="index"
                class="context-usage-tooltip-line"
              >
                {{ line }}
              </div>
            </div>
          </template>
          <div
            class="context-usage-ring"
            :class="[contextUsageLevel, { compressing: contextCompressing }]"
            :aria-label="contextUsageTooltip"
          >
            <svg :width="RING_SIZE" :height="RING_SIZE" :viewBox="`0 0 ${RING_SIZE} ${RING_SIZE}`">
              <circle
                class="ring-track"
                :cx="RING_SIZE / 2"
                :cy="RING_SIZE / 2"
                :r="RING_RADIUS"
                fill="none"
                stroke-width="3"
              />
              <circle
                class="ring-progress"
                :cx="RING_SIZE / 2"
                :cy="RING_SIZE / 2"
                :r="RING_RADIUS"
                fill="none"
                stroke-width="3"
                :stroke="contextRingColor"
                :stroke-dasharray="RING_CIRCUMFERENCE"
                :stroke-dashoffset="contextRingOffset"
                stroke-linecap="round"
                :transform="`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`"
              />
            </svg>
            <span class="ring-label">{{ contextUsagePercent }}%</span>
          </div>
        </Tooltip>
        <!-- 输入框 -->
        <Input.TextArea
          v-model:value="inputMessage"
          placeholder="输入消息... (Enter 发送, Shift+Enter 换行)"
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

/* Markdown 样式 */
.message-text :deep(p) {
  margin: 0;
}

.message-text :deep(p + p) {
  margin-top: 8px;
}

.message-text :deep(code) {
  background-color: rgba(0, 0, 0, 0.05);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

.message-text :deep(pre) {
  background-color: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 8px 0;
}

.message-text :deep(pre code) {
  background-color: transparent;
  padding: 0;
}

.message-text :deep(ul),
.message-text :deep(ol) {
  margin: 8px 0;
  padding-left: 20px;
}

.message-text :deep(blockquote) {
  border-left: 3px solid var(--color-primary);
  padding-left: 12px;
  margin: 8px 0;
  color: var(--color-text-secondary);
}

.message-text :deep(a) {
  color: var(--color-primary);
  text-decoration: underline;
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

/* 工具审批 */
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

/* 输入区域 */
.chat-input-wrapper {
  padding: 16px 24px 32px;
  background-color: var(--color-surface);
  border-top: 1px solid var(--color-border);
}

.chat-input {
  display: flex;
  gap: 12px;
  align-items: center;
  max-width: 800px;
  margin: 0 auto;
}

.context-usage-ring {
  position: relative;
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  cursor: default;
}

.context-usage-ring.compressing {
  animation: ring-pulse 1.2s ease-in-out infinite;
}

.context-usage-ring .ring-track {
  stroke: var(--color-border);
}

.context-usage-ring .ring-progress {
  transition: stroke-dashoffset 0.35s ease, stroke 0.25s ease;
}

.context-usage-ring .ring-label {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 600;
  color: var(--color-text-secondary);
  line-height: 1;
  pointer-events: none;
}

.context-usage-ring.warning .ring-label {
  color: #b8860b;
}

.context-usage-ring.danger .ring-label {
  color: var(--color-primary);
}

@keyframes ring-pulse {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.75;
    transform: scale(1.05);
  }
}

.context-usage-tooltip-line + .context-usage-tooltip-line {
  margin-top: 4px;
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
