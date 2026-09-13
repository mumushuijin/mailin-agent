import api from './index'
import type { ApiUsage, ContextUsage, SessionTokenStats } from './chat'

export interface Session {
  id: string
  created_at: number
  updated_at: number
  workspace_path?: string | null
  title: string
  title_source?: 'placeholder' | 'auto' | 'user'
}

// OpenAI 标准消息格式
export interface ToolCallFunction {
  name: string
  arguments: string  // JSON 字符串
}

export interface ToolCall {
  id: string
  type: 'function'
  function: ToolCallFunction
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'tool'
  content?: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
  timestamp?: number
  tool_result_ref?: string | null
  tool_result_preview?: string | null
  tool_result_truncated?: boolean
}

export interface SessionHistory {
  session_id: string
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
    return api.get<SessionHistory>(`/session/${id}/history`)
  },
  getHistoryPage: async (id: string, params?: { limit?: number; before?: string | null }) => {
    return api.get<SessionHistoryPage>(`/session/${id}/history`, {
      params: {
        limit: params?.limit ?? 30,
        before: params?.before ?? undefined,
      },
    })
  },
  getToolResult: async (id: string, toolCallId: string) => {
    return api.get<ToolResultPayload>(`/session/${id}/tool-result/${encodeURIComponent(toolCallId)}`)
  },
}
