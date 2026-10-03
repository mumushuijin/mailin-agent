import { expect, test } from 'vitest'

import {
  clearMarkdownCache,
  renderMarkdown,
  stabilizeStreamingMarkdown,
} from './markdown.ts'

test('fenced code with language exposes lang label structure', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['```ts', 'const x = 1', '```'].join('\n'))
  expect(html).toMatch(/class="markdown-code"/)
  expect(html).toMatch(/data-lang="ts"/)
  expect(html).toMatch(/markdown-code__lang">ts</)
  expect(html).toMatch(/const x = 1/)
})

test('untagged fence still renders as code block', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['```', 'plain', '```'].join('\n'))
  expect(html).toMatch(/class="markdown-code"/)
  expect(html).not.toMatch(/data-lang=/)
  expect(html).toMatch(/<pre><code>/)
  expect(html).toMatch(/plain/)
})

test('external links get target and rel', () => {
  clearMarkdownCache()
  const html = renderMarkdown('[docs](https://example.com/path)')
  expect(html).toMatch(/href="https:\/\/example\.com\/path"/)
  expect(html).toMatch(/target="_blank"/)
  expect(html).toMatch(/rel="noopener noreferrer"/)
})

test('javascript links are not navigable', () => {
  clearMarkdownCache()
  const html = renderMarkdown('[x](javascript:alert(1))')
  expect(html).not.toMatch(/href=["']javascript:/i)
  expect(html).toMatch(/>x</)
})

test('remote images are not auto-loaded by default', () => {
  clearMarkdownCache()
  const html = renderMarkdown('![shot](https://evil.example/a.png)')
  expect(html).not.toMatch(/<img\b/i)
  expect(html).not.toMatch(/src=["']https:\/\/evil\.example/)
  expect(html).toMatch(/markdown-image-placeholder/)
  expect(html).toMatch(/\[shot\]/)
})

test('task lists render disabled checkboxes', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['- [x] done', '- [ ] todo'].join('\n'))
  expect(html).toMatch(/type="checkbox"/)
  expect(html.includes('disabled=""')).toBe(true)
  expect(html.includes('checked=""')).toBe(true)
  expect(html).toMatch(/done/)
  expect(html).toMatch(/todo/)
})

test('tables are wrapped for horizontal scroll', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['| a | b |', '| - | - |', '| 1 | 2 |'].join('\n'))
  expect(html).toMatch(/markdown-table-wrap/)
  expect(html).toMatch(/<table>/)
})

test('script payloads are stripped', () => {
  clearMarkdownCache()
  const html = renderMarkdown('<script>alert(1)</script>\n\nhello')
  expect(html).not.toMatch(/<script/i)
  expect(html).not.toMatch(/alert\(1\)/)
  expect(html).toMatch(/hello/)
})

test('streaming incomplete fence is stabilized and not cached', () => {
  clearMarkdownCache()
  const partial = ['```ts', 'const x = 1'].join('\n')
  expect(stabilizeStreamingMarkdown(partial).endsWith('```')).toBe(true)

  const streamingHtml = renderMarkdown(partial, { streaming: true })
  expect(streamingHtml).toMatch(/markdown-code/)
  expect(streamingHtml).toMatch(/const x = 1/)

  // 流式结果不入缓存：同一原文在非流式下仍会重新解析并缓存终态
  const finalSrc = `${partial}\n\`\`\``
  const finalHtml = renderMarkdown(finalSrc)
  expect(finalHtml).toMatch(/markdown-code/)
  expect(renderMarkdown(finalSrc)).toBe(finalHtml)
})

test('stable historical text hits LRU cache', () => {
  clearMarkdownCache()
  const src = '**bold** and `code`'
  const first = renderMarkdown(src)
  const second = renderMarkdown(src)
  expect(first).toBe(second)
  expect(first).toMatch(/<strong>bold<\/strong>/)
})
