import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { StreamEvent } from '@/api/chat'
import { createChatStreamEventBatcher, type ChatStreamState } from '@/composables/useChatStream'
import type { ChatUiMessage, MessageSegment } from '@/types/chat-ui'

vi.mock('@/api/config', () => ({
  configApi: { getAgentInfo: vi.fn().mockResolvedValue({ name: '麦林' }) },
}))

const baseCtx = (): ChatStreamState => ({
  messages: ref<ChatUiMessage[]>([]),
  currentSessionId: ref('s1'),
  contextUsage: ref(null),
  apiUsage: ref(null),
  contextCompressing: ref(false),
  activeStage: ref(null),
  pendingApproval: ref(null),
  pendingAskUser: ref(null),
  sessionTodos: ref([]),
  assistantName: ref('麦林'),
  saveCurrentSession: () => {},
  updateMessageSegments: (index, segments) => {
    const message = ctx.messages.value[index]
    if (message) message.segments = segments
  },
  finalizeRunningToolSegments: () => {},
  markRunTerminal: vi.fn(),
  scrollToBottom: () => {},
})

let ctx: ChatStreamState

const event = (payload: Partial<StreamEvent> & Pick<StreamEvent, 'type'>, seq: number): StreamEvent => ({
  ...payload,
  run_id: 'run-batch',
  seq,
  event_id: `run-batch-${seq}`,
})

describe('createChatStreamEventBatcher', () => {
  it('coalesces adjacent chunks without changing text order', () => {
    ctx = baseCtx()
    const batcher = createChatStreamEventBatcher(ctx)

    batcher.handle(event({ type: 'step_start', step: 1 }, 1))
    batcher.handle(event({ type: 'chunk', content: 'a' }, 2))
    batcher.handle(event({ type: 'chunk', content: 'b' }, 3))
    batcher.flush()

    const text = ctx.messages.value[0]?.segments?.find((segment): segment is Extract<MessageSegment, { type: 'text' }> => segment.type === 'text')
    expect(text?.content).toBe('ab')
  })

  it('keeps lifecycle boundaries ordered around coalesced progress', () => {
    ctx = baseCtx()
    const batcher = createChatStreamEventBatcher(ctx)

    batcher.handle(event({ type: 'tool_start', tool: 'run_shell', args: {}, tool_call_id: 'call-1' }, 1))
    batcher.handle(event({ type: 'tool_progress', tool: 'run_shell', tool_call_id: 'call-1', message: '第一段' }, 2))
    batcher.handle(event({ type: 'tool_progress', tool: 'run_shell', tool_call_id: 'call-1', message: '第二段' }, 3))
    batcher.handle(event({ type: 'tool_finish', tool: 'run_shell', tool_call_id: 'call-1', result: '完成' }, 4))
    batcher.flush()

    const tool = ctx.messages.value[0]?.segments?.find((segment): segment is Extract<MessageSegment, { type: 'tool' }> => segment.type === 'tool')
    expect(tool?.status).toBe('done')
    expect(tool?.result).toBe('完成')
    expect(tool?.progressMessage).toBe('第二段')
  })

  it('produces the same projection for equivalent WebSocket and SSE event sequences', () => {
    const sequence = [
      event({ type: 'step_start', step: 1 }, 1),
      event({ type: 'chunk', content: '回答' }, 2),
      event({ type: 'done', content: '回答' }, 3),
    ]
    const snapshots = sequence.map(() => {
      ctx = baseCtx()
      const batcher = createChatStreamEventBatcher(ctx)
      for (const item of sequence) batcher.handle(item)
      batcher.flush()
      return JSON.stringify(ctx.messages.value.map((message) => ({
        role: message.role,
        content: message.content,
        segments: message.segments?.map((segment) => ({
          type: segment.type,
          content: segment.type === 'text' ? segment.content : undefined,
          status: segment.type === 'tool' ? segment.status : undefined,
          result: segment.type === 'tool' ? segment.result : undefined,
        })),
      })))
    })

    expect(snapshots[0]).toBe(snapshots[1])
    expect(snapshots[1]).toBe(snapshots[2])
  })
})
