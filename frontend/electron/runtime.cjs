const fs = require('fs')
const path = require('path')

function ensureDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true })
  return directory
}

function runtimePaths(root) {
  const runtimeRoot = path.resolve(root)
  return {
    runtimeRoot,
    data: path.join(runtimeRoot, 'data'),
    cache: path.join(runtimeRoot, 'cache'),
    tmp: path.join(runtimeRoot, 'tmp'),
    log: path.join(runtimeRoot, 'log'),
  }
}

function ensureRuntimeRoot(root, resourcesRoot) {
  const paths = runtimePaths(root)
  if (isSameOrChild(paths.runtimeRoot, resourcesRoot) || isSameOrChild(resourcesRoot, paths.runtimeRoot)) {
    throw new Error(`运行根不能与只读资源目录重叠: ${paths.runtimeRoot}`)
  }
  for (const directory of [paths.data, paths.cache, paths.tmp, paths.log]) {
    ensureDirectory(directory)
    const probe = path.join(directory, '.mailin-write-probe')
    fs.writeFileSync(probe, 'ok')
    fs.rmSync(probe)
  }
  return paths
}

function isSameOrChild(candidate, root) {
  const candidatePath = path.resolve(candidate)
  const rootPath = path.resolve(root)
  return candidatePath === rootPath || candidatePath.startsWith(`${rootPath}${path.sep}`)
}

function clearDirectoryContents(directory, protectedPaths = []) {
  const directoryPath = path.resolve(directory)
  for (const protectedPath of protectedPaths) {
    if (isSameOrChild(directoryPath, protectedPath) || isSameOrChild(protectedPath, directoryPath)) {
      throw new Error('拒绝清理包含受保护路径的目录')
    }
  }
  if (!fs.existsSync(directoryPath)) return
  for (const entry of fs.readdirSync(directoryPath)) {
    fs.rmSync(path.join(directoryPath, entry), { recursive: true, force: true })
  }
}

module.exports = {
  clearDirectoryContents,
  ensureDirectory,
  isSameOrChild,
  runtimePaths,
  ensureRuntimeRoot,
}
