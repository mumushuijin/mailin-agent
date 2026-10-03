const fs = require('fs')
const path = require('path')
const { spawn, spawnSync } = require('child_process')

const frontendRoot = path.resolve(__dirname, '..')
const packageJson = JSON.parse(fs.readFileSync(path.join(frontendRoot, 'package.json'), 'utf8'))
const outputRoot = path.resolve(frontendRoot, packageJson.build?.directories?.output || 'release')

const env = { ...process.env }
if (env.MAILIN_BUILD_MIRROR === '1') {
  env.ELECTRON_MIRROR ||= 'https://npmmirror.com/mirrors/electron/'
  env.ELECTRON_BUILDER_BINARIES_MIRROR ||= 'https://npmmirror.com/mirrors/electron-builder-binaries/'
  env.npm_config_registry ||= 'https://mirrors.tuna.tsinghua.edu.cn/npm/'
}
env.CSC_IDENTITY_AUTO_DISCOVERY ||= 'false'
env.ELECTRON_BUILDER_CACHE ||= path.join(frontendRoot, '..', '.electron-builder-cache')

const cli = require.resolve('electron-builder/cli.js')
const electronDist = path.join(frontendRoot, 'node_modules', 'electron', 'dist')
const argumentsForBuilder = [cli, ...process.argv.slice(2)]
if (fs.existsSync(path.join(electronDist, process.platform === 'win32' ? 'electron.exe' : 'electron'))
  && !argumentsForBuilder.some((argument) => argument.startsWith('--config.electronDist'))) {
  argumentsForBuilder.push('--config.electronDist=./node_modules/electron/dist')
}
const child = spawn(process.execPath, argumentsForBuilder, {
  cwd: frontendRoot,
  env,
  stdio: 'inherit',
  windowsHide: true,
})

function recoverTemporaryUnpackedDirectories() {
  if (!fs.existsSync(outputRoot)) return false
  let recovered = false
  for (const entry of fs.readdirSync(outputRoot, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.endsWith('.tmp')) continue
    const temporary = path.join(outputRoot, entry.name)
    const finalPath = path.join(outputRoot, entry.name.slice(0, -4))
    if (fs.existsSync(finalPath)) continue
    let renamed = false
    for (let attempt = 0; attempt < 5 && !renamed; attempt += 1) {
      try {
        fs.renameSync(temporary, finalPath)
        renamed = true
      } catch (error) {
        if (process.platform === 'win32' && attempt === 4) {
          const command = `Move-Item -LiteralPath '${temporary.replace(/'/g, "''")}' -Destination '${finalPath.replace(/'/g, "''")}'`
          const fallback = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command], {
            stdio: 'inherit',
            windowsHide: true,
          })
          renamed = fallback.status === 0
        }
        if (!renamed) {
          const delayUntil = Date.now() + 500
          while (Date.now() < delayUntil) {}
        }
      }
    }
    if (!renamed) throw new Error(`无法恢复临时输出目录: ${temporary}`)
    console.log(`已恢复 Electron 临时输出目录: ${finalPath}`)
    recovered = true
  }
  return recovered
}

child.on('error', (error) => {
  console.error(error)
  process.exitCode = 1
})

child.on('exit', (code, signal) => {
  if (code === 0) {
    process.exitCode = 0
    return
  }
  try {
    process.exitCode = recoverTemporaryUnpackedDirectories() ? 0 : code || 1
  } catch (error) {
    console.error(`恢复 Electron 临时目录失败: ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = code || 1
  }
  if (signal) console.error(`electron-builder 已被信号终止: ${signal}`)
})
