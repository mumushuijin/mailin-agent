import type { StreamEvent } from '@/api/chat'

const REJECTED_INTERACTIONS = new Set(['invalid_answer', 'stale_interrupt', 'stale_step', 'stale_tool_call', 'stale_interaction_kind', 'stale_interaction_mode', 'stale_mode', 'stale_turn', 'stale_session'])
export function normalizeInteractionRejection(event: StreamEvent): StreamEvent {
  if (event.type === 'error' && REJECTED_INTERACTIONS.has(event.reason_code || '')) {
    return { ...event, type: 'stage', stage: 'interaction_rejected', status: 'degraded' }
  }
  return event
}

export function terminalOutcome(event: Pick<StreamEvent, 'type' | 'state' | 'handoff' | 'cancelled' | 'terminal_reason'>): 'completed' | 'error' | 'cancelled' | 'handoff' | null {
  const status = event.state?.turn_status
  if (status === 'failed') return 'error'
  if (status === 'completed' || status === 'cancelled' || status === 'handoff') return status
  if (event.type === 'state_sync') return null
  if (event.handoff || event.terminal_reason === 'handoff') return 'handoff'
  if (event.cancelled) return 'cancelled'
  return event.type === 'done' ? 'completed' : event.type === 'error' ? 'error' : null
}
