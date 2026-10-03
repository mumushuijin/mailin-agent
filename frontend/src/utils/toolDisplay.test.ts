import { expect, test } from 'vitest'

import { formatToolArgs, formatToolResult, getToolActionDisplay, getToolConfig, toolResultPreview } from './toolDisplay.ts'

test('known file tools produce compact path-aware action labels', () => {
  const action = getToolActionDisplay(
    'read_file',
    { path: 'frontend/src/views/ChatView.vue' },
    'done',
  )

  expect(action.verb).toBe('已读取')
  expect(action.name).toBe('文件')
  expect(action.target).toBe('frontend/src/views/ChatView.vue')
  expect(action.targetKind).toBe('path')
})

test('command tools preserve the command as a scannable target', () => {
  const action = getToolActionDisplay(
    'run_shell',
    { command: 'npm run type-check' },
    'running',
  )

  expect(action.verb).toBe('正在运行')
  expect(action.target).toBe('npm run type-check')
  expect(action.targetKind).toBe('command')
})

test('unknown tools fall back without throwing when args are missing', () => {
  const config = getToolConfig('mcp_unknown_tool')
  const action = getToolActionDisplay('mcp_unknown_tool', undefined, 'error')

  expect(config.icon).toBe('🔌')
  expect(action.verb).toBe('已执行')
  expect(action.name).toBe('tool')
  expect(action.target).toBeUndefined()
})

test('result preview collapses multiline and oversized output', () => {
  expect(toolResultPreview('ok\nsecond line')).toBe('ok')
  expect(toolResultPreview('x'.repeat(130)).length).toBe(118)
})

test('tool detail formatting redacts sensitive values', () => {
  expect(formatToolArgs({ token: 'secret', path: 'src/App.vue' })).toMatch(/token: \[已脱敏\]/)
  expect(formatToolResult('authorization: Bearer-secret')).not.toMatch(/Bearer-secret/)
})
