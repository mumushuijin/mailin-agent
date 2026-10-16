import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentCheckpointState, StreamEvent } from './chat'

vi.mock('./index', () => ({ getWsApiBaseAsync: async () => 'ws://localhost' }))

class MockWebSocket {
  static OPEN = 1
  static sockets: MockWebSocket[] = []
  readyState = 0
  frames: Array<Record<string, unknown>> = []
  onopen?: () => void
  onmessage?: (event: { data: string }) => void
  onerror?: () => void
  onclose?: () => void

  constructor() {
    MockWebSocket.sockets.push(this)
    queueMicrotask(() => {
      this.readyState = MockWebSocket.OPEN
      this.onopen?.()
    })
  }

  send(raw: string) { this.frames.push(JSON.parse(raw)) }
  close() { this.readyState = 3; this.onclose?.() }
  receive(type: string, data: Record<string, unknown>, state?: AgentCheckpointState, turnId = TURN) {
    this.onmessage?.({ data: JSON.stringify({ type, data, state, turn_id: turnId }) })
  }
}

const SESSION = '00000000-0000-4000-8000-000000000001'
const TURN = 'turn_restored'
const checkpoint = (status: string, revision = 5): AgentCheckpointState => ({
  schema_version: 2, checkpoint_id: `cp_${'d'.repeat(32)}`, state_revision: revision,
  scope: { workspace_id: 'ws_1', session_id: SESSION, turn_id: TURN },
  context: { working_message: [] }, max_step_every_turn: 48, tasks: [], current_task: null,
  current_step: { step_id: 'step_waiting', step_name: 'tools',
    step_status: status === 'waiting_user' ? 'waiting_user' : status === 'running' ? 'running' : status,
    tool_calls: [{ call_id: 'call_write', definition_seq: 2, status: status === 'waiting_user' ? 'pending' : 'succeeded' }] },
  memory: { recorded_count: 0, last_synced_turn: null, source_files: [] },
  ledger_pointer: { applied_seq: 9 }, turn_status: status,
  pending_interaction: status === 'waiting_user'
    ? { id: 'interaction_1', kind: 'approval', call_id: 'call_write', definition_seq: 7 } : null,
})

let chatWs: (typeof import('./ws'))['chatWs']
const subscriptions: Array<() => void> = []
const socket = () => MockWebSocket.sockets[MockWebSocket.sockets.length - 1]!
const microtasks = async () => {
  for (let index = 0; index < 8; index++) await Promise.resolve()
}

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  MockWebSocket.sockets = []
  vi.stubGlobal('WebSocket', MockWebSocket)
  chatWs = (await import('./ws')).chatWs
})

afterEach(() => {
  for (const unsubscribe of subscriptions.splice(0)) unsubscribe()
  chatWs.disconnect()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('WebSocket subscription recovery', () => {
  it('delivers persisted waiting state and native interrupt after refresh without a send callback', async () => {
    const events: StreamEvent[] = []
    subscriptions.push(chatWs.onSessionStream(event => events.push(event)))
    await chatWs.connect()
    chatWs.subscribeSession(SESSION)
    expect(socket().frames).toContainEqual({ op: 'session.subscribe', session_id: SESSION })
    socket().receive('state_sync', { session_id: SESSION }, checkpoint('waiting_user'))
    socket().receive('interrupt', { session_id: SESSION, kind: 'approval', interrupt_id: 'interaction_1',
      tool_call_id: 'call_write', step_id: 'step_waiting', tool: 'write_file' }, checkpoint('waiting_user'))
    expect(events.map(event => event.type)).toEqual(['state_sync', 'interrupt'])
    expect(events[0]?.state?.pending_interaction?.id).toBe('interaction_1')
    expect(events[1]).toMatchObject({ turn_id: TURN, interrupt_id: 'interaction_1', tool_call_id: 'call_write', step_id: 'step_waiting' })
    chatWs.approve(TURN, 'allow', { sessionId: SESSION, interruptId: 'interaction_1', toolCallId: 'call_write', stepId: 'step_waiting' })
    expect(socket().frames[socket().frames.length - 1]).toMatchObject({ op: 'approve', turn_id: TURN,
      session_id: SESSION, interrupt_id: 'interaction_1', tool_call_id: 'call_write', step_id: 'step_waiting' })
  })

  it('creates a new socket after disconnect and resubscribes the restored waiting turn', async () => {
    const events: StreamEvent[] = []
    subscriptions.push(chatWs.onSessionStream(event => events.push(event)))
    await chatWs.connect()
    chatWs.subscribeSession(SESSION)
    socket().receive('state_sync', { session_id: SESSION }, checkpoint('waiting_user'))
    const first = socket()
    first.close()
    expect(chatWs.isConnected).toBe(false)
    await vi.advanceTimersByTimeAsync(1000)
    const reconnected = socket()
    expect(reconnected).not.toBe(first)
    expect(MockWebSocket.sockets).toHaveLength(2)
    expect(chatWs.isConnected).toBe(true)
    expect(reconnected.frames).toContainEqual({ op: 'session.subscribe', session_id: SESSION })
    expect(reconnected.frames).toContainEqual({ op: 'state.sync', turn_id: TURN, session_id: SESSION })
    reconnected.receive('interrupt', { session_id: SESSION, kind: 'approval', interrupt_id: 'interaction_1', tool_call_id: 'call_write' }, checkpoint('waiting_user', 6))
    expect(events[events.length - 1]?.type).toBe('interrupt')
  })

  it('settles the original send promise from a terminal snapshot when done was lost before reconnect', async () => {
    await chatWs.connect()
    const events: StreamEvent[] = []
    const promise = chatWs.sendMessage('write', SESSION, event => events.push(event), TURN)
    let settled = false
    void promise.then(() => { settled = true })
    await microtasks()
    socket().receive('session', { session_id: SESSION })
    socket().close()
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket().frames).toContainEqual({ op: 'state.sync', turn_id: TURN, session_id: SESSION })
    expect(settled).toBe(false)
    socket().receive('state_sync', { session_id: SESSION }, checkpoint('completed', 8))
    expect(await promise).toMatchObject({ session_id: SESSION, turn_id: TURN })
    expect(events[events.length - 1]?.state?.turn_status).toBe('completed')
    // Cleanup removes the completed turn from subsequent reconnect syncs.
    socket().close()
    await vi.advanceTimersByTimeAsync(1000)
    expect(socket().frames.some(frame => frame.op === 'state.sync')).toBe(false)
  })

  it('keeps an invalid answer nonterminal and accepts a later authoritative done', async () => {
    await chatWs.connect()
    const events: StreamEvent[] = []
    const promise = chatWs.sendMessage('ask', SESSION, event => events.push(event), TURN)
    let settled = false
    void promise.then(() => { settled = true })
    await microtasks()
    socket().receive('interrupt', { session_id: SESSION, kind: 'ask_user', interrupt_id: 'interaction_1',
      tool_call_id: 'call_write', prompt: 'Choose', options: ['A'] }, checkpoint('waiting_user'))
    chatWs.answerAskUser(TURN, 'invalid', { sessionId: SESSION, interruptId: 'interaction_1', toolCallId: 'call_write', stepId: 'step_waiting' })
    socket().receive('error', { session_id: SESSION, error: '请选择有效答案', reason_code: 'invalid_answer' })
    await microtasks()
    expect(settled).toBe(false)
    expect(events[events.length - 1]).toMatchObject({ type: 'stage', stage: 'interaction_rejected' })
    expect(socket().frames[socket().frames.length - 1]).toMatchObject({ op: 'state.sync', turn_id: TURN, session_id: SESSION })
    chatWs.answerAskUser(TURN, 'A', { sessionId: SESSION, interruptId: 'interaction_1', toolCallId: 'call_write', stepId: 'step_waiting' })
    socket().receive('done', { session_id: SESSION, content: 'Accepted A' }, checkpoint('completed', 8))
    await vi.advanceTimersByTimeAsync(50)
    expect(await promise).toMatchObject({ content: 'Accepted A', session_id: SESSION, turn_id: TURN })
    expect(events[events.length - 1]?.type).toBe('done')
  })
})
