import api from './index'
import type { ApiUsage, ContextUsage, SessionTokenStats } from './chat'
import type { ToolAttempt } from '@/types/chat-ui'
import type { PersistentTimelineState, TimelineEventEnvelope } from '@/utils/eventTimelineReducer'

export interface Session {
  id: string
  created_at: number
  updated_at: number
  workspace_path?: string | null
  title: string
  title_source?: 'placeholder' | 'auto' | 'user'
}

// OpenAI 标准消息格式
export interface ToolCall {
  id: string
  name: string
  arguments: Record<string, unknown>
}

export interface ContentReference { type: 'reference'; path: string; media_type: string; bytes: number; sha256: string }
export type MessageContent = string | Array<Record<string, unknown>>
export interface TurnTimeline {
  turn_id: string
  status?: string
  reason_code?: string
  steps?: Array<{ step_id: string; step_name?: string; status: string; started_at?: number; finished_at?: number; task_id?: string }>
  tasks?: Array<Record<string, unknown>>
  tools?: Array<{ call_id: string; definition_seq: number; result_seq?: number; attempts?: ToolAttempt[]; status?: string; reason_code?: string }>
  interactions?: Array<Record<string, unknown>>
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'tool' | 'system'
  content?: MessageContent
  message_id?: string | null
  turn_id?: string
  seq?: number
  tool_calls?: ToolCall[]
  tool_call_id?: string
  timestamp?: number
  tool_result_ref?: string | null
  tool_result_preview?: string | null
  tool_result_truncated?: boolean
  tool_status?: string | null
  tool_reason_code?: string | null
  tool_result_reference?: ContentReference
  interaction?: Record<string, unknown>
}

export interface SessionHistory {
  session_id: string
  workspace_id?: string
  projection_version: 4
  branch_id?: string
  as_of_event_seq?: number
  timelines?: TurnTimeline[]
  messages: ChatMessage[]
  context_usage?: ContextUsage | null
  api_usage?: ApiUsage | null
  session_token_stats?: SessionTokenStats | null
  todos?: Array<{ id: string; content: string; status: string }> | null
}

export interface SessionHistoryPage extends SessionHistory {
  session: Session
  limit: number
  before?: string | null
  has_more: boolean
  next_cursor?: string | null
  projection_updated_at?: number | null
}

export interface ToolResultPayload {
  session_id: string
  tool_call_id: string
  content?: string | null
  available: boolean
  error?: string | null
}

export interface SessionTimelinePage {
  session_id: string
  branch_id: string
  as_of_event_seq: number
  projection?: PersistentTimelineState
  events: TimelineEventEnvelope[]
  snapshot_cursor: string
  next_cursor: string
  has_more: boolean
}

export interface SessionTimelineParams {
  branch_id?: string
  limit?: number
  after_cursor?: string | null
}

export const sessionApi = {
  list: async () => {
    return api.get<{ sessions: Session[] }>('/session/list')
  },
  create: async (workspacePath: string) => {
    return api.post<{ session_id: string }>('/session/create', {
      workspace_path: workspacePath,
    })
  },
  rebind: async (id: string, workspacePath: string) => {
    return api.post<Session>(`/session/${id}/workspace`, {
      workspace_path: workspacePath,
    })
  },
  rename: async (id: string, title: string) => {
    return api.patch<Session>(`/session/${id}/title`, { title })
  },
  get: async (id: string) => {
    return api.get<Session>(`/session/${id}`)
  },
  delete: async (id: string) => {
    return api.delete(`/session/${id}`)
  },
  getHistory: async (id: string) => {
    return validateHistory(await api.get<SessionHistory>(`/session/${id}/history`))
  },
  getHistoryPage: async (id: string, params?: { limit?: number; before?: string | null }) => {
    return validateHistory(await api.get<SessionHistoryPage>(`/session/${id}/history`, {
      params: {
        limit: params?.limit ?? 30,
        before: params?.before ?? undefined,
      },
    }))
  },
  getToolResult: async (id: string, toolCallId: string, seq?: number) => {
    return api.get<ToolResultPayload>(`/session/${id}/tool-result/${encodeURIComponent(toolCallId)}`, { params: { seq } })
  },
  getLedgerMessage: async (id: string, seq: number) => api.get<{
    session_id: string; workspace_id: string; message: {
      seq: number; turn_id?: string; message_id: string; timestamp: number
      message: { role: string; content?: MessageContent; tool_calls?: ToolCall[]; tool_call_id?: string }
      execution?: { status: string; attempt?: number; error?: { code: string; message?: string } }
    }
  }>(`/session/${id}/ledger-message/${seq}`),
  getTimeline: async (id: string, params?: SessionTimelineParams) => api.get<SessionTimelinePage>(`/session/${id}/timeline`, {
    params: { branch_id: params?.branch_id ?? 'main', limit: params?.limit ?? 100, after_cursor: params?.after_cursor ?? undefined },
  }),
  getTimelineEvents: async (id: string, params?: SessionTimelineParams) => api.get<SessionTimelinePage>(`/session/${id}/events`, {
    params: { branch_id: params?.branch_id ?? 'main', limit: params?.limit ?? 100, after_cursor: params?.after_cursor ?? undefined },
  }),
}

export function validateHistory<T extends SessionHistory>(value: T): T {
  if (value.projection_version !== 4 || !Array.isArray(value.messages) || value.messages.some(message =>
    ['scope', 'origin', 'vendor_message_id', 'run_id'].some(key => key in message) ||
    !['user', 'assistant', 'tool', 'system'].includes(message.role) ||
    (message.tool_calls || []).some(call => typeof call.id !== 'string' || typeof call.name !== 'string' || !call.arguments || typeof call.arguments !== 'object' || Array.isArray(call.arguments)))) {
    throw new Error('不支持的历史协议：需要 history_projection version=4；旧会话请手动清理后新建。')
  }
  return value
}
