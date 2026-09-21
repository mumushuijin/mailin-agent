import type { Ref } from 'vue'
import { message } from 'ant-design-vue'
import { configApi } from '@/api/config'
import type { ApiUsage, ContextUsage, StreamEvent } from '@/api/chat'
import type {
  ChatUiMessage,
  MessageSegment,
  NarrativeNode,
  PendingApproval,
  PendingAskUser,
  SessionTodoItem,
  TextSegment,
  ToolSegment,
} from '@/types/chat-ui'
import { toolResultLooksLikeError } from '@/utils/toolDisplay'

export interface ChatStreamState {
  messages: Ref<ChatUiMessage[]>
  currentSessionId: Ref<string | null>
  contextUsage: Ref<ContextUsage | null>
  apiUsage: Ref<ApiUsage | null>
  contextCompressing: Ref<boolean>
  activeStage: Ref<string | null>
  pendingApproval: Ref<PendingApproval | null>
  pendingAskUser: Ref<PendingAskUser | null>
  sessionTodos: Ref<SessionTodoItem[]>
  assistantName: Ref<string>
  saveCurrentSession: (sessionId: string) => void
  updateMessageSegments: (msgIndex: number, segments: MessageSegment[]) => void
  finalizeRunningToolSegments: (segments: MessageSegment[], cancelled?: boolean) => void
  markRunTerminal: (status: 'done' | 'cancelled' | 'error' | 'handoff') => void
  scrollToBottom: () => void
}

export function buildNarrativeNodes(segments: MessageSegment[]): NarrativeNode[] {
  const nodes: NarrativeNode[] = []

  for (const segment of segments) {
    if (segment.type === 'text') {
      nodes.push({
        id: `text:${segment.id}`,
        kind: segment.presentation === 'system' ? 'system_result' : 'agent_text',
        segmentId: segment.id,
      })
      continue
    }

    nodes.push({
      id: `tool:${segment.id}`,
      kind: 'action',
      segmentId: segment.id,
      status: segment.status,
    })
  }

  return nodes
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
  } else if (event.type === 'stage') {
    const status = event.status || 'started'
    if (status === 'started' || status === 'background' || status === 'degraded') {
      ctx.activeStage.value = event.stage || null
    } else if (ctx.activeStage.value === event.stage) {
      ctx.activeStage.value = null
    }
  } else if (event.type === 'step_start') {
    ctx.activeStage.value = 'model_stream'
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
    ctx.activeStage.value = 'tool_batch'
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
    const resultText = event.result || ''
    const snapshotMatch = resultText.match(/\[snapshot_id=([0-9a-f]+)\]/i)
    const snapshotId = snapshotMatch?.[1] || null
    const policyDenied = /\[policy_denied\]/i.test(resultText)
    let undoStatus: ToolSegment['undoStatus'] = snapshotId ? 'available' : null
    if (/跨 session|其他会话/i.test(resultText)) undoStatus = 'cross_session'
    else if (/冲突/i.test(resultText)) undoStatus = 'conflict'
    else if (/过期|不存在/i.test(resultText) && event.tool === 'undo_file_change') {
      undoStatus = /过期/.test(resultText) ? 'expired' : 'unavailable'
    }

    const lastToolSegment = [...currentSegments]
      .reverse()
      .find((s) => s.type === 'tool' && s.status === 'running') as ToolSegment | undefined
    if (lastToolSegment) {
      lastToolSegment.result = event.result
      lastToolSegment.reasonCode = event.reason_code || null
      lastToolSegment.status = policyDenied
        ? 'policy_denied'
        : toolResultLooksLikeError(event.result)
          ? 'error'
          : 'done'
      lastToolSegment.snapshotId = snapshotId
      lastToolSegment.undoStatus = undoStatus
    } else {
      currentSegments.push({
        type: 'tool',
        id: Date.now(),
        tool: event.tool || '',
        args: {},
        result: event.result,
        reasonCode: event.reason_code || null,
        status: policyDenied
          ? 'policy_denied'
          : toolResultLooksLikeError(event.result)
            ? 'error'
            : 'done',
        snapshotId,
        undoStatus,
      })
    }
    ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    ctx.scrollToBottom()
  } else if (event.type === 'tool_progress') {
    ctx.activeStage.value = 'tool_batch'
  } else if (event.type === 'context_usage' && event.context_usage) {
    ctx.contextUsage.value = event.context_usage
    ctx.contextCompressing.value = Boolean(event.context_usage.compressing)
  } else if (event.type === 'api_usage' && event.api_usage) {
    ctx.apiUsage.value = event.api_usage
  } else if (event.type === 'compression') {
    ctx.contextCompressing.value = event.compression?.status !== 'done'
    ctx.activeStage.value = ctx.contextCompressing.value ? 'context_prepare' : null
  } else if (event.type === 'todo' && event.todos) {
    ctx.sessionTodos.value = event.todos
  } else if (event.type === 'interrupt') {
    ctx.activeStage.value = 'waiting_user'
    if (event.kind === 'ask_user') {
      ctx.pendingApproval.value = null
      const mode =
        event.mode === 'handoff_and_stop' ? 'handoff_and_stop' : 'answer_and_continue'
      ctx.pendingAskUser.value = {
        runId: event.run_id || '',
        prompt: event.prompt || event.reason || '需要你补充一点信息',
        options: event.options || [],
        allowMultiple: Boolean(event.allow_multiple),
        interruptId: event.interrupt_id,
        toolCallId: event.tool_call_id,
        mode,
        terminalOnAck: Boolean(event.terminal_on_ack) || mode === 'handoff_and_stop',
      }
    } else {
      ctx.pendingAskUser.value = null
      const preview = event.preview as PendingApproval['preview'] | undefined
      ctx.pendingApproval.value = {
        runId: event.run_id || '',
        tool: event.tool || '未知工具',
        args: event.args || {},
        reason: event.reason || '此工具需要您的确认',
        toolCallId: event.tool_call_id,
        reasonCode: event.reason_code,
        preview: preview || null,
      }
    }
  } else if (event.type === 'error') {
    const cancelled = Boolean(event.cancelled || event.error?.includes('已取消'))
    ctx.markRunTerminal(cancelled ? 'cancelled' : 'error')
    ctx.finalizeRunningToolSegments(currentSegments, true)
    if (assistantMsgIndex >= 0) {
      ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    }
    ctx.contextCompressing.value = false
    ctx.activeStage.value = null
    ctx.pendingApproval.value = null
    ctx.pendingAskUser.value = null
    if (!cancelled) {
      message.error(event.error || '发送消息失败')
    }
  } else if (event.type === 'done') {
    // 审批等待中若误收到普通 done，不要清掉弹窗（后端也不应在 pending interrupt 时发 done）
    const isHandoffDone = Boolean(event.handoff || event.terminal_reason === 'handoff')
    if (
      (ctx.pendingApproval.value || ctx.pendingAskUser.value) &&
      !event.cancelled &&
      !isHandoffDone
    ) {
      return { assistantMsgIndex, currentTextSegmentId }
    }
    const terminal = event.cancelled
      ? 'cancelled'
      : isHandoffDone
        ? 'handoff'
        : 'done'
    ctx.markRunTerminal(terminal)
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
    ctx.activeStage.value = null
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

const BATCHABLE_EVENT_TYPES = new Set<StreamEvent['type']>([
  'chunk',
  'tool_progress',
  'context_usage',
  'api_usage',
  'session_token_stats',
  'stage',
])

export function createChatStreamEventBatcher(
  ctx: ChatStreamState,
): {
  handle: (event: StreamEvent) => void
  flush: () => void
} {
  const queue: StreamEvent[] = []
  const session = {
    assistantMsgIndex: -1,
    currentSegments: [] as MessageSegment[],
    currentTextSegmentId: -1,
  }
  let scheduled = false

  const flush = () => {
    scheduled = false
    while (queue.length) {
      const event = queue.shift()!
      const next = applyChatStreamEvent(event, ctx, session)
      session.assistantMsgIndex = next.assistantMsgIndex
      session.currentTextSegmentId = next.currentTextSegmentId
    }
  }

  const schedule = () => {
    if (scheduled) return
    scheduled = true
    if (typeof window !== 'undefined' && window.requestAnimationFrame) {
      window.requestAnimationFrame(flush)
    } else {
      globalThis.setTimeout(flush, 50)
    }
  }

  const handle = (event: StreamEvent) => {
    queue.push(event)
    if (BATCHABLE_EVENT_TYPES.has(event.type)) {
      schedule()
      return
    }
    flush()
  }

  return { handle, flush }
}
