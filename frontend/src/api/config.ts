import api from './index'
import type { OrdinaryModuleKey } from '@/contracts/configModules'
import type { ConfigFieldError as SharedConfigFieldError } from '@/config/types'

export type { OrdinaryModuleKey }

export interface ConfigFile {
  name: string
  content: string
}

export interface ResetOptions {
  reset_sessions?: boolean
  reset_memory?: boolean
  reset_global_config?: boolean
}

export interface AgentInfo {
  name: string
}

export type ConfigErrorKind =
  | 'syntax'
  | 'type'
  | 'validation'
  | 'semantic'
  | 'conflict'
  | 'io'
  | 'draft'

export type ConfigFieldError = SharedConfigFieldError

export interface ConfigModuleSummary {
  key: OrdinaryModuleKey | string
  display_name: string
  value: Record<string, unknown>
  source: string
  warnings: string[]
  errors: ConfigFieldError[]
  has_draft_error: boolean
}

export interface ConfigModulesResponse {
  revision: number
  schema_version: number
  modules: ConfigModuleSummary[]
  warnings: string[]
  mcp_revision: number
  mcp_source: string
}

export interface ConfigModuleValidateRequest {
  value?: Record<string, unknown> | null
  text?: string | null
}

export interface ConfigModuleValidateResponse {
  ok: boolean
  module: string
  errors: ConfigFieldError[]
  normalized?: Record<string, unknown> | null
  revision?: number | null
}

export interface ConfigModuleUpdateRequest {
  base_revision: number
  value?: Record<string, unknown> | null
  text?: string | null
}

export interface ConfigModuleUpdateResponse {
  status: string
  module: string
  revision: number
  value: Record<string, unknown>
  warnings: string[]
}

export interface ConfigValidationErrorBody {
  detail?: string
  code: 'CONFIG_VALIDATION_ERROR'
  message?: string
  errors: ConfigFieldError[]
  revision?: number | null
  module?: string | null
}

export interface ConfigConflictErrorBody {
  detail?: string
  code: 'CONFIG_REVISION_CONFLICT'
  message?: string
  revision: number
  module?: string | null
  current?: Record<string, unknown> | null
}

export type ConfigApiErrorBody = ConfigValidationErrorBody | ConfigConflictErrorBody

export const MARKDOWN_CONFIG_NAMES = ['SOUL', 'USER', 'MEMORY', 'HEARTBEAT'] as const
export type MarkdownConfigName = (typeof MARKDOWN_CONFIG_NAMES)[number]

export const configApi = {
  list: async () => {
    return api.get<{ configs: string[] }>('/config/list')
  },
  get: async (name: string) => {
    return api.get<ConfigFile>(`/config/${name}`)
  },
  update: async (name: string, content: string) => {
    return api.put<{ name: string; status: string }>(`/config/${name}`, { content })
  },
  listModules: async () => {
    return api.get<ConfigModulesResponse>('/config/modules')
  },
  validateModule: async (module: string, body: ConfigModuleValidateRequest) => {
    return api.post<ConfigModuleValidateResponse>(
      `/config/modules/${encodeURIComponent(module)}/validate`,
      body,
    )
  },
  updateModule: async (module: string, body: ConfigModuleUpdateRequest) => {
    return api.put<ConfigModuleUpdateResponse>(
      `/config/modules/${encodeURIComponent(module)}`,
      body,
    )
  },
  reset: async (options: ResetOptions = {}) => {
    const params = new URLSearchParams()
    if (options.reset_sessions) params.append('reset_sessions', 'true')
    if (options.reset_memory) params.append('reset_memory', 'true')
    if (options.reset_global_config) params.append('reset_global_config', 'true')
    const query = params.toString() ? `?${params.toString()}` : ''
    return api.post<{ status: string; message: string }>(`/config/reset${query}`)
  },
  getAgentInfo: async () => {
    return api.get<AgentInfo>('/config/agent/info')
  },
}
