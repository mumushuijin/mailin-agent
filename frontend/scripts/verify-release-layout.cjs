const fs = require('fs')
const path = require('path')

const frontendRoot = path.resolve(__dirname, '..')
const repoRoot = path.resolve(frontendRoot, '..')
const backendDist = path.join(repoRoot, 'backend', 'dist', 'mailin-backend')
const sourceResources = [
  path.join(repoRoot, 'backend', 'resources', 'defaults', 'workspace'),
  path.join(repoRoot, 'backend', 'resources', 'defaults', 'config'),
]

if (!fs.existsSync(backendDist)) throw new Error(`缺少后端 onedir 目录: ${backendDist}`)
for (const resource of sourceResources) {
  if (!fs.existsSync(resource)) throw new Error(`缺少发布资源目录: ${resource}`)
}
if (!fs.existsSync(path.join(sourceResources[1], 'config.toml'))) {
  throw new Error('缺少只读默认配置 config.toml')
}

const releaseRoot = process.env.MAILIN_RELEASE_ROOT
  ? path.resolve(process.env.MAILIN_RELEASE_ROOT)
  : path.join(frontendRoot, 'release')
const resourceCandidates = []
if (fs.existsSync(releaseRoot)) {
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const fullPath = path.join(directory, entry.name)
      if (entry.isDirectory()) {
        if (entry.name.endsWith('.tmp')) continue
        if (entry.name === 'resources') resourceCandidates.push(fullPath)
        walk(fullPath)
      }
    }
  }
  walk(releaseRoot)
}

if (resourceCandidates.length === 0) throw new Error(`发布目录中没有 resources: ${releaseRoot}`)

for (const resources of resourceCandidates) {
  for (const required of ['backend', path.join('defaults', 'workspace'), path.join('defaults', 'config', 'config.toml')]) {
    if (!fs.existsSync(path.join(resources, required))) {
      throw new Error(`发布包缺少资源: ${path.join(resources, required)}`)
    }
  }
  for (const forbidden of ['data', 'cache', 'tmp', 'log', 'workspace_defaults', path.join('app', 'config', 'defaults'), 'CONFIG.json']) {
    if (fs.existsSync(path.join(resources, forbidden))) {
      throw new Error(`用户运行数据进入发布资源目录: ${path.join(resources, forbidden)}`)
    }
  }
  for (const forbidden of [
    path.join('defaults', 'workspace', 'CONFIG.json'),
    path.join('backend', '_internal', 'app', 'config', 'defaults'),
    path.join('backend', '_internal', 'app', 'config', 'CONFIG.json'),
  ]) {
    if (fs.existsSync(path.join(resources, forbidden))) throw new Error(`发现旧配置资源: ${path.join(resources, forbidden)}`)
  }
}

console.log(
  `发布包资源布局校验通过: ${resourceCandidates.length} 个 resources 目录`,
)
