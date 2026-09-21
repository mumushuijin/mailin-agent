/** 配置前端共享类型（无 axios / 契约 JSON 依赖，便于 node:test） */

export type OrdinaryModuleKey =
  | 'agent'
  | 'tools'
  | 'middleware'
  | 'skills'
  | 'telemetry'
  | 'context'

export type ConfigErrorKind =
  | 'syntax'
  | 'type'
  | 'validation'
  | 'semantic'
  | 'conflict'
  | 'io'
  | 'draft'

export interface ConfigFieldError {
  path: string
  kind: string
  message: string
  expected?: unknown
}

export const MCP_SECRET_MASK = '********'

export type McpTransport = 'stdio' | 'streamable-http' | 'sse'

export interface McpConnection {
  type: McpTransport
  url?: string | null
  headers: Record<string, string>
  command?: string | null
  args: string[]
  env: Record<string, string>
}

export interface McpAuth {
  required_env: string[]
}

export interface McpTimeouts {
  connect: number
  call: number
}

export interface McpToolsFilter {
  include: string[]
  exclude: string[]
  resources: boolean
  prompts: boolean
}

export interface McpRuntime {
  supports_parallel_tool_calls: boolean
}

export interface McpServerJsonDraft {
  display_name?: string | null
  enabled: boolean
  connection: McpConnection
  auth: McpAuth
  timeouts: McpTimeouts
  tools: McpToolsFilter
  runtime: McpRuntime
  clear_headers?: string[]
  clear_env?: string[]
  [key: string]: unknown
}

export interface McpServerWrite {
  display_name?: string | null
  enabled: boolean
  connection: McpConnection
  auth?: McpAuth
  timeouts?: McpTimeouts
  tools?: McpToolsFilter
  runtime?: McpRuntime
  clear_headers?: string[]
  clear_env?: string[]
}
