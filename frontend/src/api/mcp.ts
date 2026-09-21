import api from './index'
import type { ConfigFieldError } from './config'
import {
  MCP_SECRET_MASK,
  type McpAuth,
  type McpConnection,
  type McpRuntime,
  type McpServerJsonDraft,
  type McpServerWrite,
  type McpTimeouts,
  type McpToolsFilter,
  type McpTransport,
} from '@/config/types'

export type McpState =
  | 'disabled'
  | 'not_configured'
  | 'connecting'
  | 'ready'
  | 'degraded'
  | 'error'
  | 'stale'

export type {
  McpTransport,
  McpConnection,
  McpAuth,
  McpTimeouts,
  McpToolsFilter,
  McpRuntime,
  McpServerJsonDraft,
  McpServerWrite,
}
export { MCP_SECRET_MASK }

export interface McpServerListItem {
  id: string
  display_name: string
  enabled: boolean
  transport: McpTransport
  state: McpState
  tool_count: number
  last_checked_at?: number | null
  error_summary?: string | null
  can_connect: boolean
  validation_errors: string[]
  unresolved_variables: string[]
  source: 'mcp' | 'mcp_servers'
}

export interface McpServerDetail extends McpServerListItem {
  connection: McpConnection
  auth: McpAuth
  timeouts: McpTimeouts
  tools: McpToolsFilter
  runtime: McpRuntime
  warnings: string[]
  unknown_fields: Record<string, unknown>
  status?: {
    state: McpState
    connected: boolean
    tool_count: number
    tool_names: string[]
    error?: string | null
    error_code?: string | null
    captured_at?: number | null
    source?: string | null
    stale: boolean
    config_revision: number
    applied_revision: number
  } | null
  header_refs: Record<string, string>
  env_refs: Record<string, string>
}

export interface McpListResponse {
  servers: McpServerListItem[]
  total: number
  config_revision: number
  warnings: string[]
  legacy_shadowed: boolean
  source: 'mcp' | 'mcp_servers' | 'empty'
}

export interface McpValidateResponse {
  ok: boolean
  validation_errors: string[]
  unresolved_variables: string[]
  warnings: string[]
  can_connect: boolean
  normalized: Record<string, unknown>
  errors?: ConfigFieldError[]
}

export interface McpValidationErrorBody {
  detail?: string | { validation_errors?: string[]; message?: string }
  code?: string
  message?: string
  validation_errors?: string[]
  errors?: ConfigFieldError[]
}

export interface McpTestResponse {
  ok: boolean
  state: McpState
  duration_ms: number
  tool_count: number
  tool_names: string[]
  error?: string | null
  error_code?: string | null
  replaced_runtime: boolean
}

export interface McpRefreshResponse {
  results: Array<{
    id: string
    ok: boolean
    state: McpState
    tool_count: number
    error?: string | null
    error_code?: string | null
  }>
  config_revision: number
  applied_revision: number
}

export interface McpMutationResponse {
  status: string
  server: McpServerDetail
  config_revision: number
}

export const mcpApi = {
  list: () => api.get<McpListResponse>('/mcp/list'),
  get: (id: string) => api.get<McpServerDetail>(`/mcp/${encodeURIComponent(id)}`),
  upsert: (id: string, body: McpServerWrite) =>
    api.put<McpMutationResponse>(`/mcp/${encodeURIComponent(id)}`, body),
  remove: (id: string) => api.delete<{ status: string; id: string; config_revision: number }>(
    `/mcp/${encodeURIComponent(id)}`,
  ),
  enable: (id: string) => api.post<McpMutationResponse>(`/mcp/${encodeURIComponent(id)}/enable`),
  disable: (id: string) => api.post<McpMutationResponse>(`/mcp/${encodeURIComponent(id)}/disable`),
  validate: (body: McpServerWrite & { id?: string }) =>
    api.post<McpValidateResponse>('/mcp/validate', body),
  test: (body: McpServerWrite & { id?: string }) =>
    api.post<McpTestResponse>('/mcp/test', body),
  refresh: () => api.post<McpRefreshResponse>('/mcp/refresh'),
}
