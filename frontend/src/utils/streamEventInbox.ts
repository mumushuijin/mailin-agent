import type { StreamEvent } from '@/api/chat'

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

const runIdOf = (event: StreamEvent): string | null => (
  event.run_id || event.state?.scope.run_id || null
)

const seqOf = (event: StreamEvent): number | undefined => (
  typeof event.seq === 'number' ? event.seq : undefined
)

const eventIdOf = (event: StreamEvent): string | null => (
  event.event_id || null
)

const isTerminal = (event: StreamEvent): boolean => TERMINAL_TYPES.has(event.type)

export class StreamEventInbox {
  private readonly gapWaitMs: number
  private runId: string | null = null
  private retiredRunIds = new Set<string>()
  private lastAppliedSeq: number | null = null
  private pendingSequenced = new Map<number, QueuedEvent>()
  private pendingUnsequenced: QueuedEvent[] = []
  private seenEventIds = new Set<string>()
  private gapStartedAt: number | null = null
  private syncing = false
  private terminal = false
  private order = 0

  constructor(gapWaitMs = 80) {
    this.gapWaitMs = gapWaitMs
  }

  get currentRunId(): string | null {
    return this.runId
  }

  reset(): void {
    this.runId = null
    this.retiredRunIds.clear()
    this.lastAppliedSeq = null
    this.pendingSequenced.clear()
    this.pendingUnsequenced = []
    this.seenEventIds.clear()
    this.gapStartedAt = null
    this.syncing = false
    this.terminal = false
    this.order = 0
  }

  push(event: StreamEvent, now = Date.now()): StreamInboxDrain {
    const incomingRunId = runIdOf(event)
    const isNewRunBoundary = event.type === 'session'

    if (this.runId && incomingRunId && this.runId !== incomingRunId) {
      if (!isNewRunBoundary || this.retiredRunIds.has(incomingRunId)) return this.emptyDrain()
      const priorRunIds = new Set([...this.retiredRunIds, this.runId])
      this.reset()
      this.retiredRunIds = priorRunIds
    }

    if (this.terminal && !isTerminal(event)) return this.emptyDrain()

    if (incomingRunId) this.runId = incomingRunId

    if (event.type === 'state_sync' && typeof event.seq === 'number') {
      this.pendingSequenced.clear()
      this.pendingUnsequenced = []
      this.lastAppliedSeq = event.seq - 1
      this.syncing = false
      this.gapStartedAt = null
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
        events.push(next.event)
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
