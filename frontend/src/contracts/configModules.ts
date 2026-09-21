/**
 * 配置契约消费入口。
 * 权威 JSON 由后端 `python -m app.config.schema_export` 生成后同步到本目录。
 */
import contract from './config-document.contract.json'

export type OrdinaryModuleKey =
  | 'agent'
  | 'tools'
  | 'middleware'
  | 'skills'
  | 'telemetry'
  | 'context'

/** 配置页仅展示的用户可编辑模块 */
export type UserVisibleModuleKey = 'agent' | 'tools'

export const ORDINARY_MODULE_KEYS = contract.ordinary_modules as OrdinaryModuleKey[]

export const USER_VISIBLE_MODULE_KEYS = (
  (contract as { user_visible_modules?: string[] }).user_visible_modules ?? ['agent', 'tools']
) as UserVisibleModuleKey[]

export const MODULE_DISPLAY_NAMES: Record<OrdinaryModuleKey, string> = Object.fromEntries(
  ORDINARY_MODULE_KEYS.map((key) => [key, contract.modules[key].display_name]),
) as Record<OrdinaryModuleKey, string>

export const MODULE_DEFAULTS: Record<OrdinaryModuleKey, Record<string, unknown>> =
  Object.fromEntries(
    ORDINARY_MODULE_KEYS.map((key) => [key, contract.modules[key].defaults]),
  ) as Record<OrdinaryModuleKey, Record<string, unknown>>

export const CONFIG_ENUMS = contract.enums as {
  enforcement_mode: Array<'audit' | 'enforce'>
  mcp_transport: Array<'stdio' | 'streamable-http' | 'sse'>
}

export const CANONICAL_CONFIG_EXAMPLE = contract.example

export function isUserVisibleModuleKey(key: string): key is UserVisibleModuleKey {
  return (USER_VISIBLE_MODULE_KEYS as readonly string[]).includes(key)
}

export { contract as configDocumentContract }
