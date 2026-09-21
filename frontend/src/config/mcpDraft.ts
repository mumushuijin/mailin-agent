import { clonePlainJson } from './clonePlain.ts'
import {
  MCP_SECRET_MASK,
  type McpConnection,
  type McpServerJsonDraft,
  type McpServerWrite,
  type McpTransport,
  type ConfigFieldError,
} from './types.ts'

export type { McpServerJsonDraft, McpServerWrite }
export { MCP_SECRET_MASK }

/** 最小详情投影，避免依赖 api/mcp */
export interface McpDetailLike {
  display_name: string
  enabled: boolean
  connection: McpConnection
  auth?: { required_env: string[] }
  timeouts?: { connect: number; call: number }
  tools?: {
    include: string[]
    exclude: string[]
    resources: boolean
    prompts: boolean
  }
  runtime?: { supports_parallel_tool_calls: boolean }
  unknown_fields?: Record<string, unknown>
}

const TRANSPORTS: McpTransport[] = ['stdio', 'streamable-http', 'sse']

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asStringRecord(value: unknown): Record<string, string> {
  if (!isPlainObject(value)) return {}
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(value)) {
    if (typeof v === 'string') out[k] = v
  }
  return out
}

export function detailToServerDraft(detail: McpDetailLike): McpServerJsonDraft {
  return {
    display_name: detail.display_name,
    enabled: detail.enabled,
    connection: {
      type: detail.connection.type,
      url: detail.connection.url ?? null,
      headers: { ...(detail.connection.headers || {}) },
      command: detail.connection.command ?? null,
      args: [...(detail.connection.args || [])],
      env: { ...(detail.connection.env || {}) },
    },
    auth: { required_env: [...(detail.auth?.required_env || [])] },
    timeouts: {
      connect: detail.timeouts?.connect ?? 60,
      call: detail.timeouts?.call ?? 25,
    },
    tools: {
      include: [...(detail.tools?.include || [])],
      exclude: [...(detail.tools?.exclude || [])],
      resources: Boolean(detail.tools?.resources),
      prompts: Boolean(detail.tools?.prompts),
    },
    runtime: {
      supports_parallel_tool_calls: Boolean(detail.runtime?.supports_parallel_tool_calls),
    },
    ...clonePlainJson(detail.unknown_fields || {}),
  }
}

export function emptyServerDraft(): McpServerJsonDraft {
  return {
    display_name: '',
    enabled: true,
    connection: {
      type: 'streamable-http',
      url: '',
      headers: {},
      command: null,
      args: [],
      env: {},
    },
    auth: { required_env: [] },
    timeouts: { connect: 60, call: 25 },
    tools: { include: [], exclude: [], resources: false, prompts: false },
    runtime: { supports_parallel_tool_calls: false },
  }
}

export function guardMcpServerDraft(value: unknown):
  | { ok: true; value: McpServerJsonDraft }
  | { ok: false; errors: ConfigFieldError[] } {
  const errors: ConfigFieldError[] = []
  if (!isPlainObject(value)) {
    return {
      ok: false,
      errors: [{ path: '', kind: 'type', message: 'Server 配置必须是 JSON 对象', expected: 'object' }],
    }
  }
  if ('enabled' in value && typeof value.enabled !== 'boolean') {
    errors.push({ path: '/enabled', kind: 'type', message: '必须是布尔值', expected: 'boolean' })
  }
  const connection = value.connection
  if (!isPlainObject(connection)) {
    errors.push({ path: '/connection', kind: 'type', message: '必须是对象', expected: 'object' })
  } else {
    const transport = connection.type
    if (typeof transport !== 'string' || !TRANSPORTS.includes(transport as McpTransport)) {
      errors.push({
        path: '/connection/type',
        kind: 'type',
        message: '传输类型无效',
        expected: TRANSPORTS,
      })
    } else if (transport === 'stdio') {
      if (!connection.command || typeof connection.command !== 'string') {
        errors.push({
          path: '/connection/command',
          kind: 'validation',
          message: 'stdio 需要 command',
          expected: 'string',
        })
      }
    } else if (!connection.url || typeof connection.url !== 'string') {
      errors.push({
        path: '/connection/url',
        kind: 'validation',
        message: `${transport} 需要 url`,
        expected: 'string',
      })
    }
  }
  if (errors.length) return { ok: false, errors }
  return { ok: true, value: value as McpServerJsonDraft }
}

/**
 * 将草稿转为写入 DTO：掩码原样保留；相对基线删除的密钥进入 clear_*。
 */
export function draftToServerWrite(
  draft: McpServerJsonDraft,
  baseline?: McpServerJsonDraft | null,
): McpServerWrite {
  const headers = asStringRecord(draft.connection?.headers)
  const env = asStringRecord(draft.connection?.env)
  const baseHeaders = asStringRecord(baseline?.connection?.headers)
  const baseEnv = asStringRecord(baseline?.connection?.env)

  const clear_headers = Object.keys(baseHeaders).filter((k) => !(k in headers))
  const clear_env = Object.keys(baseEnv).filter((k) => !(k in env))

  const connection: McpConnection = {
    type: draft.connection.type,
    url: draft.connection.type === 'stdio' ? null : draft.connection.url ?? null,
    command: draft.connection.type === 'stdio' ? draft.connection.command ?? null : null,
    args: draft.connection.type === 'stdio' ? [...(draft.connection.args || [])] : [],
    headers,
    env,
  }

  return {
    display_name: draft.display_name ?? null,
    enabled: Boolean(draft.enabled),
    connection,
    auth: { required_env: [...(draft.auth?.required_env || [])] },
    timeouts: {
      connect: Number(draft.timeouts?.connect ?? 60),
      call: Number(draft.timeouts?.call ?? 25),
    },
    tools: {
      include: [...(draft.tools?.include || [])],
      exclude: [...(draft.tools?.exclude || [])],
      resources: Boolean(draft.tools?.resources),
      prompts: Boolean(draft.tools?.prompts),
    },
    runtime: {
      supports_parallel_tool_calls: Boolean(draft.runtime?.supports_parallel_tool_calls),
    },
    clear_headers,
    clear_env,
  }
}

export function copyServerDraft(source: McpServerJsonDraft): McpServerJsonDraft {
  const cloned = clonePlainJson(source)
  const headerKeys = Object.keys(cloned.connection.headers || {})
  const envKeys = Object.keys(cloned.connection.env || {})
  cloned.connection.headers = Object.fromEntries(headerKeys.map((k) => [k, '']))
  cloned.connection.env = Object.fromEntries(envKeys.map((k) => [k, '']))
  return cloned
}

export function isMaskedSecret(value: string | undefined | null): boolean {
  return value === MCP_SECRET_MASK
}
