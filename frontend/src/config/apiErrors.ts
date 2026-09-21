import type {
  ConfigApiErrorBody,
  ConfigConflictErrorBody,
  ConfigFieldError,
  ConfigValidationErrorBody,
} from '@/api/config'
import { mapPathErrors } from './jsonDraft'

export function getAxiosErrorBody(error: unknown): Record<string, unknown> | null {
  if (!error || typeof error !== 'object') return null
  const response = (error as { response?: { data?: unknown } }).response
  if (!response || typeof response.data !== 'object' || response.data === null) return null
  return response.data as Record<string, unknown>
}

export function isConfigValidationError(body: unknown): body is ConfigValidationErrorBody {
  return (
    !!body &&
    typeof body === 'object' &&
    (body as ConfigValidationErrorBody).code === 'CONFIG_VALIDATION_ERROR' &&
    Array.isArray((body as ConfigValidationErrorBody).errors)
  )
}

export function isConfigConflictError(body: unknown): body is ConfigConflictErrorBody {
  return (
    !!body &&
    typeof body === 'object' &&
    (body as ConfigConflictErrorBody).code === 'CONFIG_REVISION_CONFLICT' &&
    typeof (body as ConfigConflictErrorBody).revision === 'number'
  )
}

export function parseConfigApiError(error: unknown): {
  kind: 'validation' | 'conflict' | 'unknown'
  message: string
  errors: ConfigFieldError[]
  revision?: number
  current?: Record<string, unknown> | null
  body?: ConfigApiErrorBody
} {
  const body = getAxiosErrorBody(error)
  if (isConfigValidationError(body)) {
    return {
      kind: 'validation',
      message: body.message || body.detail || '配置校验失败',
      errors: mapPathErrors(body.errors || []),
      revision: body.revision ?? undefined,
      body,
    }
  }
  if (isConfigConflictError(body)) {
    return {
      kind: 'conflict',
      message: body.message || body.detail || '配置已被其他请求更新，请重新加载后合并',
      errors: [],
      revision: body.revision,
      current: body.current ?? null,
      body,
    }
  }
  const detail = body?.detail
  const message =
    typeof detail === 'string'
      ? detail
      : error instanceof Error
        ? error.message
        : '请求失败'
  return { kind: 'unknown', message, errors: [] }
}
