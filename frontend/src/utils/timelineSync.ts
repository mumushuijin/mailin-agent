import type { SessionTimelinePage } from '@/api/session'
import {
  hydratePersistentTimelineProjection,
  reducePersistentTimeline,
  type PersistentTimelineState,
} from './eventTimelineReducer'

const CACHE_VERSION = 1
const PAGE_LIMIT = 100

export interface TimelineSyncApi {
  getTimeline: (sessionId: string, params: { branch_id: string; limit: number }) => Promise<SessionTimelinePage>
  getTimelineEvents: (sessionId: string, params: { branch_id: string; limit: number; after_cursor: string }) => Promise<SessionTimelinePage>
}

export interface TimelineCacheEntry {
  version: 1
  session_id: string
  branch_id: string
  cursor: string
  projection: PersistentTimelineState
}

export interface TimelineSyncResult {
  projection: PersistentTimelineState
  cursor: string
  source: 'cache' | 'snapshot'
}

function cacheKey(sessionId: string, branchId: string): string {
  return `mailin.timeline.v${CACHE_VERSION}:${sessionId}:${branchId}`
}

export function readTimelineCache(sessionId: string, branchId: string, storage?: Storage): TimelineCacheEntry | null {
  try {
    const source = storage ?? globalThis.localStorage
    const raw = source.getItem(cacheKey(sessionId, branchId))
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<TimelineCacheEntry>
    if (value.version !== CACHE_VERSION || value.session_id !== sessionId || value.branch_id !== branchId ||
      typeof value.cursor !== 'string' || !value.cursor || !value.projection) return null
    return {
      version: CACHE_VERSION,
      session_id: sessionId,
      branch_id: branchId,
      cursor: value.cursor,
      projection: hydratePersistentTimelineProjection(value.projection, sessionId, branchId),
    }
  } catch {
    return null
  }
}

export function writeTimelineCache(entry: TimelineCacheEntry, storage?: Storage): void {
  try {
    const target = storage ?? globalThis.localStorage
    target.setItem(cacheKey(entry.session_id, entry.branch_id), JSON.stringify(entry))
  } catch {
    // Storage is an optimization; the server ledger remains authoritative.
  }
}

export function needsTimelineCatchUp(
  previous: { transport_id?: string; transport_seq?: number } | null,
  next: { transport_id?: string; transport_seq?: number },
): boolean {
  if (!previous || typeof previous.transport_seq !== 'number' || typeof next.transport_seq !== 'number') return false
  if (!next.transport_id || previous.transport_id !== next.transport_id) return true
  return next.transport_seq > previous.transport_seq + 1
}

export async function catchUpTimeline(
  api: TimelineSyncApi,
  sessionId: string,
  branchId: string,
  initialProjection: PersistentTimelineState,
  initialCursor: string,
): Promise<TimelineSyncResult> {
  let projection = initialProjection
  let cursor = initialCursor
  for (;;) {
    const page = await api.getTimelineEvents(sessionId, { branch_id: branchId, limit: PAGE_LIMIT, after_cursor: cursor })
    if (page.session_id !== sessionId || page.branch_id !== branchId || !Array.isArray(page.events) || typeof page.next_cursor !== 'string') {
      throw new Error('时间轴补拉响应归属或游标无效')
    }
    for (const event of page.events) projection = reducePersistentTimeline(projection, event)
    if (page.next_cursor === cursor && page.has_more) throw new Error('时间轴补拉游标未前进')
    cursor = page.next_cursor
    if (!page.has_more) break
  }
  return { projection, cursor, source: 'cache' }
}

export async function restoreTimeline(
  api: TimelineSyncApi,
  sessionId: string,
  branchId = 'main',
  cached = readTimelineCache(sessionId, branchId),
): Promise<TimelineSyncResult> {
  if (cached) {
    try {
      return await catchUpTimeline(api, sessionId, branchId, cached.projection, cached.cursor)
    } catch {
      // A stale or invalid opaque cursor is recovered from a fresh server snapshot.
    }
  }
  const page = await api.getTimeline(sessionId, { branch_id: branchId, limit: PAGE_LIMIT })
  if (page.session_id !== sessionId || page.branch_id !== branchId || !page.projection || typeof page.snapshot_cursor !== 'string') {
    throw new Error('时间轴快照响应不完整')
  }
  const projection = hydratePersistentTimelineProjection(page.projection, sessionId, branchId)
  return { projection, cursor: page.snapshot_cursor, source: 'snapshot' }
}

export async function syncTimelineFromServer(
  api: TimelineSyncApi,
  sessionId: string,
  branchId: string,
  cursor: string,
  projection: PersistentTimelineState,
): Promise<TimelineSyncResult> {
  return restoreTimeline(api, sessionId, branchId, { version: 1, session_id: sessionId, branch_id: branchId, cursor, projection })
}
