const fs = require('fs')
const path = require('path')
const { spawnSync } = require('child_process')

const frontendRoot = path.resolve(__dirname, '..')
const repoRoot = path.resolve(frontendRoot, '..')
const python = path.join(repoRoot, 'backend', '.venv', 'Scripts', 'python.exe')
const spec = path.join(frontendRoot, 'mailin-backend.spec')
const distRoot = path.join(repoRoot, 'backend', 'dist', 'mailin-backend')
const executable = path.join(
  distRoot,
  process.platform === 'win32' ? 'mailin-backend.exe' : 'mailin-backend',
)

for (const required of [python, spec]) {
  if (!fs.existsSync(required)) {
    throw new Error(`缺少后端构建输入: ${required}`)
  }
}

const result = spawnSync(
  python,
  [
    '-s',
    '-m',
    'PyInstaller',
    '--noconfirm',
    '--clean',
    '--distpath', path.join(repoRoot, 'backend', 'dist'),
    '--workpath', path.join(frontendRoot, 'build'),
    spec,
  ],
  {
    cwd: frontendRoot,
  env: {
    ...process.env,
    PYTHONNOUSERSITE: '1',
    PYTHONUSERBASE: path.join(repoRoot, '.py-userbase'),
  },
    stdio: 'inherit',
  },
)

if (result.error) throw result.error
if (result.status !== 0) process.exit(result.status || 1)

const requiredResources = [
  path.join(repoRoot, 'backend', 'resources', 'defaults', 'workspace', 'bootstraps', 'SOUL.md'),
  path.join(repoRoot, 'backend', 'resources', 'defaults', 'config', 'config.toml'),
]

for (const resource of requiredResources) {
  if (!fs.existsSync(resource)) throw new Error(`缺少发布资源源文件: ${resource}`)
}
if (!fs.existsSync(executable)) throw new Error(`后端产物不存在: ${executable}`)

console.log(`后端 onedir 产物已就绪: ${distRoot}`)
