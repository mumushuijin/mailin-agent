import { expect, test } from 'vitest'
import { createPersistentTimelineState, reducePersistentTimeline, type TimelineBranchView, type TimelineEventEnvelope, type TimelineLedgerEvent } from './eventTimelineReducer.ts'
import { buildTimelineAgentStates, reconcileTimelineMessages } from './timelineView.ts'

const branchView: TimelineBranchView = {
  branch_id: 'main',
  visible_branches: [{ branch_id: 'main', through_event_seq: null }],
}

function event<T extends TimelineLedgerEvent['event_type']>(
  eventType: T,
  seq: number,
  payload: Extract<TimelineLedgerEvent, { event_type: T }>['payload'],
): Extract<TimelineLedgerEvent, { event_type: T }> {
  return {
    schema_version: 3,
    session_id: 'session-1',
    branch_id: 'main',
    event_seq: seq,
    event_id: `event-${seq}`,
    turn_id: 'turn-1',
    event_type: eventType,
    occurred_at: new Date(seq * 1000).toISOString(),
    payload,
  } as Extract<TimelineLedgerEvent, { event_type: T }>
}

test('keeps assistant responses separate and attaches tools to the preceding response', () => {
  let state = createPersistentTimelineState('session-1', branchView)
  state = reducePersistentTimeline(state, event('message.accepted', 1, { message_id: 'user-1', content: 'inspect then explain' }))
  state = reducePersistentTimeline(state, event('model.response.completed', 3, {
    request_id: 'request-1', response_id: 'response-1', message_id: 'assistant-1', content: 'I will inspect the file.',
  }))
  state = reducePersistentTimeline(state, event('tool.attempt.started', 4, {
    call_id: 'call-1', attempt_id: 'attempt-1', tool_name: 'read_file',
  }))
  state = reducePersistentTimeline(state, event('tool.attempt.settled', 6, {
    call_id: 'call-1', attempt_id: 'attempt-1', status: 'succeeded', result_summary: 'File contents',
  }))
  state = reducePersistentTimeline(state, event('model.response.completed', 8, {
    request_id: 'request-2', response_id: 'response-2', message_id: 'assistant-2', content: 'Here is the explanation.',
  }))

  const messages = reconcileTimelineMessages([], state)
  const assistants = messages.filter((message) => message.role === 'assistant')

  expect(assistants.map((message) => message.messageId)).toEqual(['assistant-1', 'assistant-2'])
  expect(assistants[0]?.segments?.some((segment) => segment.type === 'tool' && segment.toolCallId === 'call-1')).toBe(true)
  expect(assistants[1]?.segments?.some((segment) => segment.type === 'tool')).toBe(false)
  const tool = assistants[0]?.segments?.find((segment) => segment.type === 'tool')
  expect(tool?.type === 'tool' && tool.status).toBe('succeeded')
  expect(tool?.type === 'tool' && tool.result).toBe('File contents')
})

test('updates an existing history tool segment with the durable terminal outcome', () => {
  let state = createPersistentTimelineState('session-1', branchView)
  state = reducePersistentTimeline(state, event('message.accepted', 1, { message_id: 'user-1', content: 'read it' }))
  state = reducePersistentTimeline(state, event('model.response.completed', 2, {
    request_id: 'request-1', response_id: 'response-1', message_id: 'assistant-1', content: 'Reading it now.',
  }))
  state = reducePersistentTimeline(state, event('tool.attempt.started', 3, {
    call_id: 'call-1', attempt_id: 'attempt-1', tool_name: 'read_file',
  }))
  const inProgress = reconcileTimelineMessages([], state)
  const assistant = inProgress.find((message) => message.messageId === 'assistant-1')!
  const tool = assistant.segments?.find((segment) => segment.type === 'tool')
  expect(tool?.type === 'tool' && tool.status).toBe('running')

  state = reducePersistentTimeline(state, event('tool.attempt.settled', 5, {
    call_id: 'call-1', attempt_id: 'attempt-1', status: 'succeeded', result_summary: 'Done',
  }))
  const reconciled = reconcileTimelineMessages(inProgress, state)
  const updated = reconciled.find((message) => message.messageId === 'assistant-1')?.segments?.find((segment) => segment.type === 'tool')

  expect(reconciled.filter((message) => message.messageId === 'assistant-1')).toHaveLength(1)
  expect(updated?.type === 'tool' && updated.status).toBe('succeeded')
  expect(updated?.type === 'tool' && updated.result).toBe('Done')
})

test('restores task, step, and waiting interaction lifecycle from durable events', () => {
  const envelope = (eventSeq: number, eventType: string, turnId: string | undefined, payload: Record<string, unknown>): TimelineEventEnvelope => ({
    schema_version: 3, session_id: 'session-1', branch_id: 'main', event_seq: eventSeq,
    event_id: `event-${eventSeq}`, turn_id: turnId, event_type: eventType, occurred_at: new Date(eventSeq * 1000).toISOString(), payload,
  })
  let state = createPersistentTimelineState('session-1', branchView)
  state = reducePersistentTimeline(state, event('message.accepted', 1, { message_id: 'user-1', content: 'do the work' }))
  state = reducePersistentTimeline(state, envelope(2, 'task.started', 'turn-1', { task_id: 'task-1', title: 'Inspect files' }))
  state = reducePersistentTimeline(state, envelope(3, 'step.started', 'turn-1', { step_id: 'step-1', node: 'tools', task_id: 'task-1' }))
  state = reducePersistentTimeline(state, envelope(4, 'interaction.opened', 'turn-1', { interaction_id: 'interaction-1', kind: 'approval', prompt_summary: 'Approve write?' }))

  const messages = reconcileTimelineMessages([], state)
  const timeline = buildTimelineAgentStates(state, messages)[1]

  expect(timeline?.nodes.map((node) => node.id)).toEqual(expect.arrayContaining(['task:task-1', 'step:step-1', 'interaction:interaction-1']))
  expect(timeline?.nodes.find((node) => node.id === 'step:step-1')?.status).toBe('tool_calling')
  expect(timeline?.requiresUserAction).toBe(true)
  expect(timeline?.status).toBe('awaiting_user')
})

test('merges durable responses with history and keeps uncommitted response overlays distinct', () => {
  let state = createPersistentTimelineState('session-1', branchView)
  state = reducePersistentTimeline(state, event('message.accepted', 1, { message_id: 'user-1', content: 'ask' }))
  state = reducePersistentTimeline(state, event('model.response.completed', 2, {
    request_id: 'request-1', response_id: 'response-1', message_id: 'assistant-1', content: 'committed',
  }))
  state = reducePersistentTimeline(state, event('model.response.completed', 4, {
    request_id: 'request-2', response_id: 'response-2', message_id: 'assistant-2', content: 'also committed',
  }))
  const history = [
    { id: 1, role: 'user' as const, messageId: 'user-1', turnId: 'turn-1', ledgerSeq: 1, content: 'old body', timestamp: new Date(1) },
    { id: 2, role: 'assistant' as const, messageId: 'assistant-1', turnId: 'turn-1', ledgerSeq: 2, content: 'stale body', timestamp: new Date(2) },
    { id: 3, role: 'assistant' as const, turnId: 'turn-1', requestId: 'request-2', content: 'partial only', timestamp: new Date(3) },
    { id: 4, role: 'assistant' as const, turnId: 'turn-1', requestId: 'request-3', content: 'not committed yet', timestamp: new Date(4) },
  ]

  const merged = reconcileTimelineMessages(history, state)

  expect(merged.map((message) => message.messageId)).toEqual(['user-1', 'assistant-1', 'assistant-2', undefined])
  expect(merged.map((message) => message.content)).toEqual(['ask', 'committed', 'also committed', 'not committed yet'])
})
