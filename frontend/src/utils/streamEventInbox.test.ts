import { describe, expect, it } from 'vitest'
import type { StreamEvent } from '@/api/chat'
import { StreamEventInbox } from '@/utils/streamEventInbox'

const event = (runId: string, seq: number, type: StreamEvent['type']): StreamEvent => ({
  type,
  run_id: runId,
  seq,
  event_id: `${runId}-${seq}`,
})

describe('StreamEventInbox', () => {
  it('keeps sequence gaps buffered until the missing event arrives', () => {
    const inbox = new StreamEventInbox(80)

    expect(inbox.push(event('run-1', 1, 'stage'), 0).events.map((item) => item.seq)).toEqual([1])
    const waiting = inbox.push(event('run-1', 3, 'chunk'), 10)
    expect(waiting.events).toHaveLength(0)
    expect(waiting.waitingForGap).toBe(true)

    const recovered = inbox.push(event('run-1', 2, 'step_start'), 20)
    expect(recovered.events.map((item) => item.seq)).toEqual([2, 3])
    expect(recovered.syncing).toBe(false)
  })

  it('marks a bounded gap without dropping the next available event', () => {
    const inbox = new StreamEventInbox(80)
    inbox.push(event('run-2', 1, 'stage'), 0)
    inbox.push(event('run-2', 3, 'chunk'), 10)

    const timedOut = inbox.flush(100)
    expect(timedOut.events.map((item) => item.seq)).toEqual([3])
    expect(timedOut.events[0]?.gap_detected).toBe(true)
    expect(timedOut.gapDetected).toBe(true)
    expect(timedOut.syncing).toBe(true)

    const resumed = inbox.push(event('run-2', 4, 'done'), 110)
    expect(resumed.events.map((item) => item.seq)).toEqual([4])
    expect(resumed.syncing).toBe(false)
  })

  it('uses an authoritative state snapshot to recover across a missing sequence range', () => {
    const inbox = new StreamEventInbox(80)
    inbox.push(event('run-sync', 1, 'stage'), 0)
    inbox.push(event('run-sync', 4, 'chunk'), 10)
    expect(inbox.flush(100).gapDetected).toBe(true)

    const snapshot = inbox.push(event('run-sync', 8, 'state_sync'), 110)
    expect(snapshot.events.map((item) => item.type)).toEqual(['state_sync'])
    expect(snapshot.syncing).toBe(false)
    expect(inbox.push(event('run-sync', 9, 'stage'), 120).events.map((item) => item.seq)).toEqual([9])
  })

  it('uses terminal events as a cleanup barrier across a gap', () => {
    const inbox = new StreamEventInbox(80)
    inbox.push(event('run-3', 1, 'stage'), 0)

    const terminal = inbox.push(event('run-3', 4, 'done'), 10)
    expect(terminal.events.map((item) => item.seq)).toEqual([4])
    expect(terminal.events[0]?.gap_detected).toBe(true)

    const late = inbox.push(event('run-3', 5, 'stage'), 20)
    expect(late.events).toHaveLength(0)
  })

  it('deduplicates event ids and rejects stale or cross-run events', () => {
    const inbox = new StreamEventInbox()
    const first = event('run-4', 1, 'stage')
    expect(inbox.push(first).events).toHaveLength(1)
    expect(inbox.push(first).events).toHaveLength(0)
    expect(inbox.push(event('run-4', 0, 'chunk')).events).toHaveLength(0)
    expect(inbox.push(event('run-5', 2, 'chunk')).events).toHaveLength(0)
    expect(inbox.push(event('run-5', 1, 'session')).events.map((item) => item.run_id)).toEqual(['run-5'])
    expect(inbox.push(event('run-4', 3, 'step_start')).events).toHaveLength(0)
    expect(inbox.push(event('run-4', 4, 'session')).events).toHaveLength(0)
  })
})
