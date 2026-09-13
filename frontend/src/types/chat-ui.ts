export interface TextSegment {
  type: 'text'
  id: number
  content: string
}

export interface ToolSegment {
  type: 'tool'
  id: number
  tool: string
  args: Record<string, unknown>
  result?: string
  status: 'running' | 'done' | 'error'
}

export type MessageSegment = TextSegment | ToolSegment

export interface ChatUiMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  segments?: MessageSegment[]
}

export interface PendingApproval {
  runId: string
  tool: string
  args: Record<string, unknown>
  reason: string
}

export interface PendingAskUser {
  runId: string
  prompt: string
  options: string[]
  allowMultiple: boolean
}

export interface SessionTodoItem {
  id: string
  content: string
  status: 'pending' | 'in_progress' | 'completed' | string
}
