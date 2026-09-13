import api from './index'

export type SkillSource = 'global' | 'project'
export type SkillHealth = 'ok' | 'disabled' | 'error' | 'shadowed'

export interface SkillEntry {
  id: string
  name: string
  description: string
  version?: string | null
  source: SkillSource
  path: string
  enabled: boolean
  active: boolean
  health: SkillHealth
  shadowed_by?: string | null
  errors: string[]
  summary: string
  tags: string[]
  triggers: string[]
}

export interface SkillResource {
  path: string
  content?: string | null
  error?: string | null
}

export interface SkillDetail extends SkillEntry {
  content?: string | null
  resources: SkillResource[]
}

export interface SkillMatch {
  skill: SkillEntry
  score: number
  reason: string
}

export interface SkillListResponse {
  skills: SkillEntry[]
  total: number
}

export interface SkillRefreshResponse extends SkillListResponse {
  status: string
}

export interface SkillToggleResponse {
  status: string
  skill: SkillEntry
}

export interface SkillMatchResponse {
  matches: SkillMatch[]
  total: number
}

function buildQuery(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      search.append(key, String(value))
    }
  })
  return search.toString() ? `?${search.toString()}` : ''
}

export const skillsApi = {
  list: async (source?: SkillSource | null, projectPath?: string | null) => {
    return api.get<SkillListResponse>(`/skills/list${buildQuery({ source, project_path: projectPath })}`)
  },
  refresh: async (projectPath?: string | null) => {
    return api.post<SkillRefreshResponse>(`/skills/refresh${buildQuery({ project_path: projectPath })}`)
  },
  get: async (
    id: string,
    source?: SkillSource | null,
    includeReferences = false,
    projectPath?: string | null,
  ) => {
    return api.get<SkillDetail>(`/skills/${encodeURIComponent(id)}${buildQuery({
      source,
      include_references: includeReferences,
      project_path: projectPath,
    })}`)
  },
  match: async (queryText: string, source?: SkillSource | null, limit = 5, projectPath?: string | null) => {
    return api.get<SkillMatchResponse>(`/skills/matches${buildQuery({
      query: queryText,
      source,
      limit,
      project_path: projectPath,
    })}`)
  },
  enable: async (id: string, source?: SkillSource | null, projectPath?: string | null) => {
    return api.post<SkillToggleResponse>(`/skills/${encodeURIComponent(id)}/enable${buildQuery({
      source,
      project_path: projectPath,
    })}`)
  },
  disable: async (id: string, source?: SkillSource | null, projectPath?: string | null) => {
    return api.post<SkillToggleResponse>(`/skills/${encodeURIComponent(id)}/disable${buildQuery({
      source,
      project_path: projectPath,
    })}`)
  },
}
