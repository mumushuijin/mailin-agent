import { marked } from 'marked'
import DOMPurify from 'dompurify'

// 配置 marked
marked.setOptions({
  breaks: true, // 换行符转换
  gfm: true, // GitHub Flavored Markdown
})

// 允许的标签
const allowedTags = [
  'a', 'b', 'blockquote', 'br', 'code', 'del', 'em',
  'h1', 'h2', 'h3', 'h4', 'hr', 'i', 'li', 'ol', 'p',
  'pre', 'strong', 'table', 'tbody', 'td', 'th',
  'thead', 'tr', 'ul', 'img'
]

// 允许的属性
const allowedAttrs = ['class', 'href', 'rel', 'target', 'title', 'src', 'alt']

// 渲染结果缓存：模板会在每次重渲染（如流式 chunk 到达）时对所有可见消息重新调用
// renderMarkdown，按文本记忆化可避免对稳定历史内容的重复解析，大幅降低重渲染开销。
const renderCache = new Map<string, string>()
const RENDER_CACHE_MAX = 500

/**
 * 渲染 Markdown 为安全的 HTML（带记忆化缓存）
 */
export function renderMarkdown(text: string): string {
  if (!text) return ''

  const cached = renderCache.get(text)
  if (cached !== undefined) return cached

  // 解析 Markdown
  const html = marked.parse(text) as string

  // 清理 HTML，防止 XSS
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: allowedTags,
    ALLOWED_ATTR: allowedAttrs,
  })

  // 简单 LRU：超出上限时淘汰最早插入的键
  if (renderCache.size >= RENDER_CACHE_MAX) {
    const firstKey = renderCache.keys().next().value
    if (firstKey !== undefined) renderCache.delete(firstKey)
  }
  renderCache.set(text, clean)

  return clean
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
