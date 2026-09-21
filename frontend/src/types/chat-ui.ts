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
  result?: string
  toolResultRef?: string | null
  toolResultTruncated?: boolean
  reasonCode?: string | null
  fullResultLoading?: boolean
  status: 'running' | 'done' | 'error' | 'cancelled' | 'policy_denied'
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

export interface ChatUiMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  segments?: MessageSegment[]
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
  runId: string
  tool: string
  args: Record<string, unknown>
  reason: string
  toolCallId?: string
  reasonCode?: string
  preview?: ApprovalPreview | null
}

export type AskUserMode = 'answer_and_continue' | 'handoff_and_stop'

export interface PendingAskUser {
  runId: string
  prompt: string
  options: string[]
  allowMultiple: boolean
  interruptId?: string
  toolCallId?: string
  mode: AskUserMode
  terminalOnAck?: boolean
}

export interface SessionTodoItem {
  id: string
  content: string
  status: 'pending' | 'in_progress' | 'completed' | string
}
