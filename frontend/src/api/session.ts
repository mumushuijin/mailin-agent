import api from './index'
import type { ApiUsage, ContextUsage, SessionTokenStats } from './chat'

export interface Session {
  id: string
  created_at: number
  updated_at: number
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
}

export interface SessionHistory {
  session_id: string
  messages: ChatMessage[]
  context_usage?: ContextUsage | null
  api_usage?: ApiUsage | null
  session_token_stats?: SessionTokenStats | null
}

export const sessionApi = {
  list: async () => {
    return api.get<{ sessions: Session[] }>('/session/list')
  },
  create: async () => {
    return api.post<{ session_id: string }>('/session/create')
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
}
