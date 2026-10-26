import { expect, test } from 'vitest'
import {
  applyTransientOverlay,
  createAgentTimelineState,
  createPersistentTimelineState,
  reducePersistentTimeline,
  reduceTimelineEvent,
  type TimelineBranchView,
  type TimelineLedgerEvent,
} from './eventTimelineReducer.ts'

const branchView: TimelineBranchView = {
  branch_id: 'main',
  visible_branches: [{ branch_id: 'main', through_event_seq: null }],
}

function event<T extends TimelineLedgerEvent['event_type']>(
  eventType: T,
  seq: number,
  payload: Extract<TimelineLedgerEvent, { event_type: T }>['payload'],
  extras: Partial<Pick<TimelineLedgerEvent, 'branch_id' | 'turn_id' | 'event_id' | 'activity_id'>> = {},
): Extract<TimelineLedgerEvent, { event_type: T }> {
  return {
    schema_version: 3,
    session_id: 'session-1',
    branch_id: extras.branch_id ?? 'main',
    event_seq: seq,
    event_id: extras.event_id ?? `event-${seq}`,
    turn_id: extras.turn_id ?? 'turn-1',
    event_type: eventType,
    activity_id: extras.activity_id,
    payload,
  } as Extract<TimelineLedgerEvent, { event_type: T }>
}

test('deduplicates durable event ids without appending a second message', () => {
  const state = createPersistentTimelineState('session-1', branchView)
  const accepted = event('message.accepted', 1, { message_id: 'message-1', content: 'hello' })

  const applied = reducePersistentTimeline(state, accepted)
  const duplicate = reducePersistentTimeline(applied, accepted)

  expect(duplicate).toBe(applied)
  expect(duplicate.message_order).toHaveLength(1)
  expect(duplicate.cursor).toEqual({ branch_id: 'main', event_seq: 1, event_id: 'event-1' })
})

test('keeps multiple assistant responses as separate ordered messages in one turn', () => {
  let state = createPersistentTimelineState('session-1', branchView)
  state = reducePersistentTimeline(state, event('message.accepted', 1, { message_id: 'user-1', content: 'do this' }))
  state = reducePersistentTimeline(state, event('model.response.completed', 3, {
    request_id: 'request-1', response_id: 'response-1', message_id: 'assistant-1', content: 'first update',
  }))
  state = reducePersistentTimeline(state, event('model.response.completed', 7, {
    request_id: 'request-2', response_id: 'response-2', message_id: 'assistant-2', content: 'final answer',
  }))

  expect(state.message_order.map((key) => state.messages[key]?.content)).toEqual(['do this', 'first update', 'final answer'])
  expect(state.messages[state.message_order[1] ?? '']?.turn_id).toBe('turn-1')
})

test('accepts physical sequence gaps and does not reopen a settled tool attempt', () => {
  let state = createPersistentTimelineState('session-1', branchView)
  state = reducePersistentTimeline(state, event('tool.attempt.started', 2, {
    call_id: 'call-1', attempt_id: 'attempt-1', tool_name: 'read_file',
  }, { activity_id: 'activity-1' }))
  state = reducePersistentTimeline(state, event('tool.attempt.settled', 5, {
    call_id: 'call-1', attempt_id: 'attempt-1', status: 'succeeded', result_summary: 'Read complete',
  }, { activity_id: 'activity-1' }))
  state = reducePersistentTimeline(state, event('tool.attempt.started', 9, {
    call_id: 'call-1', attempt_id: 'attempt-1', tool_name: 'read_file',
  }, { event_id: 'duplicate-start', activity_id: 'activity-1' }))

  const tool = state.tools[JSON.stringify(['turn-1', 'call-1'])]
  expect(state.cursor?.event_seq).toBe(9)
  expect(tool?.status).toBe('succeeded')
  expect(tool?.attempts).toHaveLength(1)
  expect(tool?.attempts[0]?.status).toBe('succeeded')
})

test('filters sibling branches while allowing a child branch cursor to skip physical sequences', () => {
  const childView: TimelineBranchView = {
    branch_id: 'child',
    visible_branches: [
      { branch_id: 'main', through_event_seq: 4 },
      { branch_id: 'child', through_event_seq: null },
    ],
  }
  let state = createPersistentTimelineState('session-1', childView)
  state = reducePersistentTimeline(state, event('message.accepted', 2, { message_id: 'ancestor', content: 'keep' }, { branch_id: 'main' }))
  const sibling = event('message.accepted', 6, { message_id: 'sibling', content: 'ignore' }, { branch_id: 'other' })
  expect(reducePersistentTimeline(state, sibling)).toBe(state)
  state = reducePersistentTimeline(state, event('message.accepted', 9, { message_id: 'child-message', content: 'also keep' }, { branch_id: 'child' }))

  expect(state.message_order.map((key) => state.messages[key]?.content)).toEqual(['keep', 'also keep'])
  expect(state.cursor).toEqual({ branch_id: 'child', event_seq: 9, event_id: 'event-9' })
})

test('projects activity, interaction, and turn settlement without losing terminal state', () => {
  let state = createPersistentTimelineState('session-1', branchView)
  state = reducePersistentTimeline(state, event('activity.started', 1, {
    activity_kind: 'planning', label: 'Planning', summary: 'Preparing steps',
  }, { activity_id: 'activity-1' }))
  state = reducePersistentTimeline(state, event('activity.settled', 2, { status: 'completed' }, { activity_id: 'activity-1' }))
  state = reducePersistentTimeline(state, event('interaction.opened', 3, {
    interaction_id: 'interaction-1', kind: 'approval', prompt_summary: 'Allow file write?',
  }, { activity_id: 'activity-1' }))
  state = reducePersistentTimeline(state, event('interaction.resolved', 4, {
    interaction_id: 'interaction-1', status: 'completed', decision_summary: 'Approved',
  }))
  state = reducePersistentTimeline(state, event('turn.settled', 5, { status: 'completed', final_message_id: 'assistant-1' }))
  state = reducePersistentTimeline(state, event('activity.started', 6, {
    activity_kind: 'planning', label: 'Late update',
  }, { activity_id: 'activity-1', event_id: 'late-activity-start' }))

  expect(state.activities[JSON.stringify(['turn-1', 'activity-1'])]?.status).toBe('completed')
  expect(state.interactions[JSON.stringify(['turn-1', 'interaction-1'])]?.status).toBe('completed')
  expect(state.turns['turn-1']?.status).toBe('completed')
})

test('keeps stream text transient and clears only the matching response after persistence', () => {
  let state = createAgentTimelineState('session-1', branchView)
  state = applyTransientOverlay(state, { turn_id: 'turn-1', response_id: 'response-1', text: 'partial ' })
  state = applyTransientOverlay(state, {
    turn_id: 'turn-1', response_id: 'response-1', text: 'answer', mode: 'append',
  })
  const persistentBefore = state.persistent

  state = reduceTimelineEvent(state, event('model.response.completed', 2, {
    request_id: 'request-1', response_id: 'response-1', message_id: 'assistant-1', content: 'partial answer',
  }))

  expect(persistentBefore.message_order).toHaveLength(0)
  expect(state.transient).toBeNull()
  expect(state.persistent.messages[state.persistent.message_order[0] ?? '']?.content).toBe('partial answer')
})

test('does not clear a newer response overlay when an earlier request commits', () => {
  let state = createAgentTimelineState('session-1', branchView)
  state = applyTransientOverlay(state, {
    turn_id: 'turn-1', response_id: 'request-2', request_id: 'request-2', text: 'second response',
  })
  state = reduceTimelineEvent(state, event('model.response.completed', 2, {
    request_id: 'request-1', response_id: 'response-1', message_id: 'assistant-1', content: 'first response',
  }))

  expect(state.transient?.text).toBe('second response')
  state = reduceTimelineEvent(state, event('turn.settled', 3, { status: 'completed' }))
  expect(state.transient).toBeNull()
})

test('uses the confirmed event sequence when response identity is unavailable', () => {
  let state = createAgentTimelineState('session-1', branchView)
  state = applyTransientOverlay(state, {
    turn_id: 'turn-1', response_id: 'step-2', after_event_seq: 4, text: 'current response',
  })
  state = reduceTimelineEvent(state, event('model.response.completed', 3, {
    request_id: 'request-1', response_id: 'response-1', message_id: 'assistant-1', content: 'previous response',
  }))
  expect(state.transient?.text).toBe('current response')
  state = reduceTimelineEvent(state, event('model.response.completed', 5, {
    request_id: 'request-2', response_id: 'response-2', message_id: 'assistant-2', content: 'current response',
  }))
  expect(state.transient).toBeNull()
})
