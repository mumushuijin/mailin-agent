import type { ChatUiMessage, AgentStateNode, AgentStateStream, AgentStatus, ToolExecution, ToolSegment } from '@/types/chat-ui'
import {
  AGENT_STATUS_META,
  createAgentStateStream,
  finalizeAgentState,
} from './agentStateStream'
import { displayContent, toolOutcome } from './historyProjection'
import type { PersistentTimelineState, TimelineActivity, TimelineLifecycleEntity, TimelineMessage, TimelineToolCall } from './eventTimelineReducer'

function timeValue(value: unknown, fallback: number): number {
  if (typeof value === 'number') return value
  if (typeof value === 'string') {
    const parsed = Date.parse(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

function statusForActivity(activity: TimelineActivity): AgentStatus {
  if (activity.status === 'failed') return 'error'
  if (activity.status === 'cancelled') return 'cancelled'
  if (activity.status === 'completed' || activity.status === 'skipped') return 'observing'
  const kind = `${activity.kind} ${activity.label}`.toLowerCase()
  if (kind.includes('plan')) return 'planning'
  if (kind.includes('tool')) return 'tool_calling'
  if (kind.includes('wait') || kind.includes('approval')) return 'awaiting_user'
  if (kind.includes('context')) return 'initializing'
  return 'reasoning'
}

function toolExecution(tool: TimelineToolCall, fallbackTime: number): ToolExecution {
  const attempts = tool.attempts.map((attempt, index) => ({
    attempt: index + 1,
    status: attempt.status,
    started_at: attempt.started_seq,
    finished_at: attempt.settled_seq,
    error: attempt.reason_code ? { code: attempt.reason_code } : undefined,
  }))
  return {
    toolId: tool.call_id,
    toolName: tool.tool_name,
    status: toolOutcome(tool.status),
    inputs: {},
    outputs: tool.result_summary?.slice(0, 500) ?? null,
    startedAt: fallbackTime,
    endedAt: tool.settled_seq,
    redacted: true,
    attempts,
  }
}

function timelineToolSegment(tool: TimelineToolCall): ToolSegment {
  const startedAt = timeValue((tool as unknown as Record<string, unknown>).started_at, tool.started_seq)
  const endedAt = timeValue((tool as unknown as Record<string, unknown>).settled_at, tool.settled_seq ?? startedAt)
  return {
    type: 'tool',
    id: tool.started_seq * 1000,
    tool: tool.tool_name,
    args: {},
    toolCallId: tool.call_id,
    result: tool.result_summary?.slice(0, 500),
    startedAt,
    endedAt: tool.settled_seq === undefined ? undefined : endedAt,
    reasonCode: tool.reason_code ?? null,
    status: toolOutcome(tool.status),
    attempts: tool.attempts.map((attempt, index) => ({
      attempt: index + 1,
      status: attempt.status,
      error: attempt.reason_code ? { code: attempt.reason_code } : undefined,
    })),
  }
}

function assistantOwnerForTool(messages: ChatUiMessage[], tool: TimelineToolCall): ChatUiMessage | undefined {
  const candidates = messages.filter((message) => message.role === 'assistant' && message.turnId === tool.turn_id &&
    message.messageId !== `tool:${tool.call_id}` && (message.ledgerSeq ?? message.id) <= tool.started_seq)
  candidates.sort((left, right) => (left.ledgerSeq ?? left.id) - (right.ledgerSeq ?? right.id))
  return candidates[candidates.length - 1] ?? messages.find((message) =>
    message.role === 'assistant' && message.turnId === tool.turn_id && message.ledgerSeq === undefined &&
    message.messageId !== `tool:${tool.call_id}`,
  )
}

export function buildTimelineAgentStates(
  projection: PersistentTimelineState,
  messages: ChatUiMessage[],
): Record<number, AgentStateStream> {
  const states: Record<number, AgentStateStream> = {}
  const turnToUser = new Map(messages.filter((item) => item.role === 'user' && item.turnId)
    .map((item) => [item.turnId!, item] as const))
  for (const turnId of projection.turn_order) {
    const user = turnToUser.get(turnId)
    if (!user) continue
    const state = createAgentStateStream(user.id, turnId, user.timestamp.getTime())
    state.nodes = []
    const lifecycleNodes: Array<{ seq: number; node: AgentStateNode }> = []
    for (const [kind, map, order] of [
      ['task', projection.tasks, projection.task_order],
      ['step', projection.steps, projection.step_order],
    ] as const) {
      for (const key of order ?? []) {
        const item: TimelineLifecycleEntity | undefined = map?.[key]
        if (!item || item.turn_id !== turnId) continue
        const running = ['running', 'pending', 'waiting_user'].includes(item.status)
        const status: AgentStatus = item.status === 'failed' ? 'error' : item.status === 'cancelled' ? 'cancelled'
          : item.status === 'waiting_user' ? 'awaiting_user' : running
            ? item.node === 'tools' ? 'tool_calling' : kind === 'task' ? 'planning' : 'reasoning' : 'observing'
        const label = item.label || item.title || item.name || item.action ||
          (item.node === 'tools' ? '工具执行' : kind === 'task' ? '任务' : 'Agent 执行')
        lifecycleNodes.push({ seq: item.started_seq, node: {
          id: `${kind}:${kind === 'task' ? item.task_id : item.step_id}`,
          stepId: item.step_id,
          kind: item.node === 'tools' ? 'tool' : 'phase',
          status, label, summary: item.summary,
          startedAt: timeValue(item.started_at, user.timestamp.getTime()),
          endedAt: running ? undefined : timeValue(item.settled_at, user.timestamp.getTime()),
          active: running,
        } })
      }
    }
    const activities = projection.activity_order.map((key) => projection.activities[key])
      .filter((item): item is TimelineActivity => Boolean(item && item.turn_id === turnId))
    for (const activity of activities) {
      const status = statusForActivity(activity)
      const startedAt = timeValue((activity as unknown as Record<string, unknown>).started_at, user.timestamp.getTime())
      const endedAt = timeValue((activity as unknown as Record<string, unknown>).settled_at, startedAt)
      const node: AgentStateNode = {
        id: `activity:${activity.activity_id}`,
        kind: 'phase',
        status,
        label: activity.label || AGENT_STATUS_META[status].label,
        startedAt,
        endedAt: activity.status === 'running' ? undefined : endedAt,
        active: activity.status === 'running',
        summary: activity.summary,
        stepId: activity.parent_activity_id,
      }
      lifecycleNodes.push({ seq: activity.started_seq, node })
    }
    state.nodes = lifecycleNodes.sort((left, right) => left.seq - right.seq).map((item) => item.node)
    for (const key of projection.interaction_order) {
      const interaction = projection.interactions[key]
      if (!interaction || interaction.turn_id !== turnId) continue
      const waiting = interaction.status === 'waiting'
      lifecycleNodes.push({ seq: interaction.opened_seq, node: {
        id: `interaction:${interaction.interaction_id}`, kind: 'phase',
        status: waiting ? 'awaiting_user' : interaction.status === 'failed' ? 'error' : 'observing',
        label: interaction.prompt_summary || (interaction.kind === 'approval' ? '等待审批' : '等待用户'),
        summary: interaction.decision_summary,
        startedAt: user.timestamp.getTime(), endedAt: waiting ? undefined : user.timestamp.getTime(), active: waiting,
      } })
    }
    state.nodes = lifecycleNodes.sort((left, right) => left.seq - right.seq).map((item) => item.node)
    state.requiresUserAction = projection.interaction_order.some((key) => {
      const item = projection.interactions[key]
      return item?.turn_id === turnId && item.status === 'waiting'
    })
    const tasks = Object.values(projection.tasks ?? {}).filter((item) => item.turn_id === turnId)
    state.todoTotal = tasks.length
    state.todoCompleted = tasks.filter((item) => item.status === 'completed').length
    state.currentTask = tasks.find((item) => ['running', 'waiting_user'].includes(item.status))?.task_id ?? null
    state.toolExecutions = projection.tool_order.map((key) => projection.tools[key])
      .filter((item): item is TimelineToolCall => Boolean(item && item.turn_id === turnId))
      .map((item) => toolExecution(item, timeValue((item as unknown as Record<string, unknown>).started_at, user.timestamp.getTime())))
    const turn = projection.turns[turnId]
    if (turn && turn.status !== 'running') {
      const terminal = turn.status === 'failed' ? 'error' : turn.status
      finalizeAgentState(state, terminal, turn.reason_code || AGENT_STATUS_META[terminal].label,
        timeValue((turn as unknown as Record<string, unknown>).settled_at, user.timestamp.getTime()))
    } else {
      const active = [...state.nodes].reverse().find((node) => node.active)
      state.status = state.requiresUserAction ? 'awaiting_user' : active?.status ?? 'reasoning'
    }
    states[user.id] = state
  }
  return states
}

export function reconcileTimelineMessages(
  current: ChatUiMessage[],
  projection: PersistentTimelineState,
): ChatUiMessage[] {
  const ordered = projection.message_order.map((key) => projection.messages[key])
    .filter((item): item is TimelineMessage => Boolean(item))
  const durableIds = new Set(ordered.map((item) => item.message_id))
  const toolIds = new Set(projection.tool_order.map((key) => `tool:${projection.tools[key]?.call_id}`))
  const messages = current.filter((item) => !item.messageId || durableIds.has(item.messageId) || toolIds.has(item.messageId))
  const byMessageId = new Map(messages.filter((item) => item.messageId).map((item) => [item.messageId!, item]))
  for (const item of ordered) {
    const content = displayContent(item.content as never)
    const existing = byMessageId.get(item.message_id)
    if (existing) {
      const tools = (existing.segments || []).filter((segment) => segment.type === 'tool')
      existing.content = content
      existing.turnId = item.turn_id
      existing.ledgerSeq = item.event_seq
      existing.requestId = item.request_id
      existing.responseId = item.response_id
      if (existing.role === 'assistant') existing.segments = [
        ...(content ? [{ type: 'text' as const, id: item.event_seq * 1000, content }] : []),
        ...tools,
      ]
      continue
    }
    const provisional = [...messages].reverse().find((message) => !message.messageId && (
      (item.role === 'assistant' && message.role === 'assistant' && message.turnId === item.turn_id &&
        (!message.requestId || message.requestId === item.request_id)) ||
      (item.role === 'user' && message.role === 'user' && (message.turnId === item.turn_id || (!message.turnId && message.content === content)))
    ))
    if (provisional) {
      provisional.messageId = item.message_id
      provisional.content = content
      provisional.turnId = item.turn_id
      provisional.ledgerSeq = item.event_seq
      provisional.requestId = item.request_id
      provisional.responseId = item.response_id
      provisional.timestamp = new Date(timeValue((item as unknown as Record<string, unknown>).occurred_at, Date.now()))
      const tools = (provisional.segments || []).filter((segment) => segment.type === 'tool')
      provisional.segments = [
        ...(content && provisional.role === 'assistant' ? [{ type: 'text' as const, id: item.event_seq * 1000, content }] : []),
        ...tools,
      ]
      byMessageId.set(item.message_id, provisional)
      continue
    }
    const created: ChatUiMessage = {
      id: item.event_seq,
      messageId: item.message_id,
      role: item.role,
      content,
      timestamp: new Date(timeValue((item as unknown as Record<string, unknown>).occurred_at, Date.now())),
      turnId: item.turn_id,
      ledgerSeq: item.event_seq,
      requestId: item.request_id,
      responseId: item.response_id,
      segments: item.role === 'assistant' && content ? [{ type: 'text', id: item.event_seq * 1000, content }] : undefined,
    }
    messages.push(created)
    byMessageId.set(item.message_id, created)
  }
  const tools = projection.tool_order.map((key) => projection.tools[key]).filter((item): item is TimelineToolCall => Boolean(item))
  for (const tool of tools) {
    const existingOwner = messages.find((message) => (message.segments || []).some(
      (segment) => segment.type === 'tool' && segment.toolCallId === tool.call_id,
    ))
    const existingCall = existingOwner?.segments?.find(
      (segment): segment is ToolSegment => segment.type === 'tool' && segment.toolCallId === tool.call_id,
    )
    if (existingCall) {
      const messageOwner = assistantOwnerForTool(messages, tool)
      if (messageOwner && existingOwner !== messageOwner) {
        existingOwner!.segments = (existingOwner!.segments || []).filter((segment) => segment !== existingCall)
        messageOwner.segments = [...(messageOwner.segments || []), existingCall]
        if (existingOwner!.messageId?.startsWith('tool:') && !existingOwner!.segments?.length) {
          messages.splice(messages.indexOf(existingOwner!), 1)
        }
      }
      existingCall.status = toolOutcome(tool.status)
      existingCall.result = tool.result_summary?.slice(0, 500)
      existingCall.reasonCode = tool.reason_code ?? null
      existingCall.endedAt = tool.settled_seq
      continue
    }
    const owner = assistantOwnerForTool(messages, tool)
    const segment = timelineToolSegment(tool)
    if (owner) {
      owner.segments = [...(owner.segments || []), segment]
      continue
    }
    messages.push({
      id: tool.started_seq,
      messageId: `tool:${tool.call_id}`,
      role: 'assistant',
      content: '',
      timestamp: new Date(tool.started_seq),
      turnId: tool.turn_id,
      ledgerSeq: tool.started_seq,
      segments: [segment],
    })
  }
  messages.sort((left, right) => (left.ledgerSeq ?? left.id) - (right.ledgerSeq ?? right.id))
  return messages
}
