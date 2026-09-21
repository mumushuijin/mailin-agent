import type { ConfigFieldError, OrdinaryModuleKey } from './types.ts'

export interface AgentModuleDto {
  model: string
  temperature: number
  max_steps: number
  graph_version: string
  [key: string]: unknown
}

export interface ToolsModuleDto {
  enforcement_mode: 'audit' | 'enforce'
  filesystem: boolean | Record<string, unknown>
  shell: boolean | Record<string, unknown>
  memory: boolean | Record<string, unknown>
  session: boolean | Record<string, unknown>
  skills: boolean | Record<string, unknown>
  calculator: boolean | Record<string, unknown>
  datetime: boolean | Record<string, unknown>
  web_search: boolean | Record<string, unknown>
  mcp: boolean | Record<string, unknown>
  tool_search: boolean | Record<string, unknown>
  [key: string]: unknown
}

export interface MiddlewareModuleDto {
  [key: string]: unknown
}

export interface SkillsModuleDto {
  global_roots: string[]
  project_enabled: boolean
  catalog_max_items: number
  catalog_max_chars: number
  cache_ttl_seconds: number
  disabled: string[]
  [key: string]: unknown
}

export interface TelemetryModuleDto {
  langsmith_enabled: boolean
  sample_rate: number
  [key: string]: unknown
}

export interface ContextModuleDto {
  max_tokens: number
  compress_threshold_ratio: number
  tool_result_max_tokens: number
  tool_summary_max_chars: number
  recent_turn_pairs: number
  recent_tail_max_tokens: number
  summary_max_chars: number
  summary_chunk_max_chars: number
  max_compression_attempts: number
  bootstrap?: Record<string, unknown>
  budget?: Record<string, unknown>
  memory?: Record<string, unknown>
  [key: string]: unknown
}

export type OrdinaryModuleDto =
  | AgentModuleDto
  | ToolsModuleDto
  | MiddlewareModuleDto
  | SkillsModuleDto
  | TelemetryModuleDto
  | ContextModuleDto

export type ModuleGuardResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; errors: ConfigFieldError[] }

const MODULE_KEYS: OrdinaryModuleKey[] = [
  'agent',
  'tools',
  'middleware',
  'skills',
  'telemetry',
  'context',
]

const ENFORCEMENT_MODES = ['audit', 'enforce'] as const

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function err(path: string, kind: string, message: string, expected?: unknown): ConfigFieldError {
  return { path, kind, message, expected }
}

function expectNumber(value: unknown, path: string, errors: ConfigFieldError[], integer = false) {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    errors.push(err(path, 'type', '必须是数字', integer ? 'integer' : 'number'))
    return
  }
  if (integer && !Number.isInteger(value)) {
    errors.push(err(path, 'type', '必须是整数', 'integer'))
  }
}

function expectBoolean(value: unknown, path: string, errors: ConfigFieldError[]) {
  if (typeof value !== 'boolean') {
    errors.push(err(path, 'type', '必须是布尔值', 'boolean'))
  }
}

function expectString(value: unknown, path: string, errors: ConfigFieldError[]) {
  if (typeof value !== 'string') {
    errors.push(err(path, 'type', '必须是字符串', 'string'))
  }
}

function expectStringArray(value: unknown, path: string, errors: ConfigFieldError[]) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    errors.push(err(path, 'type', '必须是字符串数组', 'string[]'))
  }
}

function expectPackageToggle(value: unknown, path: string, errors: ConfigFieldError[]) {
  if (typeof value === 'boolean' || isPlainObject(value)) return
  errors.push(err(path, 'type', '必须是布尔值或对象', 'boolean | object'))
}

export function isOrdinaryModuleKey(value: unknown): value is OrdinaryModuleKey {
  return typeof value === 'string' && (MODULE_KEYS as string[]).includes(value)
}

function guardAgent(value: Record<string, unknown>): ModuleGuardResult {
  const errors: ConfigFieldError[] = []
  if ('model' in value) expectString(value.model, '/model', errors)
  if ('temperature' in value) {
    expectNumber(value.temperature, '/temperature', errors)
    if (typeof value.temperature === 'number' && (value.temperature < 0 || value.temperature > 2)) {
      errors.push(err('/temperature', 'validation', 'temperature 必须在 0~2 之间', '0..2'))
    }
  }
  if ('max_steps' in value) {
    expectNumber(value.max_steps, '/max_steps', errors, true)
    if (typeof value.max_steps === 'number' && value.max_steps < 1) {
      errors.push(err('/max_steps', 'validation', 'max_steps 必须 >= 1', '>=1'))
    }
  }
  if ('graph_version' in value) expectString(value.graph_version, '/graph_version', errors)
  return errors.length ? { ok: false, errors } : { ok: true, value }
}

function guardTools(value: Record<string, unknown>): ModuleGuardResult {
  const errors: ConfigFieldError[] = []
  if ('enforcement_mode' in value) {
    const mode = value.enforcement_mode
    if (typeof mode !== 'string' || !(ENFORCEMENT_MODES as readonly string[]).includes(mode)) {
      errors.push(
        err('/enforcement_mode', 'type', '必须是 audit 或 enforce', [...ENFORCEMENT_MODES]),
      )
    }
  }
  for (const key of [
    'filesystem',
    'shell',
    'memory',
    'session',
    'skills',
    'calculator',
    'datetime',
    'web_search',
    'mcp',
    'tool_search',
  ]) {
    if (key in value) expectPackageToggle(value[key], `/${key}`, errors)
  }
  return errors.length ? { ok: false, errors } : { ok: true, value }
}

function guardSkills(value: Record<string, unknown>): ModuleGuardResult {
  const errors: ConfigFieldError[] = []
  if ('global_roots' in value) expectStringArray(value.global_roots, '/global_roots', errors)
  if ('project_enabled' in value) expectBoolean(value.project_enabled, '/project_enabled', errors)
  if ('catalog_max_items' in value) expectNumber(value.catalog_max_items, '/catalog_max_items', errors, true)
  if ('catalog_max_chars' in value) expectNumber(value.catalog_max_chars, '/catalog_max_chars', errors, true)
  if ('cache_ttl_seconds' in value) expectNumber(value.cache_ttl_seconds, '/cache_ttl_seconds', errors, true)
  if ('disabled' in value) expectStringArray(value.disabled, '/disabled', errors)
  return errors.length ? { ok: false, errors } : { ok: true, value }
}

function guardTelemetry(value: Record<string, unknown>): ModuleGuardResult {
  const errors: ConfigFieldError[] = []
  if ('langsmith_enabled' in value) expectBoolean(value.langsmith_enabled, '/langsmith_enabled', errors)
  if ('sample_rate' in value) {
    expectNumber(value.sample_rate, '/sample_rate', errors)
    if (typeof value.sample_rate === 'number' && (value.sample_rate < 0 || value.sample_rate > 1)) {
      errors.push(err('/sample_rate', 'validation', 'sample_rate 必须在 0~1 之间', '0..1'))
    }
  }
  return errors.length ? { ok: false, errors } : { ok: true, value }
}

function guardContext(value: Record<string, unknown>): ModuleGuardResult {
  const errors: ConfigFieldError[] = []
  for (const key of [
    'max_tokens',
    'tool_result_max_tokens',
    'tool_summary_max_chars',
    'recent_turn_pairs',
    'recent_tail_max_tokens',
    'summary_max_chars',
    'summary_chunk_max_chars',
    'max_compression_attempts',
  ]) {
    if (key in value) expectNumber(value[key], `/${key}`, errors, true)
  }
  if ('compress_threshold_ratio' in value) {
    expectNumber(value.compress_threshold_ratio, '/compress_threshold_ratio', errors)
  }
  for (const key of ['bootstrap', 'budget', 'memory']) {
    if (key in value && value[key] != null && !isPlainObject(value[key])) {
      errors.push(err(`/${key}`, 'type', '必须是对象', 'object'))
    }
  }
  return errors.length ? { ok: false, errors } : { ok: true, value }
}

export function guardOrdinaryModule(
  module: OrdinaryModuleKey,
  value: unknown,
): ModuleGuardResult {
  if (!isPlainObject(value)) {
    return {
      ok: false,
      errors: [err('', 'type', `模块 ${module} 的值必须是 JSON 对象`, 'object')],
    }
  }
  switch (module) {
    case 'agent':
      return guardAgent(value)
    case 'tools':
      return guardTools(value)
    case 'middleware':
      return { ok: true, value }
    case 'skills':
      return guardSkills(value)
    case 'telemetry':
      return guardTelemetry(value)
    case 'context':
      return guardContext(value)
    default:
      return { ok: true, value }
  }
}

export function parseModuleUnknown(
  module: OrdinaryModuleKey,
  raw: unknown,
): ModuleGuardResult {
  return guardOrdinaryModule(module, raw)
}
