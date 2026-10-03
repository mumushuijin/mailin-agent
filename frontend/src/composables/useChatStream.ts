import type { Ref } from 'vue'
import { message } from 'ant-design-vue'
import { configApi } from '@/api/config'
import { chatWs } from '@/api/ws'
import type { ApiUsage, ContextUsage, StreamEvent } from '@/api/chat'
import type {
  ChatUiMessage,
  AgentStateStream,
  MessageSegment,
  NarrativeNode,
  PendingApproval,
  PendingAskUser,
  SessionTodoItem,
  TextSegment,
  ToolSegment,
} from '@/types/chat-ui'
import {
  advanceAgentState,
  attachAgentStateMessage,
  finalizeAgentState,
  findRunningToolSegment,
  isTerminalAgentStatus,
  mapStreamEventToAgentStatus,
  redactToolInputs,
  shouldAcceptAgentEvent,
  setAgentStateSyncing,
  toolExecutionFromSegment,
  upsertToolExecution,
} from '@/utils/agentStateStream'
import { StreamEventInbox } from '@/utils/streamEventInbox'
import { toolResultLooksLikeError } from '@/utils/toolDisplay'

export interface ChatStreamState {
  messages: Ref<ChatUiMessage[]>
  currentSessionId: Ref<string | null>
  contextUsage: Ref<ContextUsage | null>
  apiUsage: Ref<ApiUsage | null>
  contextCompressing: Ref<boolean>
  activeStage: Ref<string | null>
  agentState?: Ref<AgentStateStream | null>
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

interface StreamGuard {
  runId: string | null
  lastSeq: number
  eventIds: Set<string>
  retiredRunIds: Set<string>
  lastRevision: number
  terminal: boolean
}

const streamGuards = new WeakMap<object, StreamGuard>()

function acceptAgentEvent(ctx: ChatStreamState, event: StreamEvent): boolean {
  const runId = event.run_id || event.state?.scope.run_id || null
  const guard = streamGuards.get(ctx) || {
    runId,
    lastSeq: 0,
    eventIds: new Set<string>(),
    retiredRunIds: new Set<string>(),
    lastRevision: -1,
    terminal: false,
  }

  if (guard.runId && runId && guard.runId !== runId) {
    if (event.type === 'session' && !guard.retiredRunIds.has(runId)) {
      guard.retiredRunIds.add(guard.runId)
      guard.runId = runId
      guard.lastSeq = 0
      guard.eventIds.clear()
      guard.lastRevision = -1
      guard.terminal = false
    } else {
      return false
    }
  }
  const eventId = event.event_id
  if (eventId && guard.eventIds.has(eventId)) return false
  if (guard.terminal) return false
  const revision = event.state?.state_revision
  if (typeof revision === 'number' && revision < guard.lastRevision) return false

  const seq = typeof event.seq === 'number' ? event.seq : undefined
  if (seq !== undefined && seq > guard.lastSeq + 1 && guard.lastSeq > 0) {
    event.gap_detected = true
  }
  if (seq !== undefined && seq < guard.lastSeq) return false

  guard.runId = runId || guard.runId
  if (seq !== undefined) guard.lastSeq = Math.max(guard.lastSeq, seq)
  if (typeof revision === 'number') guard.lastRevision = Math.max(guard.lastRevision, revision)
  if (eventId) guard.eventIds.add(eventId)
  if (event.type === 'done' || event.type === 'error') guard.terminal = true
  streamGuards.set(ctx, guard)
  return true
}

function ensureAgentState(ctx: ChatStreamState, event: StreamEvent): AgentStateStream | null {
  const state = ctx.agentState?.value || null
  if (!state) return null
  if (!shouldAcceptAgentEvent(state, event)) return null
  const nextStatus = mapStreamEventToAgentStatus(event)
  if (isTerminalAgentStatus(state.status) && !isTerminalAgentStatus(nextStatus)) return null
  if (event.run_id && !state.runId) state.runId = event.run_id
  const confirmedSeq = typeof event.state?.state_revision === 'number'
    ? event.state.state_revision
    : typeof event.seq === 'number'
      ? event.seq
      : undefined
  if (typeof confirmedSeq === 'number') state.confirmedSeq = confirmedSeq
  if (typeof event.step === 'number') state.currentStep = event.step
  if (typeof event.max_steps === 'number') state.maxSteps = event.max_steps
  return state
}

function projectCanonicalState(ctx: ChatStreamState, event: StreamEvent, state: AgentStateStream | null) {
  const canonical = event.state
  if (!canonical) return
  const step = canonical.current_step
  if (state) {
    if (typeof step.index === 'number') state.currentStep = step.index
    state.maxSteps = canonical.max_step_every_run
    if (canonical.terminal_reason) state.terminalLabel = canonical.terminal_reason
    if (canonical.run_status === 'waiting_user') state.requiresUserAction = true
    const stepId = canonical.scope.step_id
    if (!stepId) return
    const nodeId = `checkpoint:${stepId}`
    const stepName = typeof step.step_name === 'string' ? step.step_name : 'agent'
    const stepStatus = step.step_status === 'failed' ? 'error'
      : step.step_status === 'cancelled' ? 'cancelled'
        : step.step_status === 'waiting_user' ? 'awaiting_user'
          : stepStatusIsTerminal(step.step_status) ? 'observing' : 'reasoning'
    let checkpointNode = state.nodes.find((node) => node.id === nodeId)
    if (!checkpointNode) {
      checkpointNode = {
        id: nodeId,
        stepId,
        kind: stepName === 'tools' ? 'tool' : 'phase',
        status: stepStatus,
        label: stepName === 'tools' ? '工具执行' : 'Agent 执行',
        startedAt: typeof step.started_at === 'number' ? step.started_at * 1000 : Date.now(),
        active: !stepStatusIsTerminal(step.step_status),
      }
      state.nodes.push(checkpointNode)
    }
    checkpointNode.status = stepStatus
    checkpointNode.active = !stepStatusIsTerminal(step.step_status)
    checkpointNode.summary = typeof step.input_summary === 'string' ? step.input_summary.slice(0, 500) : checkpointNode.summary
    if (typeof step.ended_at === 'number') checkpointNode.endedAt = step.ended_at * 1000
    if (typeof step.duration_ms === 'number') checkpointNode.durationMs = step.duration_ms
    const currentNode = state.nodes[state.nodes.length - 1]
    if (currentNode && currentNode.active && currentNode.summary === undefined) {
      currentNode.summary = typeof step.input_summary === 'string' ? step.input_summary.slice(0, 500) : undefined
    }
    for (const call of Array.isArray(step.tool_calls) ? step.tool_calls : []) {
      if (!call || typeof call !== 'object') continue
      const item = call as Record<string, unknown>
      if (typeof item.call_id !== 'string' || typeof item.tool_name !== 'string') continue
      const startedAt = typeof item.started_at === 'number' ? item.started_at : Date.now()
      const status = item.status === 'success' ? 'done'
        : item.status === 'failed' ? 'error'
          : item.status === 'cancelled' ? 'cancelled'
            : item.status === 'denied' ? 'policy_denied' : 'running'
      upsertToolExecution(state, {
        toolId: item.call_id,
        toolName: item.tool_name,
        status,
        inputs: redactToolInputs(item.input && typeof item.input === 'object' ? item.input as Record<string, unknown> : undefined),
        outputs: typeof item.output_summary === 'string' ? item.output_summary.slice(0, 500) : null,
        progressMessage: null,
        startedAt,
        endedAt: typeof item.ended_at === 'number' ? item.ended_at : undefined,
        redacted: true,
      })
    }
  }
  const todos = canonical.task_details?.todos
  if (Array.isArray(todos)) {
    ctx.sessionTodos.value = todos as SessionTodoItem[]
    if (state) {
      state.todoTotal = todos.length
      state.todoCompleted = todos.filter((todo) => (
        typeof todo === 'object' && todo !== null && (todo as { status?: string }).status === 'completed'
      )).length
    }
  }
  if (typeof step.index === 'number' && state) state.currentStep = step.index
}

function stepStatusIsTerminal(value: unknown): boolean {
  return ['completed', 'failed', 'cancelled', 'skipped'].includes(String(value || ''))
}

function syncAgentStatus(ctx: ChatStreamState, event: StreamEvent, summary?: string): AgentStateStream | null {
  const state = ensureAgentState(ctx, event)
  projectCanonicalState(ctx, event, state)
  const status = mapStreamEventToAgentStatus(event)
  if (state && status) advanceAgentState(state, status, Date.now(), summary)
  return state
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
  options: { skipEnvelopeGuard?: boolean } = {},
): { assistantMsgIndex: number; currentTextSegmentId: number } {
  if (!options.skipEnvelopeGuard && !acceptAgentEvent(ctx, event)) return session
  if (event.type === 'state_sync' && event.state) {
    const state = ensureAgentState(ctx, event)
    projectCanonicalState(ctx, event, state)
    if (state) setAgentStateSyncing(state, false)
    if (ctx.activeStage.value === 'synchronizing') ctx.activeStage.value = null
    return session
  }
  const currentState = ctx.agentState?.value || null
  const eventStatus = mapStreamEventToAgentStatus(event)
  if (currentState && isTerminalAgentStatus(currentState.status) && !isTerminalAgentStatus(eventStatus)) {
    return session
  }
  const gapDetected = Boolean(event.gap_detected)
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
    const state = ctx.agentState?.value
    const messageId = ctx.messages.value[assistantMsgIndex]?.id
    if (state && typeof messageId === 'number') attachAgentStateMessage(state, messageId)
  }

  if (event.type === 'session') {
    syncAgentStatus(ctx, event)
    if (event.session_id) {
      ctx.currentSessionId.value = event.session_id
      ctx.saveCurrentSession(event.session_id)
    }
  } else if (event.type === 'stage') {
    syncAgentStatus(ctx, event)
    const status = event.status || 'started'
    if (status === 'started' || status === 'background' || status === 'degraded') {
      ctx.activeStage.value = event.stage || null
    } else if (ctx.activeStage.value === event.stage) {
      ctx.activeStage.value = null
    }
  } else if (event.type === 'step_start') {
    syncAgentStatus(ctx, event)
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
  } else if (event.type === 'step_finish') {
    syncAgentStatus(ctx, event)
  } else if (event.type === 'chunk' && event.content) {
    syncAgentStatus(ctx, event)
    const textSegment = currentSegments.find(
      (s) => s.type === 'text' && s.id === currentTextSegmentId,
    ) as TextSegment | undefined
    if (textSegment) {
      textSegment.content += event.content
      ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    }
    ctx.scrollToBottom()
  } else if (event.type === 'tool_start') {
    const state = syncAgentStatus(ctx, event)
    const toolCallId = event.tool_call_id || `tool:${Date.now()}`
    const startedAt = Date.now()
    ctx.activeStage.value = 'tool_batch'
    const existingTool = currentSegments.find(
      (segment): segment is ToolSegment => segment.type === 'tool' && segment.toolCallId === toolCallId,
    )
    const toolSegment = existingTool || {
      type: 'tool' as const,
      id: Date.now(),
      tool: event.tool || '',
      args: event.args || {},
      toolCallId,
      startedAt,
      status: 'running' as const,
    }
    if (!existingTool) currentSegments.push(toolSegment)
    if (state) {
      upsertToolExecution(state, {
        toolId: toolCallId,
        toolName: event.tool || '',
        status: 'running',
        inputs: redactToolInputs(event.args),
        outputs: null,
        progressMessage: null,
        startedAt: toolSegment.startedAt || startedAt,
        redacted: true,
      })
      const node = state.nodes[state.nodes.length - 1]
      if (node?.status === 'tool_calling') {
        node.toolIds = Array.from(new Set([...(node.toolIds || []), toolCallId]))
      }
    }
    ensureAssistant()
    ctx.scrollToBottom()
  } else if (event.type === 'tool_finish') {
    const state = syncAgentStatus(ctx, event)
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

    const exactToolSegment = event.tool_call_id
      ? currentSegments.find(
          (segment): segment is ToolSegment => segment.type === 'tool' && segment.toolCallId === event.tool_call_id,
        )
      : undefined
    const lastToolSegment = exactToolSegment || findRunningToolSegment(currentSegments, event.tool_call_id, event.tool)
    if (lastToolSegment) {
      lastToolSegment.result = event.result
      lastToolSegment.endedAt = Date.now()
      lastToolSegment.reasonCode = event.reason_code || null
      lastToolSegment.toolResultRef = event.result_ref || null
      lastToolSegment.toolResultTruncated = Boolean(event.result_truncated)
      lastToolSegment.status = policyDenied
        ? 'policy_denied'
        : toolResultLooksLikeError(event.result)
          ? 'error'
          : 'done'
      lastToolSegment.snapshotId = snapshotId
      lastToolSegment.undoStatus = undoStatus
      if (state) upsertToolExecution(state, toolExecutionFromSegment(lastToolSegment))
    } else {
      const endedAt = Date.now()
      currentSegments.push({
        type: 'tool',
        id: Date.now(),
        tool: event.tool || '',
        args: {},
        toolCallId: event.tool_call_id || `tool:${Date.now()}`,
        result: event.result,
        startedAt: endedAt,
        endedAt,
        reasonCode: event.reason_code || null,
        status: policyDenied
          ? 'policy_denied'
          : toolResultLooksLikeError(event.result)
            ? 'error'
            : 'done',
        snapshotId,
        undoStatus,
      })
      if (state) {
        const segment = currentSegments[currentSegments.length - 1]
        if (segment?.type === 'tool') upsertToolExecution(state, toolExecutionFromSegment(segment))
      }
    }
    ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    ctx.scrollToBottom()
  } else if (event.type === 'tool_progress') {
    const state = syncAgentStatus(ctx, event)
    ctx.activeStage.value = 'tool_batch'
    const runningTool = findRunningToolSegment(currentSegments, event.tool_call_id, event.tool)
    if (runningTool) {
      runningTool.progressMessage = event.message || event.result || null
      if (state) {
        upsertToolExecution(state, {
          ...toolExecutionFromSegment(runningTool),
          progressMessage: runningTool.progressMessage,
        })
      }
      if (assistantMsgIndex >= 0) ctx.updateMessageSegments(assistantMsgIndex, currentSegments)
    }
  } else if (event.type === 'context_usage' && event.context_usage) {
    ctx.contextUsage.value = event.context_usage
    ctx.contextCompressing.value = Boolean(event.context_usage.compressing)
  } else if (event.type === 'api_usage' && event.api_usage) {
    ctx.apiUsage.value = event.api_usage
  } else if (event.type === 'compression') {
    syncAgentStatus(ctx, event)
    ctx.contextCompressing.value = event.compression?.status !== 'done'
    ctx.activeStage.value = ctx.contextCompressing.value ? 'context_prepare' : null
  } else if (event.type === 'todo' && event.todos) {
    ctx.sessionTodos.value = event.todos
    const state = ensureAgentState(ctx, event)
    if (state) {
      state.todoTotal = event.todos.length
      state.todoCompleted = event.todos.filter((todo) => todo.status === 'completed').length
    }
  } else if (event.type === 'interrupt') {
    const state = syncAgentStatus(ctx, event)
    if (state) state.requiresUserAction = true
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
        stepId: event.state?.scope.step_id ?? undefined,
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
        stepId: event.state?.scope.step_id ?? undefined,
        reasonCode: event.reason_code,
        preview: preview || null,
      }
    }
  } else if (event.type === 'error') {
    const state = syncAgentStatus(ctx, event)
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
    if (state) {
      finalizeAgentState(state, cancelled ? 'cancelled' : 'error', cancelled ? '本轮已取消' : '本轮失败')
    }
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
    const state = syncAgentStatus(ctx, event)
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
    if (state) {
      finalizeAgentState(
        state,
        event.cancelled ? 'cancelled' : isHandoffDone ? 'handoff' : 'completed',
        isHandoffDone ? '已交还用户' : event.cancelled ? '本轮已取消' : '本轮完成',
      )
    }
  }

  if (gapDetected && event.type !== 'done' && event.type !== 'error') {
    ctx.activeStage.value = 'synchronizing'
    if (ctx.agentState?.value) setAgentStateSyncing(ctx.agentState.value, true)
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
  const inbox = new StreamEventInbox()
  const readyQueue: StreamEvent[] = []
  const session = {
    assistantMsgIndex: -1,
    currentSegments: [] as MessageSegment[],
    currentTextSegmentId: -1,
  }
  let scheduled = false
  let gapTimer: ReturnType<typeof setTimeout> | null = null
  let syncRequested = false

  const enqueueDrain = (drain: ReturnType<StreamEventInbox['flush']>) => {
    if (drain.events.length) readyQueue.push(...drain.events)
    if (drain.events.length === 0 && drain.waitingForGap) {
      ctx.activeStage.value = 'synchronizing'
      if (ctx.agentState?.value) setAgentStateSyncing(ctx.agentState.value, true)
    }
    if (drain.gapDetected && !syncRequested) {
      const runId = inbox.currentRunId
      const sessionId = ctx.currentSessionId.value
      if (runId && sessionId) {
        syncRequested = true
        chatWs.requestStateSync(runId, sessionId)
      }
    }
  }

  const coalesceReadyEvents = (events: StreamEvent[]): StreamEvent[] => {
    const merged: StreamEvent[] = []
    for (const event of events) {
      const previous = merged[merged.length - 1]
      if (previous?.type === 'chunk' && event.type === 'chunk') {
        merged[merged.length - 1] = {
          ...event,
          content: `${previous.content || ''}${event.content || ''}`,
        }
        continue
      }
      if (
        previous?.type === 'tool_progress'
        && event.type === 'tool_progress'
        && previous.tool_call_id === event.tool_call_id
        && previous.tool === event.tool
      ) {
        merged[merged.length - 1] = event
        continue
      }
      if (
        previous?.type === 'stage'
        && event.type === 'stage'
        && previous.stage === event.stage
      ) {
        merged[merged.length - 1] = event
        continue
      }
      if (
        previous
        && previous.type === event.type
        && (event.type === 'context_usage' || event.type === 'api_usage' || event.type === 'session_token_stats')
      ) {
        merged[merged.length - 1] = event
        continue
      }
      merged.push(event)
    }
    return merged
  }

  const applyReadyQueue = (drain: ReturnType<StreamEventInbox['flush']>) => {
    if (!drain.syncing && ctx.activeStage.value === 'synchronizing') {
      ctx.activeStage.value = null
      if (ctx.agentState?.value) setAgentStateSyncing(ctx.agentState.value, false)
    }

    for (const event of coalesceReadyEvents(readyQueue.splice(0))) {
      if (event.type === 'state_sync') syncRequested = false
      const next = applyChatStreamEvent(event, ctx, session, { skipEnvelopeGuard: true })
      session.assistantMsgIndex = next.assistantMsgIndex
      session.currentTextSegmentId = next.currentTextSegmentId
    }
  }

  const flush = (force = false) => {
    scheduled = false
    const drain = inbox.flush(Date.now(), force)
    if (gapTimer && (force || !drain.waitingForGap)) {
      globalThis.clearTimeout(gapTimer)
      gapTimer = null
    }
    enqueueDrain(drain)
    if (drain.waitingForGap && !gapTimer) {
      gapTimer = globalThis.setTimeout(() => {
        gapTimer = null
        flush(true)
      }, 100)
    }
    applyReadyQueue(drain)
  }

  const schedule = () => {
    if (scheduled) return
    scheduled = true
    if (typeof window !== 'undefined' && window.requestAnimationFrame) {
      window.requestAnimationFrame(() => flush())
    } else {
      globalThis.setTimeout(flush, 50)
    }
  }

  const handle = (event: StreamEvent) => {
    if (event.type === 'session') syncRequested = false
    const drain = inbox.push(event)
    enqueueDrain(drain)
    const isBatchable = BATCHABLE_EVENT_TYPES.has(event.type)
    if (drain.waitingForGap && !gapTimer) {
      gapTimer = globalThis.setTimeout(() => {
        gapTimer = null
        flush(true)
      }, 100)
    }
    if (isBatchable) {
      schedule()
      return
    }
    flush(true)
  }

  return { handle, flush: () => flush(true) }
}
