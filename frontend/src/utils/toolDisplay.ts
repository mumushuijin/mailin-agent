// 工具显示配置
export interface ToolDisplayConfig {
  name: string         // 友好名称
  icon: string         // emoji 图标
  hidden?: boolean     // 是否隐藏
  pastVerb?: string    // 完成态动词
  runningVerb?: string // 执行中动词
  objectKeys?: string[] // 优先展示的核心对象字段
}

export const TOOL_DISPLAY_CONFIG: Record<string, ToolDisplayConfig> = {
  // 内置工具 - 隐藏
  Thought: { name: '思考', icon: '💭', hidden: true, pastVerb: '已整理', runningVerb: '正在整理' },
  Finish: { name: '完成', icon: '✅', hidden: true, pastVerb: '已完成', runningVerb: '正在完成' },

  // 桥接披露工具（内部流程，不在 UI 展示）
  tool_search: { name: '搜索工具', icon: '🔍', hidden: true, pastVerb: '已搜索', runningVerb: '正在搜索', objectKeys: ['query', 'name'] },
  tool_describe: { name: '工具详情', icon: '📋', hidden: true, pastVerb: '已查看', runningVerb: '正在查看', objectKeys: ['name', 'tool'] },
  tool_call: { name: '调用工具', icon: '🔧', hidden: true, pastVerb: '已调用', runningVerb: '正在调用', objectKeys: ['name', 'tool'] },
  mcp_status: { name: 'MCP 状态', icon: '🔌', hidden: true, pastVerb: '已检查', runningVerb: '正在检查', objectKeys: ['name', 'server'] },

  // 文件操作工具
  read_file: { name: '文件', icon: '📄', pastVerb: '已读取', runningVerb: '正在读取', objectKeys: ['path', 'file_path', 'file', 'filename'] },
  write_file: { name: '文件', icon: '✏️', pastVerb: '已写入', runningVerb: '正在写入', objectKeys: ['path', 'file_path', 'file', 'filename'] },
  replace_in_file: { name: '文件', icon: '📝', pastVerb: '已修改', runningVerb: '正在修改', objectKeys: ['path', 'file_path', 'file', 'filename'] },
  list_directory: { name: '目录', icon: '📂', pastVerb: '已列出', runningVerb: '正在列出', objectKeys: ['path', 'directory', 'dir'] },
  search_files: { name: '文件内容', icon: '🔍', pastVerb: '已搜索', runningVerb: '正在搜索', objectKeys: ['query', 'pattern', 'path'] },
  glob_search: { name: '文件', icon: '🗂️', pastVerb: '已搜索', runningVerb: '正在搜索', objectKeys: ['pattern', 'path', 'query'] },
  mkdir: { name: '目录', icon: '📁', pastVerb: '已创建', runningVerb: '正在创建', objectKeys: ['path', 'directory', 'dir'] },
  move_file: { name: '文件', icon: '📦', pastVerb: '已移动', runningVerb: '正在移动', objectKeys: ['source', 'src', 'destination', 'dest', 'path'] },
  delete_file: { name: '文件', icon: '🗑️', pastVerb: '已删除', runningVerb: '正在删除', objectKeys: ['path', 'file_path', 'file', 'filename'] },
  // 旧名称兼容
  Read: { name: '文件', icon: '📄', pastVerb: '已读取', runningVerb: '正在读取', objectKeys: ['path', 'file_path', 'file', 'filename'] },
  Write: { name: '文件', icon: '✏️', pastVerb: '已写入', runningVerb: '正在写入', objectKeys: ['path', 'file_path', 'file', 'filename'] },
  Edit: { name: '文件', icon: '📝', pastVerb: '已修改', runningVerb: '正在修改', objectKeys: ['path', 'file_path', 'file', 'filename'] },
  MultiEdit: { name: '文件', icon: '📝', pastVerb: '已批量修改', runningVerb: '正在批量修改', objectKeys: ['path', 'file_path', 'file', 'filename'] },

  run_shell: { name: '命令', icon: '💻', pastVerb: '已运行', runningVerb: '正在运行', objectKeys: ['command', 'cmd', 'script'] },
  process: { name: '进程', icon: '📟', pastVerb: '已处理', runningVerb: '正在处理', objectKeys: ['action', 'command', 'name'] },
  todo: { name: '计划', icon: '✅', pastVerb: '已更新', runningVerb: '正在更新', objectKeys: ['content', 'title', 'name'] },
  ask_user: { name: '问题', icon: '❓', pastVerb: '已询问', runningVerb: '正在询问', objectKeys: ['prompt', 'question'] },

  // 计算工具
  python_calculator: { name: '计算', icon: '🔢', pastVerb: '已计算', runningVerb: '正在计算', objectKeys: ['expression', 'query'] },

  // 日期时间
  get_current_time: { name: '时间', icon: '🕐', pastVerb: '已获取', runningVerb: '正在获取', objectKeys: ['timezone', 'location'] },

  // 记忆工具（麦林自定义）
  memory: { name: '记忆', icon: '🧠', pastVerb: '已处理', runningVerb: '正在处理', objectKeys: ['path', 'query', 'content'] },
  memory_search: { name: '记忆', icon: '🔍', pastVerb: '已搜索', runningVerb: '正在搜索', objectKeys: ['query'] },
  memory_get: { name: '记忆', icon: '📖', pastVerb: '已读取', runningVerb: '正在读取', objectKeys: ['path', 'name'] },
  memory_add: { name: '记忆', icon: '📝', pastVerb: '已添加', runningVerb: '正在添加', objectKeys: ['content', 'path'] },
  memory_update_longterm: { name: '记忆', icon: '📚', pastVerb: '已更新', runningVerb: '正在更新', objectKeys: ['content', 'path'] },
  memory_list: { name: '记忆文件', icon: '📋', pastVerb: '已列出', runningVerb: '正在列出', objectKeys: ['path'] },
  memory_cleanup: { name: '记忆', icon: '🧹', pastVerb: '已清理', runningVerb: '正在清理', objectKeys: ['path'] },

  // 任务工具
  Task: { name: '子任务', icon: '📋', pastVerb: '已完成', runningVerb: '正在执行', objectKeys: ['prompt', 'name', 'title'] },

  // 命令执行工具
  execute_command: { name: '命令', icon: '💻', pastVerb: '已运行', runningVerb: '正在运行', objectKeys: ['command', 'cmd', 'script'] },
  exec_run: { name: '命令', icon: '💻', pastVerb: '已运行', runningVerb: '正在运行', objectKeys: ['command', 'cmd', 'script'] },
  exec_allowed_commands: { name: '命令规则', icon: '📋', pastVerb: '已查看', runningVerb: '正在查看', objectKeys: ['query', 'name'] },
  exec_dangerous_patterns: { name: '危险规则', icon: '⚠️', pastVerb: '已查看', runningVerb: '正在查看', objectKeys: ['query', 'name'] },

  // 网络工具
  web_search: { name: '网页', icon: '🌐', pastVerb: '已搜索', runningVerb: '正在搜索', objectKeys: ['query'] },
  search_web: { name: '网页', icon: '🌐', pastVerb: '已搜索', runningVerb: '正在搜索', objectKeys: ['query'] },
  web_fetch: { name: '网页', icon: '📡', pastVerb: '已获取', runningVerb: '正在获取', objectKeys: ['url'] },
  fetch_url: { name: '网页', icon: '📡', pastVerb: '已获取', runningVerb: '正在获取', objectKeys: ['url'] },
}

// 默认配置（未知工具）
export const DEFAULT_TOOL_CONFIG: ToolDisplayConfig = {
  name: '工具',
  icon: '🔧',
  pastVerb: '已执行',
  runningVerb: '正在执行',
  objectKeys: ['path', 'command', 'cmd', 'query', 'name', 'title'],
}

// 解析 MCP 工具名：mcp_{server}_{tool} → 友好展示
function parseMcpToolName(toolName: string): ToolDisplayConfig {
  if (!toolName.startsWith('mcp_')) {
    return DEFAULT_TOOL_CONFIG
  }
  const rest = toolName.slice(4)
  const lastUnderscore = rest.lastIndexOf('_')
  const rawTool = lastUnderscore >= 0 ? rest.slice(lastUnderscore + 1) : rest
  const friendly = rawTool
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .trim()
  return {
    name: friendly || rawTool || 'MCP 工具',
    icon: '🔌',
  }
}

// 获取工具显示配置
export function getToolConfig(toolName: string): ToolDisplayConfig {
  if (TOOL_DISPLAY_CONFIG[toolName]) {
    return TOOL_DISPLAY_CONFIG[toolName]
  }
  if (toolName.startsWith('mcp_')) {
    return parseMcpToolName(toolName)
  }
  return DEFAULT_TOOL_CONFIG
}

export interface ToolActionDisplay {
  verb: string
  name: string
  target?: string
  targetKind?: 'path' | 'command' | 'value'
}

type ToolActionStatus = 'running' | 'done' | 'error' | 'cancelled' | 'policy_denied'

const COMMAND_KEYS = new Set(['command', 'cmd', 'script', 'shell'])
const PATH_KEYS = new Set(['path', 'file_path', 'file', 'filename', 'directory', 'dir', 'source', 'src', 'destination', 'dest'])

const stringifyTarget = (value: unknown): string => {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

export function getToolActionDisplay(
  toolName: string,
  args: Record<string, unknown> | undefined,
  status: ToolActionStatus,
): ToolActionDisplay {
  const config = getToolConfig(toolName)
  const source = args || {}
  const keys = config.objectKeys || []
  let targetKey = ''
  let target = ''

  for (const key of keys) {
    const candidate = stringifyTarget(source[key])
    if (candidate) {
      targetKey = key
      target = candidate
      break
    }
  }

  if (!target) {
    for (const [key, value] of Object.entries(source)) {
      const candidate = stringifyTarget(value)
      if (candidate) {
        targetKey = key
        target = candidate
        break
      }
    }
  }

  if (target.length > 140) {
    target = `${target.slice(0, 137)}...`
  }

  const targetKind = COMMAND_KEYS.has(targetKey)
    ? 'command'
    : PATH_KEYS.has(targetKey)
      ? 'path'
      : target
        ? 'value'
        : undefined

  const verb = status === 'running'
    ? (config.runningVerb || '正在执行')
    : (config.pastVerb || '已执行')

  return {
    verb,
    name: config.name,
    target: target || undefined,
    targetKind,
  }
}

// 格式化工具参数显示
export function formatToolArgs(args: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) {
    return ''
  }

  const parts: string[] = []
  for (const [key, value] of Object.entries(args)) {
    let displayValue: string
    if (typeof value === 'string') {
      // 截断长字符串
      displayValue = value.length > 100 ? value.slice(0, 100) + '...' : value
    } else if (typeof value === 'object') {
      displayValue = JSON.stringify(value)
      if (displayValue.length > 100) {
        displayValue = displayValue.slice(0, 100) + '...'
      }
    } else {
      displayValue = String(value)
    }
    parts.push(`${key}: ${displayValue}`)
  }
  return parts.join('\n')
}

// 格式化工具结果显示
export function formatToolResult(result: string | undefined): string {
  if (!result) return ''
  // 截断长结果
  return result.length > 500 ? result.slice(0, 500) + '...' : result
}

export function toolResultLooksLikeError(result?: string): boolean {
  if (!result?.trim()) return false
  const text = result.trim()
  if (text.startsWith('❌')) return true
  if (text.includes('未绑定项目工作区')) return true
  try {
    const parsed = JSON.parse(text) as { error?: unknown }
    if (parsed && typeof parsed === 'object' && parsed.error) return true
  } catch {
    return false
  }
  return false
}

export function toolErrorPreview(result?: string): string {
  if (!result?.trim()) return ''
  const text = result.trim()
  try {
    const parsed = JSON.parse(text) as { error?: unknown }
    if (parsed && typeof parsed === 'object' && parsed.error) {
      return String(parsed.error)
    }
  } catch {
    // 非 JSON 错误原文
  }
  const firstLine = text.split('\n')[0] || text
  return firstLine.length > 80 ? `${firstLine.slice(0, 80)}…` : firstLine
}

export function toolResultPreview(result?: string): string {
  if (!result?.trim()) return ''
  const firstLine = result.trim().split('\n')[0] || result
  return firstLine.length > 120 ? `${firstLine.slice(0, 117)}…` : firstLine
}
