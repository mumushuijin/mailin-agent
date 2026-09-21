import { marked, Renderer } from 'marked'
import createDOMPurify from 'dompurify'

marked.setOptions({
  breaks: true,
  gfm: true,
})

type PurifyInstance = {
  sanitize: (dirty: string | Node, cfg?: Record<string, unknown>) => string
}

let purifyInstance: PurifyInstance | null = null

function getPurify(): PurifyInstance | null {
  if (purifyInstance) return purifyInstance

  const maybeInstance = createDOMPurify as unknown as PurifyInstance | ((window: Window) => PurifyInstance)
  if (
    typeof maybeInstance === 'function'
    && typeof (maybeInstance as unknown as PurifyInstance).sanitize === 'function'
  ) {
    purifyInstance = maybeInstance as unknown as PurifyInstance
    return purifyInstance
  }
  if (typeof maybeInstance === 'object' && maybeInstance && typeof maybeInstance.sanitize === 'function') {
    purifyInstance = maybeInstance
    return purifyInstance
  }

  const win = (globalThis as { window?: Window }).window
  if (!win?.document || typeof maybeInstance !== 'function') {
    return null
  }
  purifyInstance = (maybeInstance as (window: Window) => PurifyInstance)(win)
  return purifyInstance
}

/** Node 单测无 DOM 时的保守回退：剥离脚本与危险协议，保留结构标签。 */
function fallbackSanitize(html: string): string {
  return html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
    .replace(/\son[a-z]+\s*=\s*(['"]).*?\1/gi, '')
    .replace(/\son[a-z]+\s*=\s*[^\s>]+/gi, '')
    .replace(/\shref\s*=\s*(['"])\s*javascript:[^'"]*\1/gi, '')
    .replace(/\ssrc\s*=\s*(['"])\s*javascript:[^'"]*\1/gi, '')
}

/** 是否允许 Markdown 远程图片自动加载。默认关闭，避免聊天内容外呼不可控资源。 */
export const ALLOW_REMOTE_MARKDOWN_IMAGES = false

const allowedTags = [
  'a', 'b', 'blockquote', 'br', 'code', 'del', 'em',
  'h1', 'h2', 'h3', 'h4', 'hr', 'i', 'li', 'ol', 'p',
  'pre', 'strong', 'table', 'tbody', 'td', 'th',
  'thead', 'tr', 'ul', 'img', 'div', 'span', 'input',
]

const allowedAttrs = [
  'class', 'href', 'rel', 'target', 'title', 'src', 'alt',
  'data-lang', 'type', 'checked', 'disabled', 'role', 'aria-label',
]

const renderCache = new Map<string, string>()
const RENDER_CACHE_MAX = 500

let rendererReady = false

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function sanitizeLanguage(lang: string): string {
  return lang.trim().replace(/[^a-zA-Z0-9_+#.-]/g, '').slice(0, 32)
}

function isSafeHref(href: string): boolean {
  if (!href) return false
  // 允许相对路径、锚点、同源路径；带协议时仅 http/https
  if (href.startsWith('#') || href.startsWith('/') || href.startsWith('./') || href.startsWith('../')) {
    return true
  }
  const hasScheme = /^[a-z][a-z0-9+.-]*:/i.test(href)
  if (!hasScheme) return true
  return /^https?:/i.test(href)
}

function isSafeImageSrc(src: string): boolean {
  return /^https?:/i.test(src)
}

function ensureMarkdownRenderer(): void {
  if (rendererReady) return

  marked.use({
    renderer: {
      code({ text, lang, escaped }) {
        const language = sanitizeLanguage(lang || '')
        const body = `${text.replace(/\n$/, '')}\n`
        const escapedBody = escaped ? body : escapeHtml(body)
        const codeHtml = language
          ? `<pre><code class="language-${language}">${escapedBody}</code></pre>\n`
          : `<pre><code>${escapedBody}</code></pre>\n`

        if (!language) {
          return `<div class="markdown-code">${codeHtml}</div>\n`
        }

        return (
          `<div class="markdown-code" data-lang="${language}">` +
          `<span class="markdown-code__lang">${language}</span>` +
          `${codeHtml}</div>\n`
        )
      },

      link({ href, title, tokens }) {
        const text = this.parser.parseInline(tokens)
        const url = href || ''
        if (!isSafeHref(url)) return text
        const titleAttr = title ? ` title="${escapeHtml(title)}"` : ''
        return (
          `<a href="${escapeHtml(url)}"${titleAttr}` +
          ` target="_blank" rel="noopener noreferrer">${text}</a>`
        )
      },

      image({ href, title, text }) {
        const alt = escapeHtml(text || 'image')
        if (!ALLOW_REMOTE_MARKDOWN_IMAGES) {
          return (
            `<span class="markdown-image-placeholder" role="img" aria-label="${alt}">` +
            `[${alt}]</span>`
          )
        }
        const src = href || ''
        if (!isSafeImageSrc(src)) {
          return (
            `<span class="markdown-image-placeholder" role="img" aria-label="${alt}">` +
            `[${alt}]</span>`
          )
        }
        const titleAttr = title ? ` title="${escapeHtml(title)}"` : ''
        return `<img src="${escapeHtml(src)}" alt="${alt}"${titleAttr}>`
      },

      table(token) {
        const html = Renderer.prototype.table.call(this, token)
        return `<div class="markdown-table-wrap">${html}</div>\n`
      },
    },
  })

  rendererReady = true
}

/**
 * 流式未闭合围栏补闭合，减轻 marked 把后续文本吞进代码块的跳动。
 */
export function stabilizeStreamingMarkdown(text: string): string {
  if (!text) return text
  let fenceCount = 0
  for (const line of text.split('\n')) {
    if (/^ {0,3}```/.test(line)) fenceCount += 1
  }
  if (fenceCount % 2 === 0) return text
  return text.endsWith('\n') ? `${text}\`\`\`` : `${text}\n\`\`\``
}

export interface RenderMarkdownOptions {
  /** 为 true 时做围栏补闭合，且结果不写入 LRU（避免流式中间态污染终态缓存） */
  streaming?: boolean
}

function sanitizeHtml(html: string): string {
  const purify = getPurify()
  if (!purify) {
    return fallbackSanitize(html)
  }
  return purify.sanitize(html, {
    ALLOWED_TAGS: allowedTags,
    ALLOWED_ATTR: allowedAttrs,
    ALLOW_DATA_ATTR: false,
  })
}

/**
 * 渲染 Markdown 为安全的 HTML（带记忆化缓存）
 */
export function renderMarkdown(text: string, options?: RenderMarkdownOptions): string {
  if (!text) return ''

  ensureMarkdownRenderer()

  const streaming = options?.streaming === true
  const source = streaming ? stabilizeStreamingMarkdown(text) : text

  if (!streaming) {
    const cached = renderCache.get(text)
    if (cached !== undefined) return cached
  }

  const html = marked.parse(source) as string
  const clean = sanitizeHtml(html)

  if (!streaming) {
    if (renderCache.size >= RENDER_CACHE_MAX) {
      const firstKey = renderCache.keys().next().value
      if (firstKey !== undefined) renderCache.delete(firstKey)
    }
    renderCache.set(text, clean)
  }

  return clean
}

/** 测试钩子：清空渲染缓存 */
export function clearMarkdownCache(): void {
  renderCache.clear()
}

/**
 * 格式化消息时间
 * - 今天：14:30
 * - 昨天：昨天 14:30
 * - 更早：3月15日 周六 14:30（跨年则加年份）
 */
export function formatMessageTime(date: Date): string {
  const now = new Date()
  const timeStr = date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })

  const startOfDay = (d: Date) => {
    const copy = new Date(d)
    copy.setHours(0, 0, 0, 0)
    return copy.getTime()
  }

  const diffDays = Math.floor((startOfDay(now) - startOfDay(date)) / 86400000)

  if (diffDays === 0) return timeStr
  if (diffDays === 1) return `昨天 ${timeStr}`

  const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  const month = date.getMonth() + 1
  const day = date.getDate()
  const weekday = weekdays[date.getDay()]!

  if (date.getFullYear() === now.getFullYear()) {
    return `${month}月${day}日 ${weekday} ${timeStr}`
  }
  return `${date.getFullYear()}年${month}月${day}日 ${weekday} ${timeStr}`
}

/** @deprecated 使用 formatMessageTime */
export function formatTime(date: Date): string {
  return formatMessageTime(date)
}
