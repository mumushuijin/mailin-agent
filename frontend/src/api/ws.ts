import { getWsApiBase } from './index'
import type { ChatResponse, StreamCallback, StreamEvent } from './chat'

const WS_URL = `${getWsApiBase()}/api/ws/chat`
const HEARTBEAT_MS = 30_000
const RECONNECT_BASE_MS = 1_000
const RECONNECT_MAX_MS = 30_000

type BackgroundHandler = (data: {
  kind: string
  message: string
  status?: 'started' | 'done' | 'failed' | 'skipped'
  success?: boolean
  task_id?: string
  detail?: Record<string, unknown>
}) => void
type ConfigUpdatedHandler = (data: { name: string }) => void
export type WsConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected'
type ConnectionHandler = (state: WsConnectionState) => void

function mapWsToStreamEvent(msg: { type: string; data?: Record<string, unknown>; run_id?: string }): StreamEvent | null {
  const data = msg.data || {}
  const base = { run_id: msg.run_id }

  switch (msg.type) {
    case 'session':
      return { type: 'session', session_id: data.session_id as string, ...base }
    case 'step_start':
      return { type: 'step_start', step: data.step as number, max_steps: data.max_steps as number, ...base }
    case 'chunk':
      return { type: 'chunk', content: data.content as string, ...base }
    case 'tool_start':
      return {
        type: 'tool_start',
        tool: data.tool as string,
        args: data.args as Record<string, unknown>,
        tool_call_id: data.tool_call_id as string,
        ...base,
      }
    case 'tool_finish':
      return {
        type: 'tool_finish',
        tool: data.tool as string,
        result: data.result as string,
        tool_call_id: data.tool_call_id as string,
        ...base,
      }
    case 'tool_progress':
      return {
        type: 'tool_progress',
        tool: data.tool as string,
        status: data.status as string,
        result: (data.message || data.result) as string,
        message: (data.message || data.chunk) as string,
        ...base,
      }
    case 'step_finish':
      return { type: 'step_finish', step: data.step as number, ...base }
    case 'stage':
      return {
        type: 'stage',
        stage: data.stage as string,
        status: data.status as string,
        elapsed_ms: data.elapsed_ms as number,
        duration_ms: data.duration_ms as number,
        error: data.error as string,
        ...base,
      }
    case 'context_usage':
      return { type: 'context_usage', context_usage: data as unknown as StreamEvent['context_usage'], ...base }
    case 'api_usage':
      return { type: 'api_usage', api_usage: data as unknown as StreamEvent['api_usage'], ...base }
    case 'session_token_stats':
      return { type: 'session_token_stats', session_token_stats: data as unknown as StreamEvent['session_token_stats'], ...base }
    case 'compression':
      return { type: 'compression', compression: data as StreamEvent['compression'], ...base }
    case 'done':
      return {
        type: 'done',
        content: data.content as string,
        session_id: data.session_id as string,
        partial: Boolean(data.partial),
        cancelled: Boolean(data.cancelled),
        ...base,
      }
    case 'error':
      return { type: 'error', error: data.error as string, cancelled: Boolean(data.cancelled), ...base }
    case 'interrupt':
      return {
        type: 'interrupt',
        kind: (data.kind as string) || 'approval',
        tool: data.tool as string,
        args: data.args as Record<string, unknown>,
        reason: data.reason as string,
        tool_call_id: data.tool_call_id as string,
        prompt: data.prompt as string | undefined,
        options: Array.isArray(data.options) ? (data.options as string[]) : [],
        allow_multiple: Boolean(data.allow_multiple),
        ...base,
      }
    case 'todo':
      return {
        type: 'todo',
        todos: Array.isArray(data.todos) ? (data.todos as StreamEvent['todos']) : [],
        ...base,
      }
    case 'background':
      return {
        type: 'background',
        background: {
          kind: String(data.kind ?? ''),
          message: String(data.message ?? ''),
          status: (data.status as 'started' | 'done' | 'failed' | 'skipped') ?? 'done',
          success: Boolean(data.success ?? true),
          task_id: data.task_id as string | undefined,
          detail: data.detail as Record<string, unknown> | undefined,
        },
        ...base,
      }
    case 'config_updated':
      return { type: 'config_updated', config_name: data.name as string, ...base }
    default:
      return null
  }
}

class ChatWebSocket {
  private ws: WebSocket | null = null
  private connectPromise: Promise<void> | null = null
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private reconnectAttempt = 0
  private intentionalClose = false
  private currentRunId: string | null = null
  private runResolvers = new Map<string, { resolve: (v: ChatResponse) => void; reject: (e: Error) => void }>()
  private runCallbacks = new Map<string, StreamCallback>()
  private backgroundHandlers = new Set<BackgroundHandler>()
  private configHandlers = new Set<ConfigUpdatedHandler>()
  private connectionHandlers = new Set<ConnectionHandler>()
  private state: WsConnectionState = 'idle'

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  onBackground(handler: BackgroundHandler): () => void {
    this.backgroundHandlers.add(handler)
    return () => this.backgroundHandlers.delete(handler)
  }

  onConfigUpdated(handler: ConfigUpdatedHandler): () => void {
    this.configHandlers.add(handler)
    return () => this.configHandlers.delete(handler)
  }

  onConnectionState(handler: ConnectionHandler): () => void {
    this.connectionHandlers.add(handler)
    handler(this.state)
    return () => this.connectionHandlers.delete(handler)
  }

  private setState(state: WsConnectionState) {
    if (this.state === state) return
    this.state = state
    for (const h of this.connectionHandlers) h(state)
  }

  async connect(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) return
    if (this.connectPromise) return this.connectPromise

    this.intentionalClose = false
    this.setState(this.reconnectAttempt > 0 ? 'reconnecting' : 'connecting')
    this.connectPromise = new Promise<void>((resolve, reject) => {
      try {
        this.ws = new WebSocket(WS_URL)
      } catch (e) {
        this.connectPromise = null
        this.setState('disconnected')
        reject(e)
        return
      }

      this.ws.onopen = () => {
        this.reconnectAttempt = 0
        this.startHeartbeat()
        this.connectPromise = null
        this.setState('connected')
        resolve()
      }

      this.ws.onerror = () => {
        if (this.connectPromise) {
          this.connectPromise = null
          this.setState('disconnected')
          reject(new Error('WebSocket 连接失败'))
        }
      }

      this.ws.onclose = () => {
        this.stopHeartbeat()
        this.connectPromise = null
        if (!this.intentionalClose) {
          this.setState('reconnecting')
          this.scheduleReconnect()
        } else {
          this.setState('disconnected')
        }
      }

      this.ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string)
          this.handleMessage(msg)
        } catch {
          // ignore
        }
      }
    })

    return this.connectPromise
  }

  private handleMessage(msg: { type: string; data?: Record<string, unknown>; run_id?: string }) {
    if (msg.type === 'pong') return

    const event = mapWsToStreamEvent(msg)
    if (!event) return

    if (event.type === 'background' && event.background) {
      for (const h of this.backgroundHandlers) {
        h({
          kind: event.background.kind,
          message: event.background.message,
          status: event.background.status,
          success: event.background.success,
          task_id: event.background.task_id,
          detail: event.background.detail,
        })
      }
    }
    if (event.type === 'config_updated' && event.config_name) {
      for (const h of this.configHandlers) h({ name: event.config_name })
    }

    const runId = msg.run_id
    if (runId) {
      const cb = this.runCallbacks.get(runId)
      if (cb) cb(event)

      if (event.type === 'done' || event.type === 'error') {
        const resolver = this.runResolvers.get(runId)
        if (resolver) {
          if (event.type === 'error' && !event.error?.includes('已取消')) {
            resolver.reject(new Error(event.error || '未知错误'))
          } else {
            resolver.resolve({
              content: event.content || '',
              session_id: event.session_id ?? null,
            })
          }
          this.runResolvers.delete(runId)
          this.runCallbacks.delete(runId)
          if (this.currentRunId === runId) this.currentRunId = null
        }
      }
    }
  }

  private startHeartbeat() {
    this.stopHeartbeat()
    this.heartbeatTimer = setInterval(() => {
      this.send({ op: 'ping' })
    }, HEARTBEAT_MS)
  }

  private stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }

  private scheduleReconnect() {
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.reconnectAttempt, RECONNECT_MAX_MS)
    this.reconnectAttempt++
    setTimeout(() => {
      if (!this.intentionalClose) {
        this.connect().catch(() => {})
      }
    }, delay)
  }

  subscribeSession(sessionId: string | null) {
    this.send({ op: 'session.subscribe', session_id: sessionId })
  }

  private send(payload: Record<string, unknown>) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload))
    }
  }

  async sendMessage(
    message: string,
    sessionId: string | null | undefined,
    onChunk: StreamCallback,
  ): Promise<ChatResponse> {
    await this.connect()

    const runId = crypto.randomUUID()
    this.currentRunId = runId

    return new Promise<ChatResponse>((resolve, reject) => {
      this.runResolvers.set(runId, { resolve, reject })
      this.runCallbacks.set(runId, onChunk)
      this.send({
        op: 'chat.send',
        run_id: runId,
        message,
        session_id: sessionId,
      })
    })
  }

  cancel() {
    if (this.currentRunId) {
      this.send({ op: 'chat.cancel', run_id: this.currentRunId })
    }
  }

  approve(runId: string, decision: 'allow' | 'deny') {
    this.send({ op: 'approve', run_id: runId, decision })
  }

  answerAskUser(runId: string, answer: string | string[]) {
    this.send({ op: 'approve', run_id: runId, kind: 'ask_user', answer })
  }

  disconnect() {
    this.intentionalClose = true
    this.stopHeartbeat()
    this.ws?.close()
    this.ws = null
    this.setState('disconnected')
  }
}

export const chatWs = new ChatWebSocket()
