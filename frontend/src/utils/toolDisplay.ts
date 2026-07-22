// 工具显示配置
export interface ToolDisplayConfig {
  name: string         // 友好名称
  icon: string         // emoji 图标
  hidden?: boolean     // 是否隐藏
}

export const TOOL_DISPLAY_CONFIG: Record<string, ToolDisplayConfig> = {
  // 内置工具 - 隐藏
  Thought: { name: '思考', icon: '💭', hidden: true },
  Finish: { name: '完成', icon: '✅', hidden: true },

  // 桥接披露工具（内部流程，不在 UI 展示）
  tool_search: { name: '搜索工具', icon: '🔍', hidden: true },
  tool_describe: { name: '工具详情', icon: '📋', hidden: true },
  tool_call: { name: '调用工具', icon: '🔧', hidden: true },
  mcp_status: { name: 'MCP 状态', icon: '🔌', hidden: true },

  // 文件操作工具
  read_file: { name: '读取文件', icon: '📄' },
  write_file: { name: '写入文件', icon: '✏️' },
  replace_in_file: { name: '替换文本', icon: '📝' },
  list_directory: { name: '列出目录', icon: '📂' },
  search_files: { name: '搜索文件内容', icon: '🔍' },
  glob_search: { name: 'Glob 搜索', icon: '🗂️' },
  mkdir: { name: '创建目录', icon: '📁' },
  move_file: { name: '移动文件', icon: '📦' },
  delete_file: { name: '删除文件', icon: '🗑️' },
  // 旧名称兼容
  Read: { name: '读取文件', icon: '📄' },
  Write: { name: '写入文件', icon: '✏️' },
  Edit: { name: '编辑文件', icon: '📝' },
  MultiEdit: { name: '批量编辑', icon: '📝' },

  // 计算工具
  python_calculator: { name: '计算器', icon: '🔢' },

  // 日期时间
  get_current_time: { name: '当前时间', icon: '🕐' },

  // 记忆工具（麦林自定义）
  memory: { name: '记忆操作', icon: '🧠' },
  memory_search: { name: '搜索记忆', icon: '🔍' },
  memory_get: { name: '读取记忆', icon: '📖' },
  memory_add: { name: '添加记忆', icon: '📝' },
  memory_update_longterm: { name: '更新长期记忆', icon: '📚' },
  memory_list: { name: '列出记忆文件', icon: '📋' },
  memory_cleanup: { name: '清理过期记忆', icon: '🧹' },

  // 任务工具
  Task: { name: '子任务', icon: '📋' },

  // 命令执行工具
  execute_command: { name: '执行命令', icon: '💻' },
  exec_run: { name: '执行命令', icon: '💻' },
  exec_allowed_commands: { name: '查看允许的命令', icon: '📋' },
  exec_dangerous_patterns: { name: '查看危险命令', icon: '⚠️' },

  // 网络工具
  web_search: { name: '网络搜索', icon: '🌐' },
  search_web: { name: '网络搜索', icon: '🌐' },
  web_fetch: { name: '获取网页', icon: '📡' },
  fetch_url: { name: '获取网页', icon: '📡' },
}

// 默认配置（未知工具）
export const DEFAULT_TOOL_CONFIG: ToolDisplayConfig = {
  name: '工具',
  icon: '🔧',
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
