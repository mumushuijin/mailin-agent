import type { ConfigFieldError, OrdinaryModuleKey } from './types.ts'
import { clonePlainJson } from './clonePlain.ts'
import { guardOrdinaryModule } from './moduleGuards.ts'

export interface JsonDraftState {
  text: string
  lastValidText: string
  lastValidValue: Record<string, unknown> | null
  dirty: boolean
  syntaxError: ConfigFieldError | null
  typeErrors: ConfigFieldError[]
}

export function formatJson(value: unknown, space = 2): string {
  return `${JSON.stringify(value, null, space)}\n`
}

export function createJsonDraft(value: Record<string, unknown> | null | undefined): JsonDraftState {
  const safe = value && typeof value === 'object' ? value : {}
  const text = formatJson(safe)
  return {
    text,
    lastValidText: text,
    lastValidValue: clonePlainJson(safe),
    dirty: false,
    syntaxError: null,
    typeErrors: [],
  }
}

export function parseJsonText(text: string):
  | { ok: true; value: unknown }
  | { ok: false; error: ConfigFieldError } {
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch (e) {
    const message = e instanceof Error ? e.message : 'JSON 语法错误'
    return {
      ok: false,
      error: {
        path: '',
        kind: 'syntax',
        message,
        expected: 'json',
      },
    }
  }
}

/** 将后端/前端错误 path（如 /tools/shell/max_timeout）映射为可读提示 */
export function mapPathErrors(errors: ConfigFieldError[]): ConfigFieldError[] {
  return errors.map((item) => {
    const path = normalizeJsonPath(item.path)
    return {
      ...item,
      path,
      message: path ? `${path}: ${item.message}` : item.message,
    }
  })
}

export function normalizeJsonPath(path: string | undefined | null): string {
  if (!path) return ''
  const trimmed = path.trim()
  if (!trimmed) return ''
  if (trimmed.startsWith('/')) return trimmed
  if (trimmed.startsWith('$.')) return `/${trimmed.slice(2).replace(/\./g, '/')}`
  if (trimmed.includes('.')) return `/${trimmed.replace(/\./g, '/')}`
  return trimmed.startsWith('/') ? trimmed : `/${trimmed}`
}

export function updateDraftText(
  draft: JsonDraftState,
  text: string,
  module?: OrdinaryModuleKey,
): JsonDraftState {
  const next: JsonDraftState = {
    ...draft,
    text,
    dirty: text !== draft.lastValidText,
    syntaxError: null,
    typeErrors: [],
  }
  const parsed = parseJsonText(text)
  if (!parsed.ok) {
    next.syntaxError = parsed.error
    return next
  }
  if (module) {
    const guarded = guardOrdinaryModule(module, parsed.value)
    if (!guarded.ok) {
      next.typeErrors = mapPathErrors(guarded.errors)
      return next
    }
    next.lastValidValue = clonePlainJson(guarded.value)
  } else if (parsed.value && typeof parsed.value === 'object' && !Array.isArray(parsed.value)) {
    next.lastValidValue = clonePlainJson(parsed.value as Record<string, unknown>)
  }
  return next
}

export function markDraftSaved(draft: JsonDraftState, value: Record<string, unknown>): JsonDraftState {
  const text = formatJson(value)
  return {
    text,
    lastValidText: text,
    lastValidValue: clonePlainJson(value),
    dirty: false,
    syntaxError: null,
    typeErrors: [],
  }
}

export function restoreLastValid(draft: JsonDraftState): JsonDraftState {
  if (!draft.lastValidValue) return draft
  const text = formatJson(draft.lastValidValue)
  return {
    ...draft,
    text,
    dirty: text !== draft.lastValidText,
    syntaxError: null,
    typeErrors: [],
  }
}

export function reloadDraftFromServer(
  draft: JsonDraftState,
  value: Record<string, unknown>,
  keepLocalDraft: boolean,
): JsonDraftState {
  if (keepLocalDraft && draft.dirty) {
    return {
      ...draft,
      lastValidText: formatJson(value),
      lastValidValue: clonePlainJson(value),
    }
  }
  return createJsonDraft(value)
}

export function draftClientErrors(draft: JsonDraftState): ConfigFieldError[] {
  if (draft.syntaxError) return [draft.syntaxError]
  return draft.typeErrors
}

export function canSubmitDraft(draft: JsonDraftState): boolean {
  return !draft.syntaxError && draft.typeErrors.length === 0
}
