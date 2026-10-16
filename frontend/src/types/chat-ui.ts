export interface TextSegment {
  type: 'text'
  id: number
  content: string
  presentation?: 'agent' | 'system'
}

export interface ToolSegment {
  type: 'tool'
  id: number
  tool: string
  args: Record<string, unknown>
  toolCallId?: string | null
  result?: string
  progressMessage?: string | null
  startedAt?: number
  endedAt?: number
  toolResultRef?: string | null
  toolResultTruncated?: boolean
  reasonCode?: string | null
  fullResultLoading?: boolean
  status: 'running' | 'succeeded' | 'failed' | 'cancelled' | 'denied' | 'skipped' | 'unknown'
  stepId?: string
  definitionSeq?: number
  resultSeq?: number
  attempts?: ToolAttempt[]
  contentError?: string
  snapshotId?: string | null
  undoStatus?: 'available' | 'conflict' | 'expired' | 'cross_session' | 'unavailable' | null
}

export type MessageSegment = TextSegment | ToolSegment

export type NarrativeNodeKind = 'agent_text' | 'action' | 'system_result'

export interface NarrativeNode {
  id: string
  kind: NarrativeNodeKind
  segmentId: number
  status?: ToolSegment['status']
}

export interface RunProgressSummary {
  visible: boolean
  active: boolean
  stageLabel: string
  elapsedLabel: string
  todoCompleted: number
  todoTotal: number
  terminalLabel?: string
}

export type AgentStatus =
  | 'idle'
  | 'initializing'
  | 'planning'
  | 'reasoning'
  | 'tool_calling'
  | 'observing'
  | 'summarizing'
  | 'memory_sync'
  | 'awaiting_user'
  | 'completed'
  | 'cancelled'
  | 'handoff'
  | 'error'

export type AgentStateNodeKind = 'phase' | 'tool' | 'observation' | 'terminal'
export type ToolExecutionStatus = ToolSegment['status']

export interface AgentStateNode {
  id: string
  stepId?: string
  kind: AgentStateNodeKind
  status: AgentStatus
  label: string
  startedAt: number
  endedAt?: number
  durationMs?: number
  active: boolean
  summary?: string
  toolIds?: string[]
  messageId?: number
}

export interface ToolExecution {
  toolId: string
  toolName: string
  status: ToolExecutionStatus
  inputs: Record<string, unknown>
  outputs?: string | null
  progressMessage?: string | null
  startedAt: number
  endedAt?: number
  redacted?: boolean
  stepId?: string
  definitionSeq?: number
  resultSeq?: number
  attempt?: number
  attempts?: ToolAttempt[]
}

export interface ToolAttempt {
  attempt: number
  status: string
  started_at?: number
  finished_at?: number
  error?: { code: string; message?: string }
}

export interface AgentStateStream {
  turnKey: number
  turnId?: string | null
  status: AgentStatus
  currentStep: number
  maxSteps?: number
  startedAt: number
  endedAt?: number
  nodes: AgentStateNode[]
  toolExecutions: ToolExecution[]
  todoCompleted: number
  todoTotal: number
  terminalLabel?: string
  requiresUserAction: boolean
  confirmedRevision?: number | null
  transportSeq?: number | null
  ledgerSeq?: number | null
  currentTask?: string | null
  currentStepId?: string | null
  requestId?: string | null
  syncing?: boolean
  activeMessageId?: number | null
}

export interface ChatUiMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  segments?: MessageSegment[]
  turnId?: string
  messageId?: string
  ledgerSeq?: number
}

export interface ApprovalPreview {
  operation?: string
  target_summary?: string
  risk_reason?: string
  impact_summary?: string
  policy_summary?: string
  snapshot_status?: string | null
  snapshot_id?: string | null
  allowlist_matched?: boolean | null
  capabilities?: string[]
  cwd?: string | null
  normalized_command?: string | null
  timeout_seconds?: number | null
  extras?: Record<string, unknown>
}

export interface PendingApproval {
  turnId: string
  interruptId?: string
  tool: string
  args: Record<string, unknown>
  reason: string
  toolCallId?: string
  stepId?: string
  reasonCode?: string
  preview?: ApprovalPreview | null
}

export type AskUserMode = 'answer_and_continue' | 'handoff_and_stop'

export interface PendingAskUser {
  turnId: string
  prompt: string
  options: string[]
  allowMultiple: boolean
  interruptId?: string
  stepId?: string
  toolCallId?: string
  mode: AskUserMode
  terminalOnAck?: boolean
}

export interface SessionTodoItem {
  id: string
  content: string
  status: 'pending' | 'in_progress' | 'completed' | string
}
