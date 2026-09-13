import type { Ref } from 'vue'
import { message } from 'ant-design-vue'
import { configApi } from '@/api/config'
import type { ApiUsage, ContextUsage, StreamEvent } from '@/api/chat'
import type { ChatUiMessage, MessageSegment, PendingApproval, PendingAskUser, SessionTodoItem, TextSegment, ToolSegment } from '@/types/chat-ui'
import { toolResultLooksLikeError } from '@/utils/toolDisplay'

export interface ChatStreamState {
  messages: Ref<ChatUiMessage[]>
  currentSessionId: Ref<string | null>
  contextUsage: Ref<ContextUsage | null>
  apiUsage: Ref<ApiUsage | null>
  contextCompressing: Ref<boolean>
  pendingApproval: Ref<PendingApproval | null>
  pendingAskUser: Ref<PendingAskUser | null>
  sessionTodos: Ref<SessionTodoItem[]>
  assistantName: Ref<string>
  saveCurrentSession: (sessionId: string) => void
  updateMessageSegments: (msgIndex: number, segments: MessageSegment[]) => void
  finalizeRunningToolSegments: (segments: MessageSegment[], cancelled?: boolean) => void
  scrollToBottom: () => void
}

export function applyChatStreamEvent(
  event: StreamEvent,
  ctx: ChatStreamState,
  session: {
    assistantMsgIndex: number
    currentSegments: MessageSegment[]
    currentTextSegmentId: number
  },
): { assistantMsgIndex: number; currentTextSegmentId: number } {
  let { assistantMsgIndex, currentSegments, currentTextSegmentId } = session

  const ensureAssistant = () => {
    if (assistantMsgIndex === -1) {
      assistantMsgIndex = ctx.messages.value.length
      ctx.messages.value.push({
        id: Date.now(),
        role: 'assistant',
        content: '',
        timestamp: new Date(),
        segments: currentSegments,
      })
    } else {
      ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    }
  }

  if (event.type === 'session') {
    if (event.session_id) {
      ctx.currentSessionId.value = event.session_id
      ctx.saveCurrentSession(event.session_id)
    }
  } else if (event.type === 'step_start') {
    for (const seg of currentSegments) {
      if (seg.type === 'tool' && seg.status === 'running') {
        seg.status = 'done'
        if (!seg.result) seg.result = '（已结束）'
      }
    }
    currentTextSegmentId = Date.now()
    currentSegments.push({
      type: 'text',
      id: currentTextSegmentId,
      content: '',
    })
    ensureAssistant()
    ctx.scrollToBottom()
  } else if (event.type === 'chunk' && event.content) {
    const textSegment = currentSegments.find(
      (s) => s.type === 'text' && s.id === currentTextSegmentId,
    ) as TextSegment | undefined
    if (textSegment) {
      textSegment.content += event.content
      ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    }
    ctx.scrollToBottom()
  } else if (event.type === 'tool_start') {
    currentSegments.push({
      type: 'tool',
      id: Date.now(),
      tool: event.tool || '',
      args: event.args || {},
      status: 'running',
    })
    ensureAssistant()
    ctx.scrollToBottom()
  } else if (event.type === 'tool_finish') {
    const lastToolSegment = [...currentSegments]
      .reverse()
      .find((s) => s.type === 'tool' && s.status === 'running') as ToolSegment | undefined
    if (lastToolSegment) {
      lastToolSegment.result = event.result
      lastToolSegment.status = toolResultLooksLikeError(event.result) ? 'error' : 'done'
    } else {
      currentSegments.push({
        type: 'tool',
        id: Date.now(),
        tool: event.tool || '',
        args: {},
        result: event.result,
        status: toolResultLooksLikeError(event.result) ? 'error' : 'done',
      })
    }
    ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    ctx.scrollToBottom()
  } else if (event.type === 'context_usage' && event.context_usage) {
    ctx.contextUsage.value = event.context_usage
    ctx.contextCompressing.value = Boolean(event.context_usage.compressing)
  } else if (event.type === 'api_usage' && event.api_usage) {
    ctx.apiUsage.value = event.api_usage
  } else if (event.type === 'compression') {
    ctx.contextCompressing.value = event.compression?.status !== 'done'
  } else if (event.type === 'todo' && event.todos) {
    ctx.sessionTodos.value = event.todos
  } else if (event.type === 'interrupt') {
    if (event.kind === 'ask_user') {
      ctx.pendingApproval.value = null
      ctx.pendingAskUser.value = {
        runId: event.run_id || '',
        prompt: event.prompt || event.reason || '需要你补充一点信息',
        options: event.options || [],
        allowMultiple: Boolean(event.allow_multiple),
      }
    } else {
      ctx.pendingAskUser.value = null
      ctx.pendingApproval.value = {
        runId: event.run_id || '',
        tool: event.tool || '未知工具',
        args: event.args || {},
        reason: event.reason || '此工具需要您的确认',
      }
    }
  } else if (event.type === 'error') {
    const cancelled = Boolean(event.error?.includes('已取消'))
    if (cancelled) {
      ctx.finalizeRunningToolSegments(currentSegments, true)
      if (assistantMsgIndex >= 0) {
        ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
      }
      ctx.contextCompressing.value = false
      ctx.pendingApproval.value = null
      ctx.pendingAskUser.value = null
    } else {
      message.error(event.error || '发送消息失败')
    }
  } else if (event.type === 'done') {
    ctx.finalizeRunningToolSegments(currentSegments)
    if (event.content) {
      const hasStreamedText = currentSegments.some(
        (s) => s.type === 'text' && s.content && s.content.trim(),
      )
      if (!hasStreamedText) {
        const lastText = [...currentSegments].reverse().find((s) => s.type === 'text') as
          | TextSegment
          | undefined
        if (lastText) {
          lastText.content = event.content
        } else {
          currentSegments.push({ type: 'text', id: Date.now(), content: event.content })
        }
        if (assistantMsgIndex === -1) {
          assistantMsgIndex = ctx.messages.value.length
          ctx.messages.value.push({
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
      ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    }
    ctx.contextCompressing.value = false
    if (event.session_id) {
      ctx.currentSessionId.value = event.session_id
    }
    configApi
      .getAgentInfo()
      .then((agentInfo) => {
        if (agentInfo.name) ctx.assistantName.value = agentInfo.name
      })
      .catch(() => {})
    ctx.pendingApproval.value = null
    ctx.pendingAskUser.value = null
  }

  return { assistantMsgIndex, currentTextSegmentId }
}
