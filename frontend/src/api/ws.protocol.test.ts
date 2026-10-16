import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { chatWs } from './ws'
import type { StreamEvent } from './chat'

vi.mock('./index', () => ({ getWsApiBaseAsync: async () => 'ws://localhost' }))
class FakeSocket {
  static OPEN = 1
  static latest: FakeSocket
  readyState = 1
  frames: Array<Record<string, unknown>> = []
  onopen?: () => void
  onmessage?: (event: { data: string }) => void
  onerror?: () => void
  onclose?: () => void
  constructor() { FakeSocket.latest = this; queueMicrotask(() => this.onopen?.()) }
  send(raw: string) { this.frames.push(JSON.parse(raw)) }
  close() { this.readyState = 3; this.onclose?.() }
  receive(type: string, data: Record<string, unknown>, state?: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify({ type, data, state, turn_id: 'turn_1' }) })
  }
}
const state = (status: string) => ({ schema_version: 2, checkpoint_id: `cp_${'c'.repeat(32)}`, state_revision: 5,
  scope: { workspace_id: 'w1', session_id: 's1', turn_id: 'turn_1' }, context: { working_message: [] },
  max_step_every_turn: 48, tasks: [], current_task: null,
  current_step: { step_id: 'step1', step_status: 'running', tool_calls: [], model: null },
  memory: { recorded_count: 0, last_synced_turn: null, source_files: [] }, ledger_pointer: { applied_seq: 9 }, turn_status: status })
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal('WebSocket', FakeSocket) })
afterEach(() => { chatWs.disconnect(); vi.useRealTimers(); vi.unstubAllGlobals() })
const connected = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }

describe('WebSocket turn lifecycle', () => {
  it('keeps waiting callbacks after an invalid answer, includes approval identity, and settles an authoritative terminal sync', async () => {
    const events: StreamEvent[] = []
    const promise = chatWs.sendMessage('hello', 's1', event => events.push(event), 'turn_1')
    let settled = false
    void promise.then(() => { settled = true })
    await connected()
    const socket = FakeSocket.latest
    socket.receive('state_sync', { session_id: 's1' }, state('waiting_user'))
    await Promise.resolve()
    expect(settled).toBe(false)
    socket.receive('interrupt', { session_id: 's1', kind: 'approval', interrupt_id: 'i1', tool_call_id: 'c1' })
    chatWs.approve('turn_1', 'allow', { sessionId: 's1', interruptId: 'i1', stepId: 'step1', toolCallId: 'c1' })
    expect(socket.frames[socket.frames.length - 1]).toMatchObject({ op: 'approve', turn_id: 'turn_1', session_id: 's1', interrupt_id: 'i1', step_id: 'step1', tool_call_id: 'c1' })
    socket.receive('error', { session_id: 's1', error: 'retry', reason_code: 'invalid_answer' })
    expect(events[events.length - 1]?.stage).toBe('interaction_rejected')
    socket.receive('state_sync', { session_id: 's1' }, state('completed'))
    expect(await promise).toMatchObject({ session_id: 's1', turn_id: 'turn_1' })
    expect(events.some(event => event.type === 'state_sync' && event.state?.turn_status === 'completed')).toBe(true)
  })

  it('restores subscription and rejects a failed snapshot without requiring done', async () => {
    const events: StreamEvent[] = []
    const promise = chatWs.sendMessage('hello', 's1', event => events.push(event), 'turn_1')
    const rejection = promise.catch(error => error)
    await connected()
    const socket = FakeSocket.latest
    socket.onopen?.()
    expect(socket.frames.some(frame => frame.op === 'session.subscribe' && frame.session_id === 's1')).toBe(true)
    expect(socket.frames.some(frame => frame.op === 'state.sync' && frame.turn_id === 'turn_1')).toBe(true)
    socket.receive('state_sync', { session_id: 's1' }, state('failed'))
    expect(await rejection).toMatchObject({ executionStarted: true })
    expect(events[events.length - 1]?.state?.turn_status).toBe('failed')
  })
})
