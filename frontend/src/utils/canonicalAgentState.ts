import type { AgentCheckpointState } from '@/api/chat'

const object = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const identity = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const legacyKeys = ['run_id', 'run_status', 'max_step_every_run', 'meta', 'session', 'data', 'task']
const nonnegative = (value: unknown): boolean => Number.isInteger(value) && Number(value) >= 0
const positive = (value: unknown): boolean => Number.isInteger(value) && Number(value) > 0
const millis = (value: unknown): boolean => nonnegative(value)
const toolState = (call: unknown): boolean => object(call) && identity(call.call_id) &&
  !['args', 'input', 'output'].some(key => key in call) &&
  ['pending', 'running', 'waiting_user', 'succeeded', 'failed', 'cancelled', 'denied', 'skipped', 'unknown'].includes(String(call.status)) &&
  ['definition_seq', 'result_seq', 'attempt'].every(key => !(key in call) || positive(call[key])) &&
  ['started_at', 'ended_at', 'finished_at'].every(key => !(key in call) || millis(call[key]))

export function isCanonicalAgentState(value: unknown): value is AgentCheckpointState {
  if (!object(value) || value.schema_version !== 2 || legacyKeys.some(key => key in value)) return false
  const state = value as Record<string, unknown>
  const scope = state.scope as Record<string, unknown> | null
  const context = state.context as Record<string, unknown> | null
  const step = state.current_step as Record<string, unknown> | null
  return Boolean(
    typeof state.checkpoint_id === 'string' && /^cp_[0-9a-f]{32}$/.test(state.checkpoint_id) &&
    Number.isInteger(state.state_revision) && Number(state.state_revision) >= 0 &&
    scope && ['workspace_id', 'session_id', 'turn_id']
      .every((key) => typeof scope[key] === 'string' && scope[key]) &&
    Object.keys(scope).length === 3 &&
    context && Array.isArray(context.working_message) &&
    Number.isInteger(state.max_step_every_turn) && Number(state.max_step_every_turn) > 0 && Array.isArray(state.tasks) && state.tasks.every(identity) &&
    (state.current_task === null || (identity(state.current_task) && state.tasks.includes(state.current_task))) &&
    ['created', 'running', 'waiting_user', 'completed', 'failed', 'cancelled', 'handoff'].includes(String(state.turn_status)) &&
    object(state.ledger_pointer) && !('file' in state.ledger_pointer) && nonnegative(state.ledger_pointer.applied_seq) &&
    step && (step.step_id === null || identity(step.step_id)) && Array.isArray(step.tool_calls) && step.tool_calls.every(toolState) && !('token_info' in step) &&
    ['pending', 'running', 'waiting_user', 'completed', 'failed', 'cancelled', 'skipped'].includes(String(step.step_status)) &&
    ['started_at', 'ended_at'].every(key => !(key in step) || millis(step[key])) &&
    (step.model === undefined || step.model === null || (object(step.model) && identity(step.model.request_id) && (!object(step.model.usage) || !('total_tokens' in step.model.usage)))) &&
    object(state.memory) && !('last_synced_run' in state.memory) && Number.isInteger(state.memory.recorded_count) &&
    ('last_synced_turn' in state.memory) && (state.memory.last_synced_turn === null || identity(state.memory.last_synced_turn)) && Array.isArray(state.memory.source_files)
  )
}

export function readCanonicalAgentState(value: unknown): AgentCheckpointState | undefined {
  if (value === undefined || value === null) return undefined
  if (!isCanonicalAgentState(value)) throw new Error('不支持的状态协议：需要 checkpoint schema_version=2 和 turn 字段；旧数据请手动清理后新建会话。')
  return value
}

export function rejectLegacyTurnFields(value: Record<string, unknown>): void {
  if (['run_id', 'run_status', 'max_step_every_run', 'last_synced_run'].some(key => key in value)) {
    throw new Error('不支持旧 run 协议，请使用配套版本的客户端和新格式会话。')
  }
}
