import { expect, test } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))

test('settings router includes dedicated MCP route', () => {
  const source = readFileSync(join(here, 'index.ts'), 'utf8')
  expect(source).toMatch(/path:\s*'mcp'/)
  expect(source).toMatch(/name:\s*'settings-mcp'/)
  expect(source).toMatch(/McpView\.vue/)
  expect(source).toMatch(/path:\s*'\/mcp'/)
  expect(source).toMatch(/redirect:\s*'\/settings\/mcp'/)
})

test('settings router splits markdown 设定 and config 配置', () => {
  const source = readFileSync(join(here, 'index.ts'), 'utf8')
  expect(source).toMatch(/path:\s*'markdown'/)
  expect(source).toMatch(/name:\s*'settings-markdown'/)
  expect(source).toMatch(/SettingsMarkdownView\.vue/)
  expect(source).toMatch(/path:\s*'config'/)
  expect(source).toMatch(/name:\s*'settings-config'/)
  expect(source).toMatch(/ConfigView\.vue/)
  expect(source).toMatch(/redirect:\s*'\/settings\/markdown'/)
})

test('settings nav labels 设定 and 配置 without mixing markdown into modules', () => {
  const settings = readFileSync(join(here, '../views/SettingsView.vue'), 'utf8')
  expect(settings).toMatch(/>\s*设定\s*</)
  expect(settings).toMatch(/>\s*配置\s*</)
  expect(settings).toMatch(/to="\/settings\/markdown"/)
  expect(settings).toMatch(/to="\/settings\/config"/)

  const configView = readFileSync(join(here, '../views/ConfigView.vue'), 'utf8')
  expect(configView).toMatch(/listModules/)
  expect(configView).toMatch(/isUserVisibleModuleKey/)
  expect(configView).not.toMatch(/SOUL|USER\.md|HEARTBEAT|MARKDOWN_CONFIG_NAMES/)

  const markdownView = readFileSync(join(here, '../views/SettingsMarkdownView.vue'), 'utf8')
  expect(markdownView).toMatch(/MARKDOWN_CONFIG_NAMES/)
  expect(markdownView).not.toMatch(/listModules/)
})
