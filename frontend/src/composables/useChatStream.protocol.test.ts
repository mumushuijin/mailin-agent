import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { applyChatStreamEvent, type ChatStreamState } from './useChatStream'
import { createAgentStateStream } from '@/utils/agentStateStream'
import type { AgentCheckpointState, StreamEvent } from '@/api/chat'
import type { MessageSegment } from '@/types/chat-ui'
import { mapWsToStreamEvent } from '@/api/ws'

vi.mock('@/api/config', () => ({ configApi: { getAgentInfo: vi.fn().mockResolvedValue({ name: '麦林' }) } }))
vi.mock('ant-design-vue', () => ({ message: { warning: vi.fn(), error: vi.fn() } }))
const state = (revision: number, status = 'running'): AgentCheckpointState => ({
  schema_version: 3, checkpoint_id: `cp_${'b'.repeat(32)}`, state_revision: revision,
  scope: { workspace_id: 'w1', session_id: 's1', turn_id: 'turn_1' },
  context: { working_message: [] }, max_step_every_turn: 48, tasks: [], current_task: null,
  current_step: { step_id: 'step1', step_status: 'running', tool_calls: [], model: null },
  memory: { recorded_count: 0, last_synced_turn: null, source_files: [] }, turn_status: status,
  ledger_pointer: { applied_seq: 900, event_cursor: { branch_id: 'main', event_seq: 900, event_id: 'event-900' } },
})
function context(): ChatStreamState {
  return { messages: ref([]), currentSessionId: ref('s1'), contextUsage: ref(null), apiUsage: ref(null),
    contextCompressing: ref(false), activeStage: ref(null), agentState: ref(createAgentStateStream(1, 'turn_1')),
    pendingApproval: ref(null), pendingAskUser: ref(null), sessionTodos: ref([]), assistantName: ref('麦林'),
    saveCurrentSession: () => {}, updateMessageSegments: () => {}, finalizeRunningToolSegments: () => {},
    markRunTerminal: vi.fn(), scrollToBottom: () => {} }
}
const stream = () => ({ assistantMsgIndex: -1, currentSegments: [] as MessageSegment[], currentTextSegmentId: -1 })
const event = (type: StreamEvent['type'], seq: number, canonical?: AgentCheckpointState): StreamEvent => ({ type, turn_id: 'turn_1', session_id: 's1', seq, event_id: `e${seq}`, state: canonical })

describe('checkpoint and turn stream semantics', () => {
  it('accepts a new transport baseline on restart while preserving revision and ledger cursors', () => {
    const ctx = context(); const session = stream()
    applyChatStreamEvent(event('state_sync', 100, state(4)), ctx, session)
    applyChatStreamEvent(event('state_sync', 1, state(5)), ctx, session)
    applyChatStreamEvent({ ...event('stage', 2, state(6)), stage: 'thinking' }, ctx, session)
    expect(ctx.agentState?.value?.confirmedRevision).toBe(6)
    expect(ctx.agentState?.value?.transportSeq).toBe(2)
    expect(ctx.agentState?.value?.ledgerSeq).toBe(900)
    expect(ctx.agentState?.value?.currentTask).toBeNull()
    expect(ctx.agentState?.value?.requestId).toBeNull()
  })

  it('restores waiting, clears accepted interactions on running, and applies a terminal sync without done', () => {
    const ctx = context(); const session = stream()
    applyChatStreamEvent(event('state_sync', 1, state(1, 'waiting_user')), ctx, session)
    expect(ctx.agentState?.value?.requiresUserAction).toBe(true)
    ctx.pendingApproval.value = { turnId: 'turn_1', tool: 'write_file', args: {}, reason: 'confirm' }
    applyChatStreamEvent(event('state_sync', 2, state(2)), ctx, session)
    expect(ctx.pendingApproval.value).toBeNull()
    expect(ctx.agentState?.value?.requiresUserAction).toBe(false)
    applyChatStreamEvent(event('state_sync', 3, state(3, 'failed')), ctx, session)
    expect(ctx.agentState?.value?.status).toBe('error')
    expect(ctx.markRunTerminal).toHaveBeenCalledWith('error')
  })

  it('rejects an invalid answer without ending the turn and accepts its later completion', () => {
    const ctx = context(); const session = stream()
    applyChatStreamEvent({ ...event('interrupt', 1, state(1, 'waiting_user')), kind: 'ask_user', interrupt_id: 'i1', prompt: 'q' }, ctx, session)
    applyChatStreamEvent({ ...event('error', 2), reason_code: 'invalid_answer', error: 'retry' }, ctx, session)
    expect(ctx.markRunTerminal).not.toHaveBeenCalled()
    expect(ctx.pendingAskUser.value?.interruptId).toBe('i1')
    applyChatStreamEvent(event('state_sync', 3, state(2)), ctx, session)
    applyChatStreamEvent(event('done', 4, state(3, 'completed')), ctx, session)
    expect(ctx.markRunTerminal).toHaveBeenCalledWith('done')
  })

  it('uses the checkpoint failed outcome on done and does not invent tool success from response text', () => {
    const ctx = context(); const session = stream()
    const started = applyChatStreamEvent({ ...event('tool_start', 1), tool: 'run_shell', tool_call_id: 'c1' }, ctx, session)
    session.assistantMsgIndex = started.assistantMsgIndex
    applyChatStreamEvent({ ...event('tool_finish', 2), tool_call_id: 'c1', result: 'looks fine', status: 'unknown', result_seq: 42 }, ctx, session)
    const tool = session.currentSegments[0]
    expect(tool?.type === 'tool' ? tool.status : null).toBe('unknown')
    expect(tool?.type === 'tool' ? tool.resultSeq : null).toBe(42)
    applyChatStreamEvent(event('done', 3, state(1, 'failed')), ctx, session)
    expect(ctx.markRunTerminal).toHaveBeenCalledWith('error')
  })

  it('normalizes WS business statuses and rejects legacy envelope fields explicitly', () => {
    const normalized = mapWsToStreamEvent({ stream_schema_version: 3, type: 'tool_finish', turn_id: 'turn_1', data: { status: 'denied', result_seq: 8, tool_call_id: 'c1' } })
    expect(normalized?.status).toBe('denied'); expect(normalized?.result_seq).toBe(8)
    expect(() => mapWsToStreamEvent({ stream_schema_version: 3, type: 'session', run_id: 'old' } as never)).toThrow('旧 run')
  })
})
