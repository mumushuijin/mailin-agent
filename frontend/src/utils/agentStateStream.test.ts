import { expect, test } from 'vitest'
import type { StreamEvent } from '../api/chat.ts'
import {
  AGENT_STATUS_META,
  advanceAgentState,
  attachAgentStateMessage,
  createAgentStateStream,
  findRunningToolSegment,
  finalizeAgentState,
  getAgentStateTrail,
  isTerminalAgentStatus,
  mapStreamEventToAgentStatus,
  redactToolInputs,
  shouldAcceptAgentEvent,
  summarizeToolOutput,
} from './agentStateStream.ts'

test('maps lifecycle events to macro states', () => {
  expect(mapStreamEventToAgentStatus({ type: 'stage', stage: 'context_prepare' })).toBe('initializing')
  expect(mapStreamEventToAgentStatus({ type: 'stage', stage: 'thinking' })).toBe('reasoning')
  expect(mapStreamEventToAgentStatus({ type: 'tool_start' })).toBe('tool_calling')
  expect(mapStreamEventToAgentStatus({ type: 'tool_finish' })).toBe('observing')
  expect(mapStreamEventToAgentStatus({ type: 'interrupt' })).toBe('awaiting_user')
  expect(mapStreamEventToAgentStatus({ type: 'done' })).toBe('completed')
  expect(mapStreamEventToAgentStatus({ type: 'done', cancelled: true })).toBe('cancelled')
  expect(mapStreamEventToAgentStatus({ type: 'done', handoff: true })).toBe('handoff')
  expect(mapStreamEventToAgentStatus({ type: 'error' })).toBe('error')
  expect(mapStreamEventToAgentStatus({ type: 'error', cancelled: true })).toBe('cancelled')
})

test('keeps an ordered macro timeline without duplicating identical phases', () => {
  const state = createAgentStateStream(42, 'run-1', 100)
  advanceAgentState(state, 'planning', 120)
  advanceAgentState(state, 'planning', 130)
  advanceAgentState(state, 'tool_calling', 200)

  expect(state.nodes.map((node) => node.status)).toEqual(['initializing', 'planning', 'tool_calling'])
  expect(state.nodes[1]?.endedAt).toBe(200)
  expect(state.nodes[2]?.active).toBe(true)
})

test('keeps repeated phases in the optional trail and excludes the terminal node', () => {
  const state = createAgentStateStream(43, 'run-1', 100)
  advanceAgentState(state, 'planning', 120)
  advanceAgentState(state, 'tool_calling', 140)
  advanceAgentState(state, 'observing', 160)
  advanceAgentState(state, 'tool_calling', 180)
  finalizeAgentState(state, 'completed', '本轮完成', 200)

  expect(getAgentStateTrail(state).map((node) => node.status)).toEqual([
    'planning',
    'tool_calling',
    'observing',
    'tool_calling',
  ])
  expect(getAgentStateTrail(state).some((node) => isTerminalAgentStatus(node.status))).toBe(false)
})

test('locks terminal state against late phases and avoids duplicate terminal nodes', () => {
  const state = createAgentStateStream(44, 'run-1', 100)
  advanceAgentState(state, 'planning', 120)
  finalizeAgentState(state, 'completed', '本轮完成', 200)
  finalizeAgentState(state, 'completed', '本轮完成', 210)
  advanceAgentState(state, 'reasoning', 220)

  expect(state.status).toBe('completed')
  expect(state.nodes.filter((node) => isTerminalAgentStatus(node.status))).toHaveLength(1)
  expect(state.nodes.at(-1)?.active).toBe(false)

  finalizeAgentState(state, 'error', '本轮失败', 230)
  expect(state.status).toBe('error')
  expect(state.nodes.filter((node) => isTerminalAgentStatus(node.status))).toHaveLength(1)
  expect(state.terminalLabel).toBe('本轮失败')

  finalizeAgentState(state, 'cancelled', '本轮已取消', 240)
  expect(state.status).toBe('error')
  expect(state.terminalLabel).toBe('本轮失败')
})

test('redacts secrets and bounds large tool payloads', () => {
  const inputs = redactToolInputs({
    path: 'src/App.vue',
    token: 'super-secret',
    nested: { authorization: 'Bearer secret', value: 'ok' },
  })

  expect(inputs).toEqual({
    path: 'src/App.vue',
    token: '[已脱敏]',
    nested: { authorization: '[已脱敏]', value: 'ok' },
  })
  expect(summarizeToolOutput('x'.repeat(600))?.length).toBe(500)
})

test('provides user-facing metadata for every supported status', () => {
  expect(AGENT_STATUS_META.completed.terminal).toBe(true)
  expect(AGENT_STATUS_META.awaiting_user.label).toMatch(/授权/)
  expect(Object.keys(AGENT_STATUS_META)).toContain('memory_sync')
})

test('tracks confirmed sequence, syncing state, and message anchor on the normalized stream', () => {
  const state = createAgentStateStream(10, 'run-10', 100)
  state.confirmedSeq = 4
  state.syncing = true
  attachAgentStateMessage(state, 1001)

  expect(state.confirmedSeq).toBe(4)
  expect(state.syncing).toBe(true)
  expect(state.activeMessageId).toBe(1001)
  expect(state.nodes.at(-1)?.messageId).toBe(1001)
})

test('accepts stream event typing in the mapping boundary', () => {
  const event: StreamEvent = { type: 'compression', compression: { status: 'started' } }
  expect(mapStreamEventToAgentStatus(event)).toBe('initializing')
})

test('matches a tool finish to its call id before falling back to tool order', () => {
  const segments = [
    { type: 'tool' as const, id: 1, tool: 'run_shell', args: {}, toolCallId: 'call-1', status: 'running' as const },
    { type: 'tool' as const, id: 2, tool: 'run_shell', args: {}, toolCallId: 'call-2', status: 'running' as const },
  ]
  expect(findRunningToolSegment(segments, 'call-1')?.id).toBe(1)
  expect(findRunningToolSegment(segments, undefined, 'run_shell')?.id).toBe(2)
})

test('rejects late events from a different run id', () => {
  const state = createAgentStateStream(7, 'run-current', 100)
  expect(shouldAcceptAgentEvent(state, { run_id: 'run-current' })).toBe(true)
  expect(shouldAcceptAgentEvent(state, { run_id: 'run-old' })).toBe(false)
  expect(shouldAcceptAgentEvent(state, {})).toBe(true)
})

test('covers the complete visible lifecycle including approval and terminal states', () => {
  const state = createAgentStateStream(9, 'run-9', 100)
  for (const [status, at] of [
    ['planning', 120],
    ['reasoning', 140],
    ['tool_calling', 160],
    ['observing', 180],
    ['awaiting_user', 200],
    ['summarizing', 220],
    ['memory_sync', 240],
  ] as const) {
    advanceAgentState(state, status, at)
  }
  finalizeAgentState(state, 'completed', '本轮完成', 260)

  expect(state.nodes.map((node) => node.status)).toEqual([
    'initializing',
    'planning',
    'reasoning',
    'tool_calling',
    'observing',
    'awaiting_user',
    'summarizing',
    'memory_sync',
    'completed',
  ])
  expect(state.endedAt).toBe(260)
  expect(state.terminalLabel).toBe('本轮完成')
})
