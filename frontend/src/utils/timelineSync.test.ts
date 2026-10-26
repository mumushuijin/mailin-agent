import { expect, test, vi } from 'vitest'
import type { SessionTimelinePage } from '@/api/session'
import { createPersistentTimelineState, type TimelineBranchView, type TimelineEventEnvelope } from './eventTimelineReducer'
import {
  catchUpTimeline,
  readTimelineCache,
  restoreTimeline,
  writeTimelineCache,
  type TimelineCacheEntry,
} from './timelineSync'

const view: TimelineBranchView = { branch_id: 'main', visible_branches: [{ branch_id: 'main', through_event_seq: null }] }
const projection = createPersistentTimelineState('session-1', view)

const event = (eventSeq: number, eventType = 'memory.sync.recorded'): TimelineEventEnvelope => ({
  schema_version: 3,
  session_id: 'session-1',
  branch_id: 'main',
  event_seq: eventSeq,
  event_id: `event-${eventSeq}`,
  event_type: eventType,
  payload: {},
})

function page(overrides: Partial<SessionTimelinePage> = {}): SessionTimelinePage {
  return {
    session_id: 'session-1', branch_id: 'main', as_of_event_seq: 0,
    projection, events: [], snapshot_cursor: 'snapshot-0', next_cursor: 'next-0', has_more: false,
    ...overrides,
  }
}

function memoryStorage(): Storage {
  const entries = new Map<string, string>()
  return {
    get length() { return entries.size },
    clear: () => entries.clear(),
    getItem: (key) => entries.get(key) ?? null,
    key: (index) => [...entries.keys()][index] ?? null,
    removeItem: (key) => { entries.delete(key) },
    setItem: (key, value) => { entries.set(key, String(value)) },
  }
}

test('bootstraps from the complete snapshot projection and resumes at its opaque high-water cursor', async () => {
  const api = {
    getTimeline: vi.fn().mockResolvedValue(page()),
    getTimelineEvents: vi.fn(),
  }

  const restored = await restoreTimeline(api, 'session-1')

  expect(restored.source).toBe('snapshot')
  expect(restored.cursor).toBe('snapshot-0')
  expect(api.getTimelineEvents).not.toHaveBeenCalled()
  expect(restored.projection).toBeDefined()
})

test('catches up after the confirmed opaque cursor and advances across non-UI branch-visible events', async () => {
  const cached: TimelineCacheEntry = {
    version: 1,
    session_id: 'session-1',
    branch_id: 'main',
    cursor: 'cursor-1',
    projection,
  }
  const api = {
    getTimeline: vi.fn(),
    getTimelineEvents: vi.fn()
      .mockResolvedValueOnce(page({
        events: [event(3)], next_cursor: 'cursor-3', has_more: true, as_of_event_seq: 5,
      }))
      .mockResolvedValueOnce(page({
        events: [event(5)], next_cursor: 'cursor-5', has_more: false, as_of_event_seq: 5,
      })),
  }

  const restored = await restoreTimeline(api, 'session-1', 'main', cached)

  expect(api.getTimelineEvents).toHaveBeenNthCalledWith(1, 'session-1', {
    branch_id: 'main', limit: 100, after_cursor: 'cursor-1',
  })
  expect(restored.cursor).toBe('cursor-5')
  expect(restored.projection.cursor).toEqual({ branch_id: 'main', event_seq: 5, event_id: 'event-5' })
  expect(api.getTimeline).not.toHaveBeenCalled()
})

test('falls back to a fresh snapshot when a cached cursor is rejected', async () => {
  const cached: TimelineCacheEntry = { version: 1, session_id: 'session-1', branch_id: 'main', cursor: 'stale', projection }
  const api = {
    getTimeline: vi.fn().mockResolvedValue(page({ snapshot_cursor: 'fresh' })),
    getTimelineEvents: vi.fn().mockRejectedValue(new Error('invalid_timeline_cursor')),
  }

  const restored = await restoreTimeline(api, 'session-1', 'main', cached)

  expect(restored.source).toBe('snapshot')
  expect(restored.cursor).toBe('fresh')
  expect(api.getTimeline).toHaveBeenCalledOnce()
})

test('falls back to a new projection on catch-up cursor rejection', async () => {
  const cached: TimelineCacheEntry = { version: 1, session_id: 'session-1', branch_id: 'main', cursor: 'stale', projection }
  const api = {
    getTimeline: vi.fn().mockResolvedValue(page({ snapshot_cursor: 'fresh' })),
    getTimelineEvents: vi.fn().mockRejectedValue(new Error('invalid_timeline_cursor')),
  }
  const restored = await restoreTimeline(api, 'session-1', 'main', cached)
  expect(restored.source).toBe('snapshot')
  expect(api.getTimeline).toHaveBeenCalledOnce()
})

test('stores only the durable projection and tolerates inaccessible local storage', () => {
  const storage = memoryStorage()
  const entry: TimelineCacheEntry = { version: 1, session_id: 'session-1', branch_id: 'main', cursor: 'opaque', projection }
  writeTimelineCache(entry, storage)

  const restored = readTimelineCache('session-1', 'main', storage)
  expect(restored?.cursor).toBe('opaque')
  expect(restored?.projection.session_id).toBe('session-1')
  expect(JSON.parse(storage.getItem('mailin.timeline.v1:session-1:main') || '{}')).not.toHaveProperty('transient')
  expect(() => writeTimelineCache(entry, { ...storage, setItem: () => { throw new Error('denied') } })).not.toThrow()
})

test('checks transport sequence gaps independently from persistent event sequence', async () => {
  const { needsTimelineCatchUp } = await import('./timelineSync')
  expect(needsTimelineCatchUp({ transport_id: 'connection-1', transport_seq: 8 }, {
    transport_id: 'connection-1', transport_seq: 10,
  })).toBe(true)
  expect(needsTimelineCatchUp({ transport_id: 'connection-1', transport_seq: 8 }, {
    transport_id: 'connection-1', transport_seq: 9,
  })).toBe(false)
  // A caller never infers a gap from event_seq; only the transport counter triggers this path.
})

test('rejects a page cursor that does not advance while more events remain', async () => {
  const api = {
    getTimeline: vi.fn(),
    getTimelineEvents: vi.fn().mockResolvedValue(page({ next_cursor: 'cursor-1', has_more: true })),
  }
  await expect(catchUpTimeline(api, 'session-1', 'main', projection, 'cursor-1')).rejects.toThrow('游标未前进')
})
