import api, { getStreamApiBase } from './index'
import { chatWs } from './ws'

const API_BASE = getStreamApiBase()

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatResponse {
  content: string
  session_id: string | null
}

export interface ContextUsageBreakdown {
  bootstrap: number
  skills: number
  summary: number
  recent_turns: number
  tool_results: number
  current_message: number
  reserved: number
}

export interface ContextUsage {
  total_tokens: number
  max_tokens: number
  ratio: number
  breakdown: ContextUsageBreakdown
  compression_layer?: number | null
  warning?: string | null
  compressing?: boolean
  /** 本地粗估，仅在与 total_tokens 不同时出现 */
  estimated_tokens?: number
}

export interface ApiUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  prompt_cache_hit_tokens?: number
  prompt_cache_miss_tokens?: number
  reasoning_tokens?: number
  estimated_prompt_tokens?: number
  estimate_delta?: number
  sources?: string[]
  agent?: Record<string, number>
  compression?: Record<string, number>
}

export interface SessionTokenStats {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  prompt_cache_hit_tokens: number
  reasoning_tokens: number
  request_count: number
}

export interface StreamEvent {
  type:
    | 'session'
    | 'step_start'
    | 'chunk'
    | 'tool_start'
    | 'tool_finish'
    | 'step_finish'
    | 'done'
    | 'error'
    | 'context_usage'
    | 'api_usage'
    | 'session_token_stats'
    | 'compression'
    | 'interrupt'
    | 'background'
    | 'config_updated'
  content?: string
  tool?: string
  args?: Record<string, unknown>
  result?: string
  error?: string
  session_id?: string | null
  step?: number
  max_steps?: number
  run_id?: string
  reason?: string
  tool_call_id?: string
  background?: {
    kind: string
    message: string
    status?: 'started' | 'done' | 'failed' | 'skipped'
    success?: boolean
    task_id?: string
    detail?: Record<string, unknown>
  }
  config_name?: string
  context_usage?: ContextUsage
  api_usage?: ApiUsage
  session_token_stats?: SessionTokenStats
  compression?: { status: string; layer?: number; message?: string }
}

export type StreamCallback = (event: StreamEvent) => void

export const chatApi = {
  // 流式发送消息 (SSE)
  sendMessage: async (message: string, sessionId?: string) => {
    return api.post('/chat/send', { message, session_id: sessionId })
  },

  // 同步发送消息（支持取消，超时时间 5 分钟）
  sendMessageSync: async (
    message: string,
    sessionId?: string,
    signal?: AbortSignal
  ): Promise<ChatResponse> => {
    return api.post('/chat/send/sync', { message, session_id: sessionId }, {
      signal,
      timeout: 300000, // 5 分钟超时
    })
  },

  // 流式发送消息 (SSE) - 返回完整响应
  sendMessageStream: async (
    message: string,
    sessionId: string | null | undefined,
    onChunk: StreamCallback,
    signal?: AbortSignal
  ): Promise<ChatResponse> => {
    const response = await fetch(`${API_BASE}/api/chat/send/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ message, session_id: sessionId }),
      signal,
    })

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      throw new Error('No response body')
    }

    const decoder = new TextDecoder()
    let buffer = ''
    let fullContent = ''
    let finalSessionId = sessionId

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // 解析 SSE 事件
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        let currentEvent = ''
        for (const line of lines) {
          // 跳过 ping 事件和空行
          if (line.startsWith('ping') || line.trim() === '') {
            continue
          }

          if (line.startsWith('event:')) {
            currentEvent = line.substring(6).trim()
          } else if (line.startsWith('data:')) {
            const data = line.substring(5).trim()
            if (data && currentEvent) {
              try {
                const parsed = JSON.parse(data)

                if (currentEvent === 'session') {
                  finalSessionId = parsed.session_id
                  onChunk({ type: 'session', session_id: parsed.session_id })
                } else if (currentEvent === 'step_start') {
                  onChunk({ type: 'step_start', step: parsed.step, max_steps: parsed.max_steps })
                } else if (currentEvent === 'chunk') {
                  fullContent += parsed.content || ''
                  onChunk({ type: 'chunk', content: parsed.content })
                } else if (currentEvent === 'tool_start') {
                  onChunk({ type: 'tool_start', tool: parsed.tool, args: parsed.args })
                } else if (currentEvent === 'tool_finish') {
                  onChunk({ type: 'tool_finish', tool: parsed.tool, result: parsed.result })
                } else if (currentEvent === 'step_finish') {
                  onChunk({ type: 'step_finish', step: parsed.step })
                } else if (currentEvent === 'context_usage') {
                  onChunk({ type: 'context_usage', context_usage: parsed })
                } else if (currentEvent === 'api_usage') {
                  onChunk({ type: 'api_usage', api_usage: parsed })
                } else if (currentEvent === 'session_token_stats') {
                  onChunk({ type: 'session_token_stats', session_token_stats: parsed })
                } else if (currentEvent === 'compression') {
                  onChunk({ type: 'compression', compression: parsed })
                } else if (currentEvent === 'done') {
                  finalSessionId = parsed.session_id
                  onChunk({ type: 'done', content: parsed.content, session_id: parsed.session_id })
                } else if (currentEvent === 'error') {
                  onChunk({ type: 'error', error: parsed.error })
                }
              } catch {
                // 忽略解析错误
              }
              currentEvent = ''
            }
          }
        }
      }
    } finally {
      reader.releaseLock()
    }

    return { content: fullContent, session_id: finalSessionId ?? null }
  },

  // WebSocket 发送（优先），失败时降级 SSE
  sendMessageWs: async (
    message: string,
    sessionId: string | null | undefined,
    onChunk: StreamCallback,
    signal?: AbortSignal,
  ): Promise<ChatResponse> => {
    if (signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError')
    }

    const abortHandler = () => {
      chatWs.cancel()
    }
    signal?.addEventListener('abort', abortHandler)

    try {
      return await chatWs.sendMessage(message, sessionId, onChunk)
    } catch (wsError) {
      console.warn('WebSocket 发送失败，降级到 SSE:', wsError)
      return chatApi.sendMessageStream(message, sessionId, onChunk, signal)
    } finally {
      signal?.removeEventListener('abort', abortHandler)
    }
  },

  // 服务端取消（WebSocket）
  cancelGeneration: () => {
    chatWs.cancel()
  },

  // 工具审批
  approveTool: (runId: string, decision: 'allow' | 'deny') => {
    chatWs.approve(runId, decision)
  },
}
