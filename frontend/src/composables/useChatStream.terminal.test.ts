import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { StreamEvent } from '@/api/chat'
import { applyChatStreamEvent, type ChatStreamState } from '@/composables/useChatStream'
import { createAgentStateStream } from '@/utils/agentStateStream'
import type {
  ChatUiMessage,
  MessageSegment,
  PendingApproval,
  PendingAskUser,
  SessionTodoItem,
} from '@/types/chat-ui'

vi.mock('@/api/config', () => ({
  configApi: { getAgentInfo: vi.fn().mockResolvedValue({ name: '麦林' }) },
}))

function makeCtx(withAgentState = false): ChatStreamState {
  return {
    messages: ref<ChatUiMessage[]>([]),
    currentSessionId: ref<string | null>('s1'),
    contextUsage: ref(null),
    apiUsage: ref(null),
    contextCompressing: ref(false),
    activeStage: ref<string | null>('waiting_user'),
    agentState: withAgentState ? ref(createAgentStateStream(1, 'r1', 100)) : undefined,
    pendingApproval: ref<PendingApproval | null>({
      runId: 'r1',
      tool: 'write_file',
      args: {},
      reason: 'need',
    }),
    pendingAskUser: ref<PendingAskUser | null>({
      runId: 'r1',
      prompt: 'q',
      options: [],
      allowMultiple: false,
      mode: 'answer_and_continue',
    }),
    sessionTodos: ref<SessionTodoItem[]>([]),
    assistantName: ref('麦林'),
    saveCurrentSession: () => {},
    updateMessageSegments: () => {},
    finalizeRunningToolSegments: () => {},
    markRunTerminal: vi.fn(),
    scrollToBottom: () => {},
  }
}

describe('useChatStream terminal cleanup', () => {
  const canonicalState = (runId: string, revision: number, todos: unknown[] = []) => ({
    checkpoint_id: `cp-${revision}`, state_revision: revision,
    scope: { workspace_id: 'w1', session_id: 's1', run_id: runId, task_id: 't1', request_id: 'q1', step_id: 'step1' },
    context: { working_message: [] }, max_step_every_run: 4, tasks: ['t1'], current_task: 't1',
    current_step: { step_id: 'step1', step_name: 'agent', step_status: 'running', tool_calls: [], token_info: {} },
    memory: { recorded_count: 0, last_synced_run: null, source_files: [] },
    task_details: { todos },
  })

  it('clears pending prompts on handoff done', () => {
    const ctx = makeCtx()
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }
    applyChatStreamEvent(
      { type: 'done', handoff: true, terminal_reason: 'handoff', content: '' } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.pendingApproval.value).toBeNull()
    expect(ctx.pendingAskUser.value).toBeNull()
    expect(ctx.activeStage.value).toBeNull()
    expect(ctx.markRunTerminal).toHaveBeenCalledWith('handoff')
  })

  it('clears pending on error/cancel', () => {
    const ctx = makeCtx()
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }
    applyChatStreamEvent(
      { type: 'error', error: '已取消', cancelled: true } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.pendingApproval.value).toBeNull()
    expect(ctx.pendingAskUser.value).toBeNull()
  })

  it('keeps approval modal when a premature done arrives while waiting', () => {
    const ctx = makeCtx()
    ctx.pendingAskUser.value = null
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }
    applyChatStreamEvent(
      { type: 'done', content: '', run_id: 'r1' } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.pendingApproval.value).not.toBeNull()
    expect(ctx.markRunTerminal).not.toHaveBeenCalled()
  })

  it('maps ask_user handoff mode without using approval fields', () => {
    const ctx = makeCtx()
    ctx.pendingAskUser.value = null
    ctx.pendingApproval.value = null
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }
    applyChatStreamEvent(
      {
        type: 'interrupt',
        kind: 'ask_user',
        mode: 'handoff_and_stop',
        prompt: '请查看结果',
        interrupt_id: 'iid',
        run_id: 'r2',
        terminal_on_ack: true,
      } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.pendingAskUser.value?.mode).toBe('handoff_and_stop')
    expect(ctx.pendingApproval.value).toBeNull()
  })

  it('settles ask-user handoff as a terminal state without leaving a running stage', () => {
    const ctx = makeCtx(true)
    ctx.pendingApproval.value = null
    ctx.pendingAskUser.value = null
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }

    applyChatStreamEvent(
      {
        type: 'interrupt',
        kind: 'ask_user',
        mode: 'handoff_and_stop',
        prompt: '请查看结果',
        interrupt_id: 'iid',
        run_id: 'r1',
        terminal_on_ack: true,
      } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.agentState?.value?.status).toBe('awaiting_user')

    applyChatStreamEvent(
      {
        type: 'done',
        handoff: true,
        terminal_reason: 'handoff',
        content: '',
        run_id: 'r1',
      } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.agentState?.value?.status).toBe('handoff')
    expect(ctx.activeStage.value).toBeNull()
    expect(ctx.pendingAskUser.value).toBeNull()
  })

  it('ignores late non-terminal events after a terminal run', () => {
    const ctx = makeCtx(true)
    ctx.pendingApproval.value = null
    ctx.pendingAskUser.value = null
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }

    applyChatStreamEvent(
      { type: 'done', content: '', run_id: 'r1' } as StreamEvent,
      ctx,
      session,
    )
    applyChatStreamEvent(
      { type: 'tool_start', tool: 'write_file', args: {}, run_id: 'r1' } as StreamEvent,
      ctx,
      session,
    )
    applyChatStreamEvent(
      { type: 'stage', stage: 'thinking', run_id: 'r1' } as StreamEvent,
      ctx,
      session,
    )

    expect(ctx.agentState?.value?.status).toBe('completed')
    expect(ctx.activeStage.value).toBeNull()
    expect(ctx.messages.value).toHaveLength(0)
  })

  it('ignores duplicate and late canonical state events while preserving run ordering', () => {
    const ctx = makeCtx()
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }
    const first = {
      type: 'step_start',
      step: 1,
      run_id: 'r3',
      seq: 1,
      event_id: 'r3-1',
      state: canonicalState('r3', 1),
    } as StreamEvent
    applyChatStreamEvent(first, ctx, session)
    applyChatStreamEvent(first, ctx, session)
    applyChatStreamEvent(
      {
        type: 'chunk',
        content: 'late',
        run_id: 'r3',
        seq: 0,
        event_id: 'r3-0',
      } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.messages.value).toHaveLength(1)
    expect(ctx.messages.value[0].content).toBe('')
  })

  it('projects canonical todos and exposes a resync stage on sequence gaps', () => {
    const ctx = makeCtx()
    const session = {
      assistantMsgIndex: -1,
      currentSegments: [] as MessageSegment[],
      currentTextSegmentId: -1,
    }
    applyChatStreamEvent(
      {
        type: 'stage',
        stage: 'thinking',
        run_id: 'r4',
        seq: 1,
        event_id: 'r4-1',
        state: canonicalState('r4', 1, [{ id: '1', content: 'x', status: 'completed' }]),
      } as StreamEvent,
      ctx,
      session,
    )
    applyChatStreamEvent(
      {
        type: 'stage',
        stage: 'thinking',
        run_id: 'r4',
        seq: 3,
        event_id: 'r4-3',
      } as StreamEvent,
      ctx,
      session,
    )
    expect(ctx.sessionTodos.value[0].status).toBe('completed')
    expect(ctx.agentState?.value?.currentStep ?? 2).toBe(2)
    expect(ctx.activeStage.value).toBe('synchronizing')
  })

  it('rejects an older revision and a prior run after the next session starts', () => {
    const ctx = makeCtx()
    const session = { assistantMsgIndex: -1, currentSegments: [] as MessageSegment[], currentTextSegmentId: -1 }
    applyChatStreamEvent({ type: 'session', run_id: 'r1', seq: 1, event_id: 'r1-1' } as StreamEvent, ctx, session)
    applyChatStreamEvent({ type: 'state_sync', run_id: 'r1', seq: 2, event_id: 'r1-2', state: canonicalState('r1', 5) } as StreamEvent, ctx, session)
    applyChatStreamEvent({ type: 'state_sync', run_id: 'r1', seq: 3, event_id: 'r1-3', state: canonicalState('r1', 4) } as StreamEvent, ctx, session)
    expect(ctx.agentState?.value?.confirmedSeq).not.toBe(4)
    applyChatStreamEvent({ type: 'session', run_id: 'r2', seq: 1, event_id: 'r2-1' } as StreamEvent, ctx, session)
    applyChatStreamEvent({ type: 'step_start', run_id: 'r1', seq: 4, event_id: 'r1-4' } as StreamEvent, ctx, session)
    expect(ctx.messages.value).toHaveLength(0)
  })
})
