import type { AgentCheckpointState } from '@/api/chat'

const REQUIRED = [
  'checkpoint_id', 'scope', 'context', 'max_step_every_run',
  'tasks', 'current_task', 'current_step', 'memory',
] as const

export function isCanonicalAgentState(value: unknown): value is AgentCheckpointState {
  if (!value || typeof value !== 'object') return false
  const state = value as Record<string, unknown>
  if (!REQUIRED.every((key) => key in state)) return false
  const scope = state.scope as Record<string, unknown> | null
  const context = state.context as Record<string, unknown> | null
  const step = state.current_step as Record<string, unknown> | null
  return Boolean(
    typeof state.checkpoint_id === 'string' &&
    Number.isInteger(state.state_revision) &&
    scope && ['workspace_id', 'session_id', 'run_id']
      .every((key) => typeof scope[key] === 'string' && scope[key]) &&
    ['task_id', 'request_id', 'step_id']
      .every((key) => scope[key] === null || (typeof scope[key] === 'string' && Boolean(scope[key]))) &&
    context && Array.isArray(context.working_message) &&
    Number.isInteger(state.max_step_every_run) && Array.isArray(state.tasks) &&
    (state.current_task === null || typeof state.current_task === 'string') &&
    step && Array.isArray(step.tool_calls) && state.memory !== null && typeof state.memory === 'object'
  )
}

export function readCanonicalAgentState(value: unknown): AgentCheckpointState | undefined {
  return isCanonicalAgentState(value) ? value : undefined
}
