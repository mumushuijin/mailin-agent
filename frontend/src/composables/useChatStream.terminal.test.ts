import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { StreamEvent } from '@/api/chat'
import { applyChatStreamEvent, type ChatStreamState } from '@/composables/useChatStream'
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

function makeCtx(): ChatStreamState {
  return {
    messages: ref<ChatUiMessage[]>([]),
    currentSessionId: ref<string | null>('s1'),
    contextUsage: ref(null),
    apiUsage: ref(null),
    contextCompressing: ref(false),
    activeStage: ref<string | null>('waiting_user'),
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
})
