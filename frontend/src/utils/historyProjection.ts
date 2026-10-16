import type { ChatMessage, MessageContent, TurnTimeline } from '@/api/session'
import type { AgentStateStream, ChatUiMessage, MessageSegment, ToolSegment } from '@/types/chat-ui'
import { createAgentStateStream, finalizeAgentState, redactToolInputs, toolExecutionFromSegment } from './agentStateStream'

export function displayContent(content: MessageContent | undefined): string {
  if (typeof content === 'string') return content
  return (content || []).filter(block => block.type === 'text' && typeof block.text === 'string').map(block => block.text).join('\n')
}

export function toolOutcome(value: unknown): ToolSegment['status'] {
  return ['running', 'succeeded', 'failed', 'cancelled', 'denied', 'skipped', 'unknown'].includes(String(value))
    ? value as ToolSegment['status'] : 'unknown'
}

export function buildHistoryMessages(raw: ChatMessage[], timelines: TurnTimeline[] = []): ChatUiMessage[] {
  const results = new Map(raw.filter(row => row.role === 'tool' && row.tool_call_id).map(row => [`${row.turn_id}:${row.tool_call_id}`, row]))
  const calls = new Map(timelines.flatMap(turn => (turn.tools || []).map(call => [`${turn.turn_id}:${call.call_id}`, call] as const)))
  const messages: ChatUiMessage[] = []
  const seen = new Set<string>()
  for (const row of raw) {
    if (row.message_id && seen.has(row.message_id)) continue
    if (row.message_id) seen.add(row.message_id)
    if (row.role !== 'user' && row.role !== 'assistant') continue
    const segments: MessageSegment[] = []
    const text = displayContent(row.content)
    if (text) segments.push({ type: 'text', id: (row.seq || messages.length + 1) * 1000, content: text })
    for (const [index, call] of (row.tool_calls || []).entries()) {
      const result = results.get(`${row.turn_id}:${call.id}`)
      const history = calls.get(`${row.turn_id}:${call.id}`)
      const reference = result?.tool_result_reference
      segments.push({
        type: 'tool', id: (row.seq || messages.length + 1) * 1000 + index + 1,
        tool: call.name, args: redactToolInputs(call.arguments), toolCallId: call.id,
        result: result?.tool_result_preview || displayContent(result?.content),
        toolResultRef: result?.tool_result_ref || reference?.path,
        toolResultTruncated: Boolean(result?.tool_result_truncated || reference),
        status: toolOutcome(result?.tool_status || history?.status), reasonCode: result?.tool_reason_code || history?.reason_code,
        definitionSeq: history?.definition_seq || row.seq, resultSeq: history?.result_seq || result?.seq,
        attempts: history?.attempts,
        startedAt: history?.attempts?.[0]?.started_at,
        endedAt: history?.attempts?.[history.attempts.length - 1]?.finished_at,
      })
    }
    messages.push({ id: row.seq || messages.length + 1, messageId: row.message_id || undefined, turnId: row.turn_id,
      ledgerSeq: row.seq, role: row.role, content: text, timestamp: new Date(row.timestamp || 0),
      segments: row.role === 'assistant' ? segments : undefined })
  }
  return messages
}

export function buildHistoryTimelines(messages: ChatUiMessage[], timelines: TurnTimeline[]): Record<number, AgentStateStream> {
  const states: Record<number, AgentStateStream> = {}
  for (const turn of timelines) {
    const user = messages.find(message => message.role === 'user' && message.turnId === turn.turn_id)
    if (!user) continue
    const state = createAgentStateStream(user.id, turn.turn_id, user.timestamp.getTime())
    state.nodes = (turn.steps || []).map(step => ({ id: `checkpoint:${step.step_id}`, stepId: step.step_id,
      kind: step.step_name === 'tools' ? 'tool' : 'phase', status: step.status === 'failed' ? 'error' : 'observing',
      label: step.step_name === 'tools' ? '工具执行' : 'Agent 执行', startedAt: step.started_at || user.timestamp.getTime(),
      endedAt: step.finished_at, active: false }))
    state.toolExecutions = messages.filter(message => message.turnId === turn.turn_id).flatMap(message => (message.segments || []).filter((segment): segment is ToolSegment => segment.type === 'tool').map(toolExecutionFromSegment))
    const terminal = turn.status === 'failed' ? 'error' : turn.status === 'cancelled' ? 'cancelled' : turn.status === 'handoff' ? 'handoff' : 'completed'
    if (['completed', 'failed', 'cancelled', 'handoff'].includes(turn.status || '')) {
      finalizeAgentState(state, terminal, turn.reason_code || (terminal === 'completed' ? '本轮完成' : terminal === 'error' ? '本轮失败' : terminal === 'handoff' ? '已交还用户' : '本轮已取消'), turn.steps?.[turn.steps.length - 1]?.finished_at || user.timestamp.getTime())
    }
    states[user.id] = state
  }
  return states
}
