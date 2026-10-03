import type { StreamEvent } from '../api/chat'
import type {
  AgentStateNode,
  AgentStateNodeKind,
  AgentStateStream,
  AgentStatus,
  ToolExecution,
  ToolSegment,
} from '../types/chat-ui'

export interface AgentStatusMeta {
  label: string
  icon: string
  tone: 'neutral' | 'active' | 'warning' | 'danger' | 'success'
  terminal: boolean
}

export const AGENT_STATUS_META: Record<AgentStatus, AgentStatusMeta> = {
  idle: { label: '准备开始', icon: '○', tone: 'neutral', terminal: false },
  initializing: { label: '正在加载上下文…', icon: '⚡', tone: 'active', terminal: false },
  planning: { label: '正在拆解任务…', icon: '🗺️', tone: 'active', terminal: false },
  reasoning: { label: '正在思考中…', icon: '🧠', tone: 'active', terminal: false },
  tool_calling: { label: '正在执行操作…', icon: '🛠️', tone: 'active', terminal: false },
  observing: { label: '正在检查执行结果…', icon: '👁️', tone: 'active', terminal: false },
  summarizing: { label: '正在生成回复…', icon: '✍️', tone: 'active', terminal: false },
  memory_sync: { label: '正在归档记忆…', icon: '💾', tone: 'active', terminal: false },
  awaiting_user: { label: '等待授权继续', icon: '⏸️', tone: 'warning', terminal: false },
  completed: { label: '任务完成', icon: '✨', tone: 'success', terminal: true },
  cancelled: { label: '本轮已取消', icon: '⏹️', tone: 'warning', terminal: true },
  handoff: { label: '已交还用户', icon: '↪️', tone: 'neutral', terminal: true },
  error: { label: '遇到了一点问题', icon: '❌', tone: 'danger', terminal: true },
}

export const TERMINAL_STATUSES = new Set<AgentStatus>(['completed', 'cancelled', 'handoff', 'error'])

const TERMINAL_STATUS_PRIORITY: Record<Extract<AgentStatus, 'completed' | 'cancelled' | 'handoff' | 'error'>, number> = {
  completed: 1,
  cancelled: 2,
  handoff: 3,
  error: 4,
}

const normalizeStage = (stage?: string | null): string => (stage || '').trim().toLowerCase()

export function mapStreamEventToAgentStatus(
  event: Pick<StreamEvent, 'type' | 'stage' | 'status' | 'compression' | 'cancelled' | 'handoff' | 'terminal_reason'>,
): AgentStatus | null {
  if (event.type === 'done') {
    if (event.handoff || event.terminal_reason === 'handoff') return 'handoff'
    if (event.cancelled) return 'cancelled'
    return 'completed'
  }
  if (event.type === 'error') return event.cancelled ? 'cancelled' : 'error'
  if (event.type === 'interrupt') return 'awaiting_user'
  if (event.type === 'tool_start' || event.type === 'tool_progress') return 'tool_calling'
  if (event.type === 'tool_finish') return 'observing'
  if (event.type === 'step_start') return 'reasoning'
  if (event.type === 'step_finish') return 'observing'
  if (event.type === 'compression') {
    return event.compression?.status === 'done' ? 'observing' : 'initializing'
  }

  const stage = normalizeStage(event.stage)
  if (stage.includes('context') || stage.includes('history') || stage === 'accepted' || stage === 'connecting') {
    return 'initializing'
  }
  if (stage.includes('plan')) return 'planning'
  if (stage.includes('think') || stage.includes('model_stream')) return 'reasoning'
  if (stage.includes('tool')) return 'tool_calling'
  if (stage.includes('wait') || stage.includes('approval') || stage.includes('user')) return 'awaiting_user'
  if (stage.includes('memory')) return 'memory_sync'
  if (stage.includes('postprocess') || stage.includes('final')) return 'summarizing'
  if (event.type === 'chunk') return 'reasoning'
  return null
}

export function createAgentStateStream(
  turnId: number,
  runId?: string | null,
  startedAt = Date.now(),
): AgentStateStream {
  const state: AgentStateStream = {
    turnId,
    runId: runId || null,
    status: 'initializing',
    currentStep: 0,
    startedAt,
    nodes: [],
    toolExecutions: [],
    todoCompleted: 0,
    todoTotal: 0,
    requiresUserAction: false,
    confirmedSeq: null,
    syncing: false,
    activeMessageId: null,
  }
  advanceAgentState(state, 'initializing', startedAt)
  return state
}

export function shouldAcceptAgentEvent(
  state: AgentStateStream,
  event: Pick<StreamEvent, 'run_id'>,
): boolean {
  return !state.runId || !event.run_id || state.runId === event.run_id
}

function nodeKindForStatus(status: AgentStatus): AgentStateNodeKind {
  if (TERMINAL_STATUSES.has(status)) return 'terminal'
  if (status === 'tool_calling') return 'tool'
  if (status === 'observing') return 'observation'
  return 'phase'
}

export function isTerminalAgentStatus(status?: AgentStatus | null): boolean {
  return Boolean(status && TERMINAL_STATUSES.has(status))
}

function terminalPriority(status: AgentStatus): number {
  return isTerminalAgentStatus(status)
    ? TERMINAL_STATUS_PRIORITY[status as keyof typeof TERMINAL_STATUS_PRIORITY]
    : 0
}

export function getAgentStateTrail(state?: AgentStateStream | null, limit = 4): AgentStateNode[] {
  if (!state || limit <= 0) return []

  const currentId = state.nodes[state.nodes.length - 1]?.id
  const trail: AgentStateNode[] = []

  for (const node of state.nodes) {
    if (!node || node.id === currentId || isTerminalAgentStatus(node.status)) continue
    trail.push(node)
  }

  return trail.slice(-limit)
}

export function advanceAgentState(
  state: AgentStateStream,
  status: AgentStatus,
  now = Date.now(),
  summary?: string,
): AgentStateNode {
  const currentStatus = state.status
  if (isTerminalAgentStatus(currentStatus)) {
    if (!isTerminalAgentStatus(status)) {
      return state.nodes[state.nodes.length - 1]!
    }

    const currentNode = state.nodes[state.nodes.length - 1]
    if (!currentNode) {
      throw new Error('终态状态流缺少当前节点')
    }

    if (status === currentStatus || terminalPriority(status) <= terminalPriority(currentStatus)) {
      if (summary && status === currentStatus) currentNode.summary = summary
      return currentNode
    }

    const meta = AGENT_STATUS_META[status]
    currentNode.kind = 'terminal'
    currentNode.status = status
    currentNode.label = meta.label
    currentNode.summary = summary || currentNode.summary
    currentNode.active = false
    currentNode.endedAt = now
    state.status = status
    state.endedAt = now
    state.terminalLabel = summary || state.terminalLabel
    state.requiresUserAction = false
    return currentNode
  }

  const current = state.nodes[state.nodes.length - 1]
  if (current && current.active && current.status === status) {
    if (summary) current.summary = summary
    state.status = status
    state.requiresUserAction = status === 'awaiting_user'
    return current
  }

  if (current?.active) {
    current.active = false
    current.endedAt = now
  }

  const meta = AGENT_STATUS_META[status]
  const node: AgentStateNode = {
    id: `${state.turnId}:${status}:${state.nodes.length + 1}`,
    kind: nodeKindForStatus(status),
    status,
    label: meta.label,
    startedAt: now,
    active: !meta.terminal,
    summary,
  }
  state.nodes.push(node)
  state.status = status
  state.requiresUserAction = status === 'awaiting_user'
  if (meta.terminal) {
    state.endedAt = now
  }
  return node
}

export function setAgentStateSyncing(state: AgentStateStream, syncing: boolean, confirmedSeq?: number): void {
  state.syncing = syncing
  if (typeof confirmedSeq === 'number') state.confirmedSeq = confirmedSeq
}

export function attachAgentStateMessage(state: AgentStateStream, messageId: number): void {
  state.activeMessageId = messageId
  const current = state.nodes[state.nodes.length - 1]
  if (current) current.messageId = messageId
}

export function finalizeAgentState(
  state: AgentStateStream,
  status: Extract<AgentStatus, 'completed' | 'cancelled' | 'handoff' | 'error'>,
  terminalLabel: string,
  now = Date.now(),
): void {
  const node = advanceAgentState(state, status, now, terminalLabel)
  node.active = false
  node.endedAt = now
  state.endedAt = state.endedAt || now
  if (state.status === status || !state.terminalLabel) state.terminalLabel = terminalLabel
  state.requiresUserAction = false
}

export function upsertToolExecution(
  state: AgentStateStream,
  tool: ToolExecution,
): ToolExecution {
  const index = state.toolExecutions.findIndex((item) => item.toolId === tool.toolId)
  if (index === -1) {
    state.toolExecutions.push(tool)
    return tool
  }
  state.toolExecutions[index] = { ...state.toolExecutions[index], ...tool }
  return state.toolExecutions[index]
}

export function redactToolValue(value: unknown, depth = 0): unknown {
  if (depth > 3) return '[已截断]'
  if (typeof value === 'string') {
    return value.length > 500 ? `${value.slice(0, 497)}...` : value
  }
  if (Array.isArray(value)) return value.slice(0, 20).map((item) => redactToolValue(item, depth + 1))
  if (value && typeof value === 'object') {
    const source = value as Record<string, unknown>
    return Object.fromEntries(
      Object.entries(source).map(([key, item]) => [
        key,
        /token|secret|password|authorization|cookie|api[_-]?key|private[_-]?key|env(?:ironment)?/i.test(key)
          ? '[已脱敏]'
          : redactToolValue(item, depth + 1),
      ]),
    )
  }
  return value
}

export function redactToolInputs(args: Record<string, unknown> | undefined): Record<string, unknown> {
  return (redactToolValue(args || {}) as Record<string, unknown>) || {}
}

export function summarizeToolOutput(result: string | undefined | null, maxLength = 500): string | null {
  if (!result) return null
  const text = result.trim()
  if (!text) return null
  return text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text
}

export function toolExecutionFromSegment(segment: ToolSegment): ToolExecution {
  const startedAt = segment.startedAt || Date.now()
  return {
    toolId: segment.toolCallId || `segment:${segment.id}`,
    toolName: segment.tool,
    status: segment.status,
    inputs: redactToolInputs(segment.args),
    outputs: summarizeToolOutput(segment.result),
    progressMessage: segment.progressMessage || null,
    startedAt,
    endedAt: segment.endedAt,
    redacted: true,
  }
}

export function findRunningToolSegment(
  segments: Array<{ type: 'text'; id: number; content: string } | ToolSegment>,
  toolCallId?: string,
  toolName?: string,
): ToolSegment | undefined {
  if (toolCallId) {
    const exact = segments.find(
      (segment): segment is ToolSegment => (
        segment.type === 'tool' && segment.status === 'running' && segment.toolCallId === toolCallId
      ),
    )
    if (exact) return exact
  }
  return [...segments]
    .reverse()
    .find((segment): segment is ToolSegment => (
      segment.type === 'tool'
      && segment.status === 'running'
      && (!toolName || segment.tool === toolName)
    ))
}
