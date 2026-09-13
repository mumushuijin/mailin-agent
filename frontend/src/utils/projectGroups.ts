export const UNBOUND_GROUP_KEY = '__unbound__'

export interface GroupableSession {
  id: string
  title: string
  created_at: number
  updated_at: number
  workspace_path?: string | null
}

export interface ProjectGroup {
  key: string
  label: string
  path: string | null
  sessions: GroupableSession[]
  updatedAt: number
}

export function normalizeProjectKey(path: string | null | undefined): string | null {
  const trimmed = (path || '').trim().replace(/[/\\]+$/, '')
  if (!trimmed) return null
  return trimmed.replace(/\\/g, '/').toLowerCase()
}

export function projectLabel(path: string): string {
  const cleaned = path.replace(/[/\\]+$/, '')
  const parts = cleaned.split(/[/\\]/)
  return parts[parts.length - 1] || path
}

export function groupSessionsByProject(sessions: GroupableSession[]): ProjectGroup[] {
  const map = new Map<string, ProjectGroup>()

  for (const session of sessions) {
    const key = normalizeProjectKey(session.workspace_path) ?? UNBOUND_GROUP_KEY
    let group = map.get(key)
    if (!group) {
      group = {
        key,
        label: key === UNBOUND_GROUP_KEY ? '未绑定' : projectLabel(session.workspace_path || ''),
        path: session.workspace_path ?? null,
        sessions: [],
        updatedAt: 0,
      }
      map.set(key, group)
    }
    group.sessions.push(session)
    group.updatedAt = Math.max(group.updatedAt, session.updated_at || 0)
  }

  const groups = [...map.values()]
  groups.sort((a, b) => {
    if (a.key === UNBOUND_GROUP_KEY) return 1
    if (b.key === UNBOUND_GROUP_KEY) return -1
    return b.updatedAt - a.updatedAt
  })
  for (const group of groups) {
    group.sessions.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
  }
  return groups
}
