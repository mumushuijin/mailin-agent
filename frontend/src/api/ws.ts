import { getWsApiBaseAsync } from './index'
import type { AgentCheckpointState, ChatResponse, ChatSubmissionOptions, StreamCallback, StreamEvent } from './chat'
import { readCanonicalAgentState, rejectLegacyTurnFields } from '@/utils/canonicalAgentState'
import { normalizeInteractionRejection, requireStreamSchemaVersion, terminalOutcome } from '@/utils/streamProtocol'

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

export function mapWsToStreamEvent(msg: {
  type: string
  data?: Record<string, unknown>
  turn_id?: string
  state?: AgentCheckpointState
  seq?: number
  event_id?: string
  event_seq?: number
  branch_id?: string
  transport_id?: string
  transport_seq?: number
  state_revision?: number
  stream_schema_version?: number
}): StreamEvent | null {
  const data = msg.data || {}
  requireStreamSchemaVersion(msg)
  rejectLegacyTurnFields(msg as Record<string, unknown>)
  rejectLegacyTurnFields(data)
  const state = readCanonicalAgentState(msg.state || data.state)
  const base = {
    turn_id: msg.turn_id || state?.scope.turn_id || undefined,
    state,
    event_id: msg.event_id,
    event_seq: (msg.event_seq ?? data.event_seq) as number | undefined,
    branch_id: (msg.branch_id ?? data.branch_id) as string | undefined,
    transport_id: (msg.transport_id ?? data.transport_id) as string | undefined,
    transport_seq: (msg.transport_seq ?? data.transport_seq) as number | undefined,
    state_revision: (msg.state_revision ?? data.state_revision) as number | undefined,
    seq: msg.seq,
    session_id: (data.session_id || state?.scope.session_id) as string | undefined,
    step_id: (data.step_id || state?.current_step.step_id) as string | undefined,
  }

  switch (msg.type) {
    case 'session':
      return { type: 'session', ...base }
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
        result_ref: data.result_ref as string | null | undefined,
        result_seq: data.result_seq as number | undefined,
        result_truncated: Boolean(data.result_truncated),
        status: data.status as string | undefined,
        reason_code: data.reason_code as string | undefined,
        execution: data.execution as StreamEvent['execution'],
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
        partial: Boolean(data.partial),
        cancelled: Boolean(data.cancelled),
        handoff: Boolean(data.handoff),
        terminal_reason: data.terminal_reason as string | undefined,
        ...base,
      }
    case 'error':
      return normalizeInteractionRejection({
        type: 'error',
        error: data.error as string,
        cancelled: Boolean(data.cancelled),
        fail_closed: Boolean(data.fail_closed),
        reason_code: data.reason_code as string | undefined,
        ...base,
      })
    case 'interrupt':
      return {
        type: 'interrupt',
        kind: (data.kind as string) || 'approval',
        tool: data.tool as string,
        args: data.args as Record<string, unknown>,
        reason: data.reason as string,
        reason_code: data.reason_code as string | undefined,
        tool_call_id: data.tool_call_id as string,
        interrupt_id: data.interrupt_id as string | undefined,
        mode: data.mode as string | undefined,
        terminal_on_ack: Boolean(data.terminal_on_ack),
        prompt: data.prompt as string | undefined,
        options: Array.isArray(data.options) ? (data.options as string[]) : [],
        allow_multiple: Boolean(data.allow_multiple),
        preview: (data.preview as Record<string, unknown> | undefined) || undefined,
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
    case 'state_sync':
      return { type: 'state_sync', ...base }
    case 'timeline_event':
      return { type: 'timeline_event', timeline_event: data.event as Record<string, unknown>, ...base }
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
  private currentTurnId: string | null = null
  private currentSessionId: string | null = null
  private currentBranchId = 'main'
  private sessionStreamHandlers = new Set<StreamCallback>()
  private turnResolvers = new Map<string, { resolve: (v: ChatResponse) => void; reject: (e: Error) => void }>()
  private turnCallbacks = new Map<string, StreamCallback>()
  /** 已进入 interrupt 等待的 run：抑制抢先到达的 done，避免注销 callback */
  private pendingInterruptTurns = new Set<string>()
  private deferredDoneTimers = new Map<string, ReturnType<typeof setTimeout>>()
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

  onSessionStream(handler: StreamCallback): () => void {
    this.sessionStreamHandlers.add(handler)
    return () => this.sessionStreamHandlers.delete(handler)
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
    this.connectPromise = (async () => {
      const wsUrl = `${await getWsApiBaseAsync()}/api/ws/chat`
      return new Promise<void>((resolve, reject) => {
        try {
          this.ws = new WebSocket(wsUrl)
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
        if (this.currentSessionId) this.subscribeSession(this.currentSessionId, this.currentBranchId)
        if (this.currentSessionId && this.currentTurnId) this.requestStateSync(this.currentTurnId, this.currentSessionId)
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
          } catch (error) {
            const event: StreamEvent = { type: 'error', error: error instanceof Error ? error.message : '不支持的传输协议', turn_id: this.currentTurnId || undefined }
            if (this.currentTurnId) this.deliverAndSettle(this.currentTurnId, event)
          }
        }
      })
    })()

    return this.connectPromise
  }

  private handleMessage(msg: { type: string; data?: Record<string, unknown>; turn_id?: string }) {
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

    const turnId = msg.turn_id
    if (!turnId) {
      if (event.type === 'timeline_event' && (!event.session_id || event.session_id === this.currentSessionId)) {
        for (const handler of this.sessionStreamHandlers) handler(event)
      }
      return
    }
    if (event.type === 'session' && event.session_id && !this.currentSessionId && turnId === this.currentTurnId) this.currentSessionId = event.session_id
    if (event.type === 'stage' && event.stage === 'interaction_timeout') this.releaseInterruptWait(turnId)
    if (event.type === 'stage' && event.stage === 'interaction_rejected') {
      this.pendingInterruptTurns.add(turnId)
      if (this.currentSessionId) this.requestStateSync(turnId, this.currentSessionId)
    }
    if (event.session_id && event.session_id !== this.currentSessionId) return
    if (event.branch_id && event.branch_id !== this.currentBranchId && event.type !== 'timeline_event') return
    if (!this.turnCallbacks.has(turnId)) {
      if (event.type === 'state_sync' || event.type === 'interrupt') this.currentTurnId = turnId
      for (const handler of this.sessionStreamHandlers) handler(event)
    }

    if (event.type === 'interrupt') {
      this.pendingInterruptTurns.add(turnId)
      this.clearDeferredDone(turnId)
      const cb = this.turnCallbacks.get(turnId)
      if (cb) cb(event)
      return
    }

    if (event.type === 'state_sync' && event.state?.turn_status !== 'waiting_user') {
      this.releaseInterruptWait(turnId)
      if (terminalOutcome(event)) {
        this.clearDeferredDone(turnId)
        this.deliverAndSettle(turnId, event)
        return
      }
    }

    if (event.type === 'done') {
      // LangGraph 1.x 可能在 interrupt 前误发 done；短暂延迟，若随后收到 interrupt 则丢弃该 done
      this.clearDeferredDone(turnId)
      const timer = setTimeout(() => {
        this.deferredDoneTimers.delete(turnId)
        if (this.pendingInterruptTurns.has(turnId) && !['completed', 'failed', 'cancelled', 'handoff'].includes(event.state?.turn_status || '')) return
        this.deliverAndSettle(turnId, event)
      }, 50)
      this.deferredDoneTimers.set(turnId, timer)
      return
    }

    const cb = this.turnCallbacks.get(turnId)
    if (cb) cb(event)

    if (event.type === 'error') {
      this.pendingInterruptTurns.delete(turnId)
      this.clearDeferredDone(turnId)
      this.settleTurn(turnId, event)
    }
  }

  private clearDeferredDone(turnId: string) {
    const timer = this.deferredDoneTimers.get(turnId)
    if (timer) {
      clearTimeout(timer)
      this.deferredDoneTimers.delete(turnId)
    }
  }

  private deliverAndSettle(turnId: string, event: StreamEvent) {
    const cb = this.turnCallbacks.get(turnId)
    if (cb) cb(event)
    this.settleTurn(turnId, event)
  }

  private settleTurn(turnId: string, event: StreamEvent) {
    const resolver = this.turnResolvers.get(turnId)
    if (!resolver) return
    if (terminalOutcome(event) === 'error' && !event.error?.includes('已取消')) {
      resolver.reject(Object.assign(new Error(event.error || '未知错误'), { executionStarted: true }))
    } else {
      resolver.resolve({
        content: event.content || '',
        session_id: event.session_id ?? null,
        turn_id: turnId,
      })
    }
    this.turnResolvers.delete(turnId)
    this.turnCallbacks.delete(turnId)
    this.pendingInterruptTurns.delete(turnId)
    if (this.currentTurnId === turnId) this.currentTurnId = null
  }

  private releaseInterruptWait(turnId: string) {
    this.pendingInterruptTurns.delete(turnId)
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

  subscribeSession(sessionId: string | null, branchId = 'main') {
    if (this.currentSessionId !== sessionId || this.currentBranchId !== branchId) this.currentTurnId = null
    this.currentSessionId = sessionId
    this.currentBranchId = branchId
    this.send({ op: 'session.subscribe', session_id: sessionId, branch_id: branchId })
  }

  requestStateSync(turnId: string, sessionId: string) {
    this.send({ op: 'state.sync', turn_id: turnId, session_id: sessionId, branch_id: this.currentBranchId })
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
    submittedTurnId?: string,
    submittedSubmissionId?: string,
    options?: ChatSubmissionOptions,
  ): Promise<ChatResponse> {
    await this.connect()

    const turnId = submittedTurnId || crypto.randomUUID()
    this.currentTurnId = turnId
    this.currentSessionId = sessionId || null
    this.currentBranchId = options?.branch_id ?? 'main'

    return new Promise<ChatResponse>((resolve, reject) => {
      this.turnResolvers.set(turnId, { resolve, reject })
      this.turnCallbacks.set(turnId, onChunk)
      this.send({
        op: 'chat.send',
        turn_id: turnId,
        submission_id: submittedSubmissionId || crypto.randomUUID(),
        branch_id: this.currentBranchId,
        fork_cursor: options?.fork_cursor,
        message,
        session_id: sessionId,
      })
    })
  }

  cancel() {
    if (this.currentTurnId) {
      this.releaseInterruptWait(this.currentTurnId)
      this.clearDeferredDone(this.currentTurnId)
      this.send({ op: 'chat.cancel', turn_id: this.currentTurnId, session_id: this.currentSessionId, branch_id: this.currentBranchId })
    }
  }

  approve(turnId: string, decision: 'allow' | 'deny', opts?: { interruptId?: string; toolCallId?: string; sessionId?: string; stepId?: string }) {
    this.releaseInterruptWait(turnId)
    this.send({
      op: 'approve',
      branch_id: this.currentBranchId,
      turn_id: turnId,
      decision,
      interrupt_id: opts?.interruptId,
      tool_call_id: opts?.toolCallId,
      session_id: opts?.sessionId,
      step_id: opts?.stepId,
    })
  }

  answerAskUser(
    turnId: string,
    answer: string | string[],
    opts?: { interruptId?: string; toolCallId?: string; mode?: string; sessionId?: string; stepId?: string },
  ) {
    this.releaseInterruptWait(turnId)
    this.send({
      op: 'approve',
      branch_id: this.currentBranchId,
      turn_id: turnId,
      kind: 'ask_user',
      answer,
      interrupt_id: opts?.interruptId,
      tool_call_id: opts?.toolCallId,
      mode: opts?.mode || 'answer_and_continue',
      session_id: opts?.sessionId,
      step_id: opts?.stepId,
    })
  }

  ackHandoff(
    turnId: string,
    opts?: { interruptId?: string; toolCallId?: string; sessionId?: string; stepId?: string },
  ) {
    this.releaseInterruptWait(turnId)
    this.send({
      op: 'approve',
      branch_id: this.currentBranchId,
      turn_id: turnId,
      kind: 'ask_user',
      mode: 'handoff_and_stop',
      interrupt_id: opts?.interruptId,
      tool_call_id: opts?.toolCallId,
      session_id: opts?.sessionId,
      step_id: opts?.stepId,
    })
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
