const LAST_WORKSPACE_KEY = 'mailin.lastWorkspacePath'

export function getLastWorkspacePath(): string {
  return localStorage.getItem(LAST_WORKSPACE_KEY) || ''
}

export function saveLastWorkspacePath(path: string) {
  if (path.trim()) {
    localStorage.setItem(LAST_WORKSPACE_KEY, path.trim())
  }
}

export async function pickDirectory(current = ''): Promise<string | null> {
  const native = window.mailin?.dialog?.selectDirectory
  if (native) {
    const selected = await native()
    return selected
  }
  const typed = window.prompt('请输入本机项目文件夹的绝对路径', current)
  return typed?.trim() || null
}

export async function openDirectory(folderPath: string): Promise<'opened' | 'copied' | 'failed'> {
  const trimmed = folderPath.trim()
  if (!trimmed) return 'failed'
  const native = window.mailin?.shell?.openPath
  if (native) {
    try {
      const result = await native(trimmed)
      if (result?.ok !== false && !result?.error) return 'opened'
    } catch {
      // Electron 打开失败时退回复制路径
    }
  }
  try {
    await navigator.clipboard.writeText(trimmed)
    return 'copied'
  } catch {
    return 'failed'
  }
}
