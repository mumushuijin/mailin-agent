export type TurnOutcome = 'completed' | 'failed' | 'cancelled' | 'handoff'
export type ActivityOutcome = 'completed' | 'failed' | 'cancelled' | 'skipped'
export type ToolOutcome = 'running' | 'succeeded' | 'failed' | 'cancelled' | 'denied' | 'skipped' | 'unknown'
export type InteractionOutcome = 'waiting' | 'completed' | 'failed' | 'cancelled' | 'denied' | 'expired'

export interface TimelineCursor {
  branch_id: string
  event_seq: number
  event_id: string
}

export interface TimelineEventEnvelope {
  schema_version: number
  session_id: string
  branch_id: string
  event_seq: number
  event_id: string
  turn_id?: string
  event_type: string
  recorded_at?: string
  occurred_at?: string
  activity_id?: string
  parent_activity_id?: string
  call_id?: string
  attempt_id?: string
  request_id?: string
  interaction_id?: string
  batch_id?: string
  payload: Record<string, unknown>
}

export interface TimelineBranchView {
  branch_id: string
  /** Ancestor branches are capped at their fork cursor; null means the selected live branch. */
  visible_branches: ReadonlyArray<{ branch_id: string; through_event_seq: number | null }>
}

interface EventBase<T extends string, P> {
  schema_version: 3
  session_id: string
  branch_id: string
  event_seq: number
  event_id: string
  turn_id: string
  event_type: T
  recorded_at?: string
  occurred_at?: string
  activity_id?: string
  parent_activity_id?: string
  payload: P
}

export type TimelineLedgerEvent =
  | EventBase<'message.accepted', { message_id: string; content: string; submission_id?: string }>
  | EventBase<'model.response.completed', {
      request_id: string
      response_id: string
      message_id: string
      content: string
      finish_reason?: string
    }>
  | EventBase<'activity.started', { activity_kind: string; label: string; summary?: string }>
  | EventBase<'activity.settled', { status: ActivityOutcome; summary?: string }>
  | EventBase<'tool.attempt.started', {
      call_id: string
      attempt_id: string
      tool_name: string
      batch_id?: string
      input_summary?: string
    }>
  | EventBase<'tool.attempt.settled', {
      call_id: string
      attempt_id: string
      status: Exclude<ToolOutcome, 'running'>
      result_summary?: string
      reason_code?: string
    }>
  | EventBase<'interaction.opened', { interaction_id: string; kind: string; prompt_summary?: string }>
  | EventBase<'interaction.resolved', {
      interaction_id: string
      status: Exclude<InteractionOutcome, 'waiting'>
      decision_summary?: string
    }>
  | EventBase<'turn.settled', { status: TurnOutcome; reason_code?: string; final_message_id?: string }>

export interface TimelineMessage {
  key: string
  message_id: string
  turn_id: string
  role: 'user' | 'assistant'
  content: string | Array<Record<string, unknown>>
  event_seq: number
  event_id?: string
  occurred_at?: string
  recorded_at?: string
  response_id?: string
  request_id?: string
}

export interface TimelineActivity {
  key: string
  activity_id: string
  turn_id: string
  parent_activity_id?: string
  kind: string
  label: string
  summary?: string
  status: 'running' | ActivityOutcome
  started_seq: number
  settled_seq?: number
}

export interface TimelineToolAttempt {
  attempt_id: string
  status: ToolOutcome
  started_seq?: number
  settled_seq?: number
  result_summary?: string
  reason_code?: string
}

export interface TimelineToolCall {
  key: string
  call_id: string
  turn_id: string
  activity_id?: string
  parent_activity_id?: string
  batch_id?: string
  tool_name: string
  input_summary?: string
  status: ToolOutcome
  attempts: TimelineToolAttempt[]
  started_seq: number
  settled_seq?: number
  result_summary?: string
  reason_code?: string
}

export interface TimelineInteraction {
  key: string
  interaction_id: string
  turn_id: string
  activity_id?: string
  kind: string
  prompt_summary?: string
  status: InteractionOutcome
  decision_summary?: string
  opened_seq: number
  resolved_seq?: number
}

export interface TimelineTurn {
  turn_id: string
  status: 'running' | TurnOutcome
  started_seq: number
  settled_seq?: number
  reason_code?: string
  final_message_id?: string
}

export interface TimelineLifecycleEntity {
  key: string
  turn_id: string
  task_id?: string
  step_id?: string
  node?: string
  title?: string
  name?: string
  label?: string
  summary?: string
  action?: string
  status: string
  started_seq: number
  settled_seq?: number
  started_at?: string
  settled_at?: string
}

export type TimelineItemRef =
  | { kind: 'message'; key: string; event_seq: number }
  | { kind: 'activity'; key: string; event_seq: number }
  | { kind: 'tool'; key: string; event_seq: number }
  | { kind: 'interaction'; key: string; event_seq: number }
  | { kind: 'task' | 'step' | 'model_request'; key: string; event_seq: number }

export interface PersistentTimelineState {
  schema_version: 1
  session_id: string
  branch_view: TimelineBranchView
  cursor: TimelineCursor | null
  applied_events: Readonly<Record<string, { branch_id: string; event_seq: number }>>
  turns: Readonly<Record<string, TimelineTurn>>
  turn_order: ReadonlyArray<string>
  messages: Readonly<Record<string, TimelineMessage>>
  message_order: ReadonlyArray<string>
  activities: Readonly<Record<string, TimelineActivity>>
  activity_order: ReadonlyArray<string>
  tools: Readonly<Record<string, TimelineToolCall>>
  tool_order: ReadonlyArray<string>
  interactions: Readonly<Record<string, TimelineInteraction>>
  interaction_order: ReadonlyArray<string>
  tasks?: Readonly<Record<string, TimelineLifecycleEntity>>
  task_order?: ReadonlyArray<string>
  steps?: Readonly<Record<string, TimelineLifecycleEntity>>
  step_order?: ReadonlyArray<string>
  model_requests?: Readonly<Record<string, Record<string, unknown>>>
  model_request_order?: ReadonlyArray<string>
  items: ReadonlyArray<TimelineItemRef>
}

export interface TransientStreamOverlay {
  turn_id: string
  response_id: string
  request_id?: string
  after_event_seq?: number
  text: string
  status: 'streaming' | 'typing' | 'connecting'
}

export interface AgentTimelineState {
  persistent: PersistentTimelineState
  transient: TransientStreamOverlay | null
}

const entityKey = (turnId: string, id: string): string => JSON.stringify([turnId, id])

export function createPersistentTimelineState(
  sessionId: string,
  branchView: TimelineBranchView,
  cursor: TimelineCursor | null = null,
): PersistentTimelineState {
  return {
    schema_version: 1,
    session_id: sessionId,
    branch_view: branchView,
    cursor,
    applied_events: {},
    turns: {},
    turn_order: [],
    messages: {},
    message_order: [],
    activities: {},
    activity_order: [],
    tools: {},
    tool_order: [],
    interactions: {},
    interaction_order: [],
    items: [],
  }
}

export function createAgentTimelineState(
  sessionId: string,
  branchView: TimelineBranchView,
  cursor: TimelineCursor | null = null,
): AgentTimelineState {
  return { persistent: createPersistentTimelineState(sessionId, branchView, cursor), transient: null }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function recordOrEmpty<T>(value: unknown): Record<string, T> {
  return isRecord(value) ? value as Record<string, T> : {}
}

function stringArrayOrEmpty(value: unknown): string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string') ? value : []
}

/** Restore the server's materialized projection without treating it as a new event. */
export function hydratePersistentTimelineProjection(
  value: unknown,
  expectedSessionId: string,
  expectedBranchId: string,
): PersistentTimelineState {
  if (!isRecord(value) || value.schema_version !== 1 || value.session_id !== expectedSessionId || !isRecord(value.branch_view) ||
    value.branch_view.branch_id !== expectedBranchId || !Array.isArray(value.branch_view.visible_branches) ||
    (value.cursor !== null && (!isRecord(value.cursor) || typeof value.cursor.event_seq !== 'number' ||
      typeof value.cursor.event_id !== 'string' || typeof value.cursor.branch_id !== 'string')) ||
    !Array.isArray(value.items)) {
    throw new Error('不支持的时间轴投影协议')
  }
  return {
    schema_version: 1,
    session_id: expectedSessionId,
    branch_view: value.branch_view as unknown as TimelineBranchView,
    cursor: value.cursor as TimelineCursor | null,
    applied_events: recordOrEmpty(value.applied_events),
    turns: recordOrEmpty(value.turns),
    turn_order: stringArrayOrEmpty(value.turn_order),
    messages: recordOrEmpty(value.messages),
    message_order: stringArrayOrEmpty(value.message_order),
    activities: recordOrEmpty(value.activities),
    activity_order: stringArrayOrEmpty(value.activity_order),
    tools: recordOrEmpty(value.tools),
    tool_order: stringArrayOrEmpty(value.tool_order),
    interactions: recordOrEmpty(value.interactions),
    interaction_order: stringArrayOrEmpty(value.interaction_order),
    tasks: recordOrEmpty(value.tasks),
    task_order: stringArrayOrEmpty(value.task_order),
    steps: recordOrEmpty(value.steps),
    step_order: stringArrayOrEmpty(value.step_order),
    model_requests: recordOrEmpty(value.model_requests),
    model_request_order: stringArrayOrEmpty(value.model_request_order),
    items: value.items as TimelineItemRef[],
  }
}

function visibleThrough(state: PersistentTimelineState, branchId: string): number | null | undefined {
  return state.branch_view.visible_branches.find((branch) => branch.branch_id === branchId)?.through_event_seq
}

function ensureTurn(
  turns: Record<string, TimelineTurn>,
  order: string[],
  turnId: string,
  eventSeq: number,
): TimelineTurn {
  const existing = turns[turnId]
  if (existing) return existing
  const turn: TimelineTurn = { turn_id: turnId, status: 'running', started_seq: eventSeq }
  turns[turnId] = turn
  order.push(turnId)
  return turn
}

function appendItem(items: TimelineItemRef[], item: TimelineItemRef): void {
  if (!items.some((existing) => existing.kind === item.kind && existing.key === item.key)) items.push(item)
}

function reduceAcceptedEvent(state: PersistentTimelineState, event: TimelineLedgerEvent): PersistentTimelineState {
  const eventSeq = event.event_seq
  const turns = { ...state.turns }
  const turnOrder = [...state.turn_order]
  const messages = { ...state.messages }
  const messageOrder = [...state.message_order]
  const activities = { ...state.activities }
  const activityOrder = [...state.activity_order]
  const tools = { ...state.tools }
  const toolOrder = [...state.tool_order]
  const interactions = { ...state.interactions }
  const interactionOrder = [...state.interaction_order]
  const items = [...state.items]
  const turn = ensureTurn(turns, turnOrder, event.turn_id, eventSeq)

  switch (event.event_type) {
    case 'message.accepted': {
      const key = entityKey(event.turn_id, event.payload.message_id)
      if (!messages[key]) {
        messages[key] = {
          key,
          message_id: event.payload.message_id,
          turn_id: event.turn_id,
          role: 'user',
          content: event.payload.content,
          event_seq: eventSeq,
          event_id: event.event_id,
          occurred_at: event.occurred_at,
          recorded_at: event.recorded_at,
        }
        messageOrder.push(key)
        appendItem(items, { kind: 'message', key, event_seq: eventSeq })
      }
      break
    }
    case 'model.response.completed': {
      const key = entityKey(event.turn_id, event.payload.message_id)
      if (!messages[key]) {
        messages[key] = {
          key,
          message_id: event.payload.message_id,
          turn_id: event.turn_id,
          role: 'assistant',
          content: event.payload.content,
          event_seq: eventSeq,
          event_id: event.event_id,
          occurred_at: event.occurred_at,
          recorded_at: event.recorded_at,
          response_id: event.payload.response_id,
          request_id: event.payload.request_id,
        }
        messageOrder.push(key)
        appendItem(items, { kind: 'message', key, event_seq: eventSeq })
      }
      break
    }
    case 'activity.started': {
      const key = entityKey(event.turn_id, event.activity_id ?? '')
      if (!activities[key]) {
        activities[key] = {
          key,
          activity_id: event.activity_id ?? '',
          turn_id: event.turn_id,
          parent_activity_id: event.parent_activity_id,
          kind: event.payload.activity_kind,
          label: event.payload.label,
          summary: event.payload.summary,
          status: 'running',
          started_seq: eventSeq,
        }
        activityOrder.push(key)
        appendItem(items, { kind: 'activity', key, event_seq: eventSeq })
      }
      break
    }
    case 'activity.settled': {
      const activityId = event.activity_id ?? ''
      const key = entityKey(event.turn_id, activityId)
      const existing = activities[key]
      if (!existing) {
        activities[key] = {
          key,
          activity_id: activityId,
          turn_id: event.turn_id,
          parent_activity_id: event.parent_activity_id,
          kind: 'unknown',
          label: '活动',
          summary: event.payload.summary,
          status: event.payload.status,
          started_seq: eventSeq,
          settled_seq: eventSeq,
        }
        activityOrder.push(key)
        appendItem(items, { kind: 'activity', key, event_seq: eventSeq })
      } else if (existing.status === 'running') {
        activities[key] = { ...existing, status: event.payload.status, summary: event.payload.summary ?? existing.summary, settled_seq: eventSeq }
      }
      break
    }
    case 'tool.attempt.started': {
      const key = entityKey(event.turn_id, event.payload.call_id)
      const existing = tools[key]
      const existingAttempt = existing?.attempts.find((attempt) => attempt.attempt_id === event.payload.attempt_id)
      if (existingAttempt) break
      const retryAllowed = !existing || existing.status === 'failed' || existing.status === 'unknown'
      if (!retryAllowed) break
      const attempt: TimelineToolAttempt = { attempt_id: event.payload.attempt_id, status: 'running', started_seq: eventSeq }
      if (existing) {
        tools[key] = {
          ...existing,
          activity_id: event.activity_id ?? existing.activity_id,
          parent_activity_id: event.parent_activity_id ?? existing.parent_activity_id,
          batch_id: event.payload.batch_id ?? existing.batch_id,
          input_summary: event.payload.input_summary ?? existing.input_summary,
          status: 'running',
          attempts: [...existing.attempts, attempt],
          settled_seq: undefined,
          result_summary: undefined,
          reason_code: undefined,
        }
      } else {
        tools[key] = {
          key,
          call_id: event.payload.call_id,
          turn_id: event.turn_id,
          activity_id: event.activity_id,
          parent_activity_id: event.parent_activity_id,
          batch_id: event.payload.batch_id,
          tool_name: event.payload.tool_name,
          input_summary: event.payload.input_summary,
          status: 'running',
          attempts: [attempt],
          started_seq: eventSeq,
        }
        toolOrder.push(key)
        appendItem(items, { kind: 'tool', key, event_seq: eventSeq })
      }
      break
    }
    case 'tool.attempt.settled': {
      const key = entityKey(event.turn_id, event.payload.call_id)
      const existing = tools[key]
      const attempts = existing ? [...existing.attempts] : []
      const index = attempts.findIndex((attempt) => attempt.attempt_id === event.payload.attempt_id)
      const priorAttempt = attempts[index]
      if (priorAttempt && priorAttempt.status !== 'running') break
      const settledAttempt: TimelineToolAttempt = {
        ...(priorAttempt ?? { attempt_id: event.payload.attempt_id, started_seq: eventSeq }),
        status: event.payload.status,
        settled_seq: eventSeq,
        result_summary: event.payload.result_summary,
        reason_code: event.payload.reason_code,
      }
      if (index >= 0) attempts[index] = settledAttempt
      else attempts.push(settledAttempt)
      const firstStart = existing?.started_seq ?? eventSeq
      const call: TimelineToolCall = {
        key,
        call_id: event.payload.call_id,
        turn_id: event.turn_id,
        activity_id: event.activity_id ?? existing?.activity_id,
        parent_activity_id: event.parent_activity_id ?? existing?.parent_activity_id,
        batch_id: existing?.batch_id,
        tool_name: existing?.tool_name ?? 'unknown',
        input_summary: existing?.input_summary,
        status: event.payload.status,
        attempts,
        started_seq: firstStart,
        settled_seq: eventSeq,
        result_summary: event.payload.result_summary,
        reason_code: event.payload.reason_code,
      }
      tools[key] = call
      if (!existing) {
        toolOrder.push(key)
        appendItem(items, { kind: 'tool', key, event_seq: eventSeq })
      }
      break
    }
    case 'interaction.opened': {
      const key = entityKey(event.turn_id, event.payload.interaction_id)
      if (!interactions[key]) {
        interactions[key] = {
          key,
          interaction_id: event.payload.interaction_id,
          turn_id: event.turn_id,
          activity_id: event.activity_id,
          kind: event.payload.kind,
          prompt_summary: event.payload.prompt_summary,
          status: 'waiting',
          opened_seq: eventSeq,
        }
        interactionOrder.push(key)
        appendItem(items, { kind: 'interaction', key, event_seq: eventSeq })
      }
      break
    }
    case 'interaction.resolved': {
      const key = entityKey(event.turn_id, event.payload.interaction_id)
      const existing = interactions[key]
      if (existing && existing.status !== 'waiting') break
      interactions[key] = {
        key,
        interaction_id: event.payload.interaction_id,
        turn_id: event.turn_id,
        activity_id: event.activity_id ?? existing?.activity_id,
        kind: existing?.kind ?? 'unknown',
        prompt_summary: existing?.prompt_summary,
        status: event.payload.status,
        decision_summary: event.payload.decision_summary,
        opened_seq: existing?.opened_seq ?? eventSeq,
        resolved_seq: eventSeq,
      }
      if (!existing) {
        interactionOrder.push(key)
        appendItem(items, { kind: 'interaction', key, event_seq: eventSeq })
      }
      break
    }
    case 'turn.settled':
      if (turn.status === 'running') {
        turns[event.turn_id] = {
          ...turn,
          status: event.payload.status,
          settled_seq: eventSeq,
          reason_code: event.payload.reason_code,
          final_message_id: event.payload.final_message_id,
        }
      }
      break
  }

  return {
    ...state,
    cursor: { branch_id: state.branch_view.branch_id, event_seq: eventSeq, event_id: event.event_id },
    applied_events: {
      ...state.applied_events,
      [event.event_id]: { branch_id: event.branch_id, event_seq: eventSeq },
    },
    turns,
    turn_order: turnOrder,
    messages,
    message_order: messageOrder,
    activities,
    activity_order: activityOrder,
    tools,
    tool_order: toolOrder,
    interactions,
    interaction_order: interactionOrder,
    items,
  }
}

function normalizeKnownEvent(envelope: TimelineEventEnvelope): TimelineLedgerEvent | null {
  const payload = envelope.payload
  const base = {
    schema_version: 3 as const,
    session_id: envelope.session_id,
    branch_id: envelope.branch_id,
    event_seq: envelope.event_seq,
    event_id: envelope.event_id,
    turn_id: envelope.turn_id!,
    event_type: envelope.event_type,
    recorded_at: envelope.recorded_at,
    occurred_at: envelope.occurred_at,
    activity_id: envelope.activity_id,
    parent_activity_id: envelope.parent_activity_id,
  }
  if (!envelope.turn_id) return null
  switch (envelope.event_type) {
    case 'message.accepted':
      return { ...base, event_type: 'message.accepted', payload: payload as unknown as Extract<TimelineLedgerEvent, { event_type: 'message.accepted' }>['payload'] }
    case 'model.response.completed':
      return { ...base, event_type: 'model.response.completed', payload } as TimelineLedgerEvent
    case 'activity.started':
      return { ...base, event_type: 'activity.started', payload } as TimelineLedgerEvent
    case 'activity.settled':
      return { ...base, event_type: 'activity.settled', payload } as TimelineLedgerEvent
    case 'tool.attempt.started':
      return { ...base, event_type: 'tool.attempt.started', payload: {
        ...payload,
        call_id: String(payload.call_id ?? envelope.call_id ?? ''),
        attempt_id: String(payload.attempt_id ?? envelope.attempt_id ?? ''),
        tool_name: String(payload.tool_name ?? 'unknown'),
      } } as TimelineLedgerEvent
    case 'tool.attempt.settled':
      return { ...base, event_type: 'tool.attempt.settled', payload: {
        ...payload,
        call_id: String(payload.call_id ?? envelope.call_id ?? ''),
        attempt_id: String(payload.attempt_id ?? envelope.attempt_id ?? ''),
      } } as TimelineLedgerEvent
    case 'interaction.opened':
      return { ...base, event_type: 'interaction.opened', payload: {
        ...payload,
        interaction_id: String(payload.interaction_id ?? envelope.interaction_id ?? ''),
      } } as TimelineLedgerEvent
    case 'interaction.resolved':
      return { ...base, event_type: 'interaction.resolved', payload: {
        ...payload,
        interaction_id: String(payload.interaction_id ?? envelope.interaction_id ?? ''),
      } } as TimelineLedgerEvent
    case 'turn.settled':
      return { ...base, event_type: 'turn.settled', payload } as TimelineLedgerEvent
    default:
      return null
  }
}

/** Apply committed events; unknown durable event kinds still advance the confirmed cursor. */
export function reducePersistentTimeline(
  state: PersistentTimelineState,
  event: TimelineLedgerEvent | TimelineEventEnvelope,
): PersistentTimelineState {
  if (event.schema_version !== 3) throw new Error('不支持的持久事件版本')
  const priorEvent = state.applied_events[event.event_id]
  if (priorEvent) {
    if (priorEvent.branch_id !== event.branch_id || priorEvent.event_seq !== event.event_seq) {
      throw new Error('event_id 对应了冲突的账本位置')
    }
    return state
  }
  if (event.session_id !== state.session_id) return state
  const visibleLimit = visibleThrough(state, event.branch_id)
  if (visibleLimit === undefined || (visibleLimit !== null && event.event_seq > visibleLimit)) return state
  if (state.cursor && event.event_seq <= state.cursor.event_seq) return state

  const supported = normalizeKnownEvent(event as TimelineEventEnvelope)
  if (!supported) {
    const next: PersistentTimelineState = {
      ...state,
      cursor: { branch_id: state.branch_view.branch_id, event_seq: event.event_seq, event_id: event.event_id },
      applied_events: {
        ...state.applied_events,
        [event.event_id]: { branch_id: event.branch_id, event_seq: event.event_seq },
      },
    }
    if (event.turn_id && ['task.started', 'task.settled', 'step.started', 'step.settled'].includes(event.event_type)) {
      const payload = event.payload as Record<string, unknown>
      const subject = event.event_type.startsWith('task.') ? 'task' : 'step'
      const mapName = subject === 'task' ? 'tasks' : 'steps'
      const orderName = subject === 'task' ? 'task_order' : 'step_order'
      const id = payload[`${subject}_id`]
      if (typeof id !== 'string' || !id) throw new Error(`持久 ${subject} 事件缺少身份`)
      const key = entityKey(event.turn_id, id)
      const existing = state[mapName]?.[key]
      const terminal = existing && !['running', 'pending', 'waiting_user'].includes(existing.status)
      if (!terminal) {
        const started = event.event_type.endsWith('.started')
        const item: TimelineLifecycleEntity = {
          ...(existing ?? { key, turn_id: event.turn_id, started_seq: event.event_seq, started_at: event.occurred_at }),
          status: started ? 'running' : String(payload.status ?? 'unknown'),
        }
        for (const name of ['task_id', 'step_id', 'node', 'title', 'name', 'label', 'summary', 'action'] as const) {
          const value = payload[name]
          if (typeof value === 'string') item[name] = value
        }
        if (!started) { item.settled_seq = event.event_seq; item.settled_at = event.occurred_at }
        next[mapName] = { ...state[mapName], [key]: item }
        next[orderName] = existing ? state[orderName] : [...(state[orderName] ?? []), key]
        if (!existing) next.items = [...state.items, { kind: subject, key, event_seq: event.event_seq }]
      }
      if (!state.turns[event.turn_id]) {
        next.turns = { ...state.turns, [event.turn_id]: { turn_id: event.turn_id, status: 'running', started_seq: event.event_seq } }
        next.turn_order = [...state.turn_order, event.turn_id]
      }
    }
    return next
  }
  return reduceAcceptedEvent(state, supported)
}

export function applyTransientOverlay(
  state: AgentTimelineState,
  update: { turn_id: string; response_id: string; request_id?: string; after_event_seq?: number; text: string; mode?: 'append' | 'replace'; status?: TransientStreamOverlay['status'] },
): AgentTimelineState {
  const current = state.transient
  const sameResponse = current?.turn_id === update.turn_id && current.response_id === update.response_id &&
    current.request_id === update.request_id
  const text = update.mode === 'append' && sameResponse ? current.text + update.text : update.text
  return {
    ...state,
    transient: {
      turn_id: update.turn_id,
      response_id: update.response_id,
      request_id: update.request_id,
      after_event_seq: sameResponse ? current.after_event_seq ?? update.after_event_seq : update.after_event_seq,
      text,
      status: update.status ?? 'streaming',
    },
  }
}

export function clearTransientOverlay(state: AgentTimelineState, responseId?: string): AgentTimelineState {
  if (!state.transient || (responseId && state.transient.response_id !== responseId)) return state
  return { ...state, transient: null }
}

export function reduceTimelineEvent(state: AgentTimelineState, event: TimelineLedgerEvent): AgentTimelineState {
  const persistent = reducePersistentTimeline(state.persistent, event)
  const isNewEvent = persistent !== state.persistent
  const completesOverlay = event.event_type === 'model.response.completed' &&
    state.transient?.turn_id === event.turn_id &&
    (state.transient.request_id
      ? state.transient.request_id === event.payload.request_id
      : state.transient.after_event_seq !== undefined
        ? event.event_seq > state.transient.after_event_seq
        : state.transient.response_id === event.payload.response_id)
  const settlesOverlay = event.event_type === 'turn.settled' && state.transient?.turn_id === event.turn_id
  return {
    persistent,
    transient: isNewEvent && (completesOverlay || settlesOverlay) ? null : state.transient,
  }
}
