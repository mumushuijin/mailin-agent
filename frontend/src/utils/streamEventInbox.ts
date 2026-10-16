import type { StreamEvent } from '@/api/chat'
import { isCanonicalAgentState, rejectLegacyTurnFields } from './canonicalAgentState'
import { normalizeInteractionRejection, terminalOutcome } from './streamProtocol'

export interface StreamInboxDrain {
  events: StreamEvent[]
  gapDetected: boolean
  waitingForGap: boolean
  syncing: boolean
}

interface QueuedEvent {
  event: StreamEvent
  seq?: number
  order: number
}

const TERMINAL_TYPES = new Set<StreamEvent['type']>(['done', 'error'])

const turnIdOf = (event: StreamEvent): string | null => (
  event.turn_id || event.state?.scope.turn_id || null
)

const seqOf = (event: StreamEvent): number | undefined => (
  typeof event.seq === 'number' ? event.seq : undefined
)

const eventIdOf = (event: StreamEvent): string | null => (
  event.event_id || null
)

const isTerminal = (event: StreamEvent): boolean => TERMINAL_TYPES.has(event.type) || (event.type === 'state_sync' && terminalOutcome(event) !== null)

export class StreamEventInbox {
  private readonly gapWaitMs: number
  private turnId: string | null = null
  private retiredTurnIds = new Set<string>()
  private lastAppliedSeq: number | null = null
  private pendingSequenced = new Map<number, QueuedEvent>()
  private pendingUnsequenced: QueuedEvent[] = []
  private seenEventIds = new Set<string>()
  private gapStartedAt: number | null = null
  private syncing = false
  private terminal = false
  private order = 0
  private lastRevision = -1
  private sessionId: string | null = null

  constructor(gapWaitMs = 80) {
    this.gapWaitMs = gapWaitMs
  }

  get currentTurnId(): string | null {
    return this.turnId
  }

  reset(): void {
    this.turnId = null
    this.retiredTurnIds.clear()
    this.lastAppliedSeq = null
    this.pendingSequenced.clear()
    this.pendingUnsequenced = []
    this.seenEventIds.clear()
    this.gapStartedAt = null
    this.syncing = false
    this.terminal = false
    this.order = 0
    this.lastRevision = -1
    this.sessionId = null
  }

  push(event: StreamEvent, now = Date.now()): StreamInboxDrain {
    event = normalizeInteractionRejection(event)
    rejectLegacyTurnFields(event as unknown as Record<string, unknown>)
    if (event.state && !isCanonicalAgentState(event.state)) throw new Error('不支持的 checkpoint 协议')
    if (event.turn_id && event.state && event.turn_id !== event.state.scope.turn_id) throw new Error('传输 turn 与 checkpoint 归属不一致')
    const sessionId = event.session_id || event.state?.scope.session_id || null
    if (this.sessionId && sessionId && this.sessionId !== sessionId) return this.emptyDrain()
    const incomingTurnId = turnIdOf(event)
    const isNewRunBoundary = event.type === 'session'

    if (this.turnId && incomingTurnId && this.turnId !== incomingTurnId) {
      if (!isNewRunBoundary || this.retiredTurnIds.has(incomingTurnId)) return this.emptyDrain()
      const priorTurnIds = new Set([...this.retiredTurnIds, this.turnId])
      this.reset()
      this.retiredTurnIds = priorTurnIds
    }

    if (this.terminal) return this.emptyDrain()

    if (incomingTurnId) this.turnId = incomingTurnId
    if (sessionId) this.sessionId = sessionId

    const revision = event.state?.state_revision
    if (typeof revision === 'number' && revision < this.lastRevision) return this.emptyDrain()

    if (event.type === 'state_sync' && typeof event.seq === 'number') {
      this.pendingSequenced.clear()
      this.pendingUnsequenced = []
      this.lastAppliedSeq = event.seq - 1
      this.syncing = false
      this.gapStartedAt = null
      this.seenEventIds.clear()
    }

    const eventId = eventIdOf(event)
    if (eventId && this.seenEventIds.has(eventId)) return this.emptyDrain()
    if (eventId) this.seenEventIds.add(eventId)

    const queued: QueuedEvent = {
      event,
      seq: seqOf(event),
      order: this.order++,
    }

    if (typeof queued.seq === 'number') {
      if (this.lastAppliedSeq !== null && queued.seq <= this.lastAppliedSeq) {
        return this.emptyDrain()
      }
      if (this.pendingSequenced.has(queued.seq)) return this.emptyDrain()
      this.pendingSequenced.set(queued.seq, queued)
    } else {
      this.pendingUnsequenced.push(queued)
    }

    const forceTerminal = isTerminal(event)
    return this.drain(now, forceTerminal)
  }

  flush(now = Date.now(), force = false): StreamInboxDrain {
    return this.drain(now, force)
  }

  private emptyDrain(): StreamInboxDrain {
    return {
      events: [],
      gapDetected: false,
      waitingForGap: false,
      syncing: this.syncing,
    }
  }

  private drain(now: number, force: boolean): StreamInboxDrain {
    const events: StreamEvent[] = []
    let gapDetected = false
    let waitingForGap = false

    while (this.pendingSequenced.size > 0) {
      const nextSeq = this.lastAppliedSeq === null ? this.minPendingSeq() : this.lastAppliedSeq + 1
      const next = this.pendingSequenced.get(nextSeq)

      if (next) {
        this.pendingSequenced.delete(nextSeq)
        this.lastAppliedSeq = nextSeq
        const revision = next.event.state?.state_revision
        if (typeof revision === 'number' && revision < this.lastRevision) continue
        const revisionGap = typeof revision === 'number' && this.lastRevision >= 0 && revision > this.lastRevision + 1 && next.event.type !== 'state_sync'
        if (revisionGap) { gapDetected = true; this.syncing = true }
        if (typeof revision === 'number') this.lastRevision = revision
        events.push(revisionGap ? { ...next.event, gap_detected: true } : next.event)
        if (this.syncing) this.syncing = false
        this.gapStartedAt = null
        if (isTerminal(next.event)) this.terminal = true
        continue
      }

      const firstPending = this.firstPending()
      if (!firstPending) break

      const hasTerminalPending = Array.from(this.pendingSequenced.values()).some((item) => isTerminal(item.event))
      const gapAge = this.gapStartedAt === null ? 0 : now - this.gapStartedAt
      if (!force && !hasTerminalPending && gapAge < this.gapWaitMs) {
        if (this.gapStartedAt === null) this.gapStartedAt = now
        waitingForGap = true
        break
      }

      this.pendingSequenced.delete(firstPending.seq!)
      this.lastAppliedSeq = firstPending.seq!
      events.push({ ...firstPending.event, gap_detected: true })
      gapDetected = true
      this.syncing = true
      this.gapStartedAt = null
      if (isTerminal(firstPending.event)) this.terminal = true
    }

    if (this.pendingSequenced.size === 0 && !waitingForGap && this.pendingUnsequenced.length > 0) {
      const unsequenced = [...this.pendingUnsequenced].sort((left, right) => left.order - right.order)
      this.pendingUnsequenced = []
      for (const item of unsequenced) {
        const revision = item.event.state?.state_revision
        if (typeof revision === 'number' && revision < this.lastRevision) continue
        if (typeof revision === 'number' && this.lastRevision >= 0 && revision > this.lastRevision + 1 && item.event.type !== 'state_sync') {
          item.event = { ...item.event, gap_detected: true }; gapDetected = true; this.syncing = true
        }
        if (typeof revision === 'number') this.lastRevision = revision
        if (this.terminal) continue
        events.push(item.event)
        if (isTerminal(item.event)) this.terminal = true
      }
    }

    return { events, gapDetected, waitingForGap, syncing: this.syncing }
  }

  private minPendingSeq(): number {
    return Math.min(...this.pendingSequenced.keys())
  }

  private firstPending(): QueuedEvent | null {
    const seq = this.minPendingSeq()
    return this.pendingSequenced.get(seq) || null
  }
}
