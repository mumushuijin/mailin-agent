const { app, BrowserWindow, shell, ipcMain, dialog } = require('electron')
const path = require('path')
const fs = require('fs')
const net = require('net')
const { spawn, execFile } = require('child_process')
const { ensureDirectory, ensureRuntimeRoot, isSameOrChild, runtimePaths } = require('./runtime.cjs')

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged
const managedBackend = app.isPackaged || process.env.MAILIN_MANAGED_BACKEND === '1'

let mainWindow = null
let backendProcess = null
let backendLogStream = null
let quitting = false
let runtimeRoot = null
let recentBackendOutput = ''

const backendState = {
  mode: managedBackend ? 'managed' : 'external',
  status: managedBackend ? 'starting' : 'external',
  endpoint: null,
  installRoot: null,
  runtimeRoot: null,
  paths: null,
  error: null,
  pid: null,
}

function normalizeEndpoint(raw) {
  const value = String(raw || '').trim().replace(/\/$/, '')
  return value.replace(/\/api$/, '')
}

function bootstrapPath() {
  return path.join(app.getPath('userData'), 'mailin.bootstrap.json')
}

function readStoredRuntimeRoot() {
  try {
    const parsed = JSON.parse(fs.readFileSync(bootstrapPath(), 'utf8'))
    const installRoot = path.dirname(process.resourcesPath)
    return parsed.installRoot === installRoot && typeof parsed.runtimeRoot === 'string' && parsed.runtimeRoot.trim()
      ? parsed.runtimeRoot.trim() : null
  } catch {
    return null
  }
}

function readLegacyDataRoot() {
  try {
    const parsed = JSON.parse(fs.readFileSync(bootstrapPath(), 'utf8'))
    return typeof parsed.dataRoot === 'string' && parsed.dataRoot.trim() ? parsed.dataRoot.trim() : null
  } catch {
    return null
  }
}

function writeStoredRuntimeRoot(root) {
  ensureDirectory(app.getPath('userData'))
  fs.writeFileSync(
    bootstrapPath(),
    `${JSON.stringify({ version: 2, installRoot: path.dirname(process.resourcesPath), runtimeRoot: root }, null, 2)}\n`,
    'utf8',
  )
}

async function resolveRuntimeRoot() {
  const explicit = process.env.MAILIN_RUNTIME_ROOT?.trim()
  if (explicit) {
    runtimeRoot = explicit
    return explicit
  }

  const stored = readStoredRuntimeRoot()
  if (stored) {
    runtimeRoot = stored
    return stored
  }

  const fallback = !managedBackend ? path.resolve(__dirname, '..', '..', '.runtime') : path.join(app.getPath('userData'), 'runtime')
  if (!managedBackend) {
    runtimeRoot = fallback
    return fallback
  }

  const choice = await dialog.showMessageBox({
    type: 'question',
    title: '选择 Mailin 运行根',
    message: 'Mailin 的运行数据保存在哪里？',
    detail: `安装目录：${path.dirname(process.resourcesPath)}。运行根将派生 data、cache、tmp、log 四个目录。`,
    buttons: ['选择文件夹', '使用默认位置'],
    defaultId: 0,
    cancelId: 1,
  })

  let selected = fallback
  if (choice.response === 0) {
    const result = await dialog.showOpenDialog({
      properties: ['openDirectory', 'createDirectory'],
    })
    if (!result.canceled && result.filePaths[0]) selected = result.filePaths[0]
  }

  runtimeRoot = selected
  return selected
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : null
      server.close(() => (port ? resolve(port) : reject(new Error('无法获取可用端口'))))
    })
  })
}

function backendExecutablePath() {
  const override = process.env.MAILIN_BACKEND_EXECUTABLE?.trim()
  if (override) return override

  const name = process.platform === 'win32' ? 'mailin-backend.exe' : 'mailin-backend'
  const packaged = path.join(process.resourcesPath, 'backend', name)
  if (app.isPackaged) return packaged
  if (fs.existsSync(packaged)) return packaged

  return path.join(__dirname, '..', '..', 'backend', 'dist', 'mailin-backend', name)
}

function appendBackendLog(chunk) {
  const text = String(chunk)
  recentBackendOutput = `${recentBackendOutput}${text}`.slice(-3000)
  process.stdout.write(`[mailin-backend] ${text}`)
  if (backendLogStream) backendLogStream.write(text)
}

async function waitForHealth(endpoint, timeoutMs = 30_000) {
  const started = Date.now()
  let lastError = null
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(`${endpoint}/health`)
      if (response.ok) return true
      lastError = new Error(`health status ${response.status}`)
    } catch (error) {
      lastError = error
    }
    if (backendState.status === 'unavailable') {
      throw new Error(backendState.error || '后端启动失败')
    }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw lastError || new Error('后端健康检查超时')
}

async function migrateLegacyReleaseData(executable, root) {
  if (!app.isPackaged) return
  const legacyData = readLegacyDataRoot()
  if (!legacyData || !fs.existsSync(legacyData)) return
  const args = [
    'migrate', '--runtime-root', root,
    '--resources-dir', process.resourcesPath,
    '--legacy-data-root', legacyData,
  ]
  const oldCache = path.join(app.getPath('cache'), 'Mailin')
  const oldTmp = path.join(app.getPath('temp'), 'Mailin')
  if (fs.existsSync(oldCache)) args.push('--legacy-cache-dir', oldCache)
  if (fs.existsSync(oldTmp)) args.push('--legacy-temp-dir', oldTmp)
  await new Promise((resolve, reject) => {
    const child = spawn(executable, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    let diagnostic = ''
    for (const stream of [child.stdout, child.stderr]) {
      stream.on('data', (chunk) => { diagnostic = `${diagnostic}${String(chunk)}`.slice(-4000) })
    }
    child.once('error', reject)
    child.once('exit', (code) => code === 0 ? resolve() : reject(new Error(`旧数据迁移失败 (${legacyData} → ${root}): ${diagnostic || `code=${code}`}`)))
  })
}

async function startManagedBackend(root) {
  quitting = false
  recentBackendOutput = ''
  const executable = backendExecutablePath()
  if (!fs.existsSync(executable)) {
    throw new Error(`找不到后端可执行文件: ${executable}`)
  }

  await migrateLegacyReleaseData(executable, root)
  const port = await getFreePort()
  const endpoint = `http://127.0.0.1:${port}`
  const paths = ensureRuntimeRoot(root, process.resourcesPath)
  const logDir = paths.log

  backendLogStream = fs.createWriteStream(path.join(logDir, 'managed-backend.log'), { flags: 'a' })
  const args = [
    'serve',
    '--mode', 'production',
    '--host', '127.0.0.1',
    '--port', String(port),
    '--runtime-root', root,
    '--resources-dir', process.resourcesPath,
  ]

  const child = spawn(executable, args, {
    cwd: process.resourcesPath,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  backendProcess = child
  backendState.pid = child.pid || null
  backendState.endpoint = endpoint
  backendState.runtimeRoot = root
  backendState.paths = paths

  child.stdout.on('data', appendBackendLog)
  child.stderr.on('data', appendBackendLog)
  child.on('error', (error) => {
    if (backendProcess !== child) return
    backendState.status = 'unavailable'
    backendState.error = error.message
    backendState.endpoint = null
  })
  child.on('exit', (code, signal) => {
    if (backendProcess !== child) return
    backendProcess = null
    backendState.status = quitting ? 'stopped' : 'unavailable'
    backendState.error = quitting ? null : `后端已退出 code=${code} signal=${signal || 'none'}: ${recentBackendOutput}`
    backendState.pid = null
    backendState.endpoint = null
    backendLogStream?.end()
    backendLogStream = null
  })

  await waitForHealth(endpoint)
  if (backendProcess !== child || backendState.status === 'unavailable') {
    throw new Error(backendState.error || '后端在就绪后退出')
  }
  backendState.status = 'ready'
  writeStoredRuntimeRoot(root)
}

async function startBackend(root) {
  backendState.runtimeRoot = root
  backendState.installRoot = path.dirname(process.resourcesPath)
  backendState.paths = runtimePaths(root)
  if (!managedBackend) {
    backendState.endpoint = normalizeEndpoint(
      process.env.MAILIN_BACKEND_URL || 'http://127.0.0.1:8000',
    )
    backendState.status = 'external'
    return
  }

  try {
    await startManagedBackend(root)
  } catch (error) {
    backendState.status = 'unavailable'
    backendState.error = error instanceof Error ? error.message : String(error)
    backendState.endpoint = null
  }
}

async function stopBackend() {
  if (!backendProcess) return
  const child = backendProcess
  backendProcess = null
  quitting = true
  if (child.exitCode === null) {
    child.kill()
    await Promise.race([
      new Promise((resolve) => child.once('exit', resolve)),
      new Promise((resolve) => setTimeout(resolve, 1500)),
    ])
  }
  if (child.exitCode === null && process.platform === 'win32' && child.pid) {
    await new Promise((resolve) => execFile('taskkill', ['/pid', String(child.pid), '/t', '/f'], () => resolve()))
  }
  backendState.status = 'stopped'
  backendState.endpoint = null
  backendState.pid = null
  backendLogStream?.end()
  backendLogStream = null
}

function setupBackendIpc() {
  ipcMain.handle('backend:getState', () => ({ ...backendState }))
  ipcMain.handle('backend:getEndpoint', () => {
    return ['ready', 'external'].includes(backendState.status)
      ? backendState.endpoint
      : null
  })
  ipcMain.handle('backend:retry', async () => {
    if (!runtimeRoot) return { ...backendState }
    if (backendProcess) await stopBackend()
    backendState.status = 'starting'
    backendState.error = null
    await startBackend(runtimeRoot)
    return { ...backendState }
  })
  ipcMain.handle('backend:chooseRuntimeRoot', async () => {
    const result = await dialog.showOpenDialog({ properties: ['openDirectory', 'createDirectory'] })
    if (result.canceled || !result.filePaths[0]) return { ...backendState }
    const selected = result.filePaths[0]
    try {
      if (isSameOrChild(selected, process.resourcesPath) || isSameOrChild(process.resourcesPath, selected)) {
        throw new Error(`运行根不能与只读资源目录重叠: ${selected}`)
      }
      const paths = runtimePaths(selected)
      if (backendProcess) await stopBackend()
      runtimeRoot = selected
      backendState.runtimeRoot = selected
      backendState.paths = paths
      backendState.status = 'starting'
      backendState.error = null
      await startBackend(selected)
    } catch (error) {
      backendState.status = 'unavailable'
      backendState.error = error instanceof Error ? error.message : String(error)
    }
    return { ...backendState }
  })
}

function setupWindowControls() {
  ipcMain.handle('window:minimize', (event) => {
    BrowserWindow.fromWebContents(event.sender)?.minimize()
  })

  ipcMain.handle('window:maximize', (event) => {
    const win = BrowserWindow.fromWebContents(event.sender)
    if (!win) return
    if (win.isMaximized()) win.unmaximize()
    else win.maximize()
  })

  ipcMain.handle('window:close', (event) => {
    BrowserWindow.fromWebContents(event.sender)?.close()
  })

  ipcMain.handle('window:isMaximized', (event) => {
    return BrowserWindow.fromWebContents(event.sender)?.isMaximized() ?? false
  })

  ipcMain.handle('dialog:selectDirectory', async (event) => {
    const win = BrowserWindow.fromWebContents(event.sender)
    const result = await dialog.showOpenDialog(win ?? undefined, {
      properties: ['openDirectory'],
    })
    if (result.canceled || !result.filePaths[0]) return null
    return result.filePaths[0]
  })

  ipcMain.handle('shell:openPath', async (_event, folderPath) => {
    if (typeof folderPath !== 'string' || !folderPath.trim()) {
      return { ok: false, error: 'empty path' }
    }
    const error = await shell.openPath(folderPath.trim())
    return { ok: !error, error: error || null }
  })
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    title: '麦林',
    show: false,
    frame: false,
    backgroundColor: '#f3efe4',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())

  const sendMaximizeState = () => {
    mainWindow.webContents.send('window:maximized-changed', mainWindow.isMaximized())
  }

  mainWindow.on('maximize', sendMaximizeState)
  mainWindow.on('unmaximize', sendMaximizeState)
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
    if (process.env.OPEN_DEVTOOLS === '1') mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'))
  }
}

app.whenReady().then(async () => {
  setupWindowControls()
  setupBackendIpc()
  try {
    const root = await resolveRuntimeRoot()
    await startBackend(root)
  } catch (error) {
    backendState.status = 'unavailable'
    backendState.error = error instanceof Error ? error.message : String(error)
    backendState.endpoint = null
  }
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('before-quit', async (event) => {
  if (backendProcess && !quitting) {
    event.preventDefault()
    await stopBackend()
    app.quit()
  }
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
