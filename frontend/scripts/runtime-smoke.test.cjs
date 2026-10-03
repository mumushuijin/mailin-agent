const assert = require('node:assert/strict')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { after, test } = require('node:test')
const { spawn } = require('child_process')
const { clearDirectoryContents, ensureDirectory, ensureRuntimeRoot, runtimePaths } = require('../electron/runtime.cjs')

const repoRoot = path.resolve(__dirname, '..', '..')
const backendRoot = path.join(repoRoot, 'backend')
const python = path.join(backendRoot, '.venv', 'Scripts', 'python.exe')
const packagedBackend = process.env.MAILIN_BACKEND_EXECUTABLE || null
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'mailin-runtime-smoke-'))

function copyReleaseResources(resourcesRoot) {
  fs.cpSync(path.join(backendRoot, 'resources', 'defaults'), path.join(resourcesRoot, 'defaults'), { recursive: true })
}

function resourceSnapshot(root) {
  const output = []
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const full = path.join(directory, entry.name)
      if (entry.isDirectory()) walk(full)
      else output.push([path.relative(root, full), fs.readFileSync(full).toString('base64')])
    }
  }
  walk(root)
  return output.sort((a, b) => a[0].localeCompare(b[0]))
}

function freePort() {
  return new Promise((resolve, reject) => {
    const net = require('net')
    const server = net.createServer()
    server.unref()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = address && typeof address === 'object' ? address.port : null
      server.close(() => (port ? resolve(port) : reject(new Error('无法获取测试端口'))))
    })
  })
}

async function waitForHealth(endpoint, child) {
  const deadline = Date.now() + 20_000
  let lastError = null
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${endpoint}/health`)
      if (response.ok) return
      lastError = new Error(`health status ${response.status}`)
    } catch (error) {
      lastError = error
    }
    if (child.exitCode !== null) break
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`后端未就绪 (${child.exitCode ?? 'running'}): ${child.diagnostic || lastError?.message || 'unknown'}`)
}

async function startBackend({ runtimeRoot, resourcesRoot }) {
  const port = await freePort()
  const child = spawn(
    packagedBackend || python,
    [
      ...(packagedBackend ? ['serve'] : ['-m', 'cli.__main__', 'serve']),
      '--mode', 'production',
      '--host', '127.0.0.1',
      '--port', String(port),
      '--runtime-root', runtimeRoot,
      '--resources-dir', resourcesRoot,
    ],
    { cwd: packagedBackend ? path.dirname(packagedBackend) : backendRoot, env: { ...process.env, PYTHONNOUSERSITE: '1' }, stdio: ['ignore', 'pipe', 'pipe'] },
  )
  child.diagnostic = ''
  for (const stream of [child.stdout, child.stderr]) {
    stream.on('data', (chunk) => { child.diagnostic = `${child.diagnostic}${String(chunk)}`.slice(-4000) })
  }
  const endpoint = `http://127.0.0.1:${port}`
  await waitForHealth(endpoint, child)
  return { child, endpoint }
}

function stopBackend(child) {
  return new Promise((resolve) => {
    if (child.exitCode !== null) return resolve()
    child.once('exit', resolve)
    child.kill()
  })
}

after(() => fs.rmSync(tempRoot, { recursive: true, force: true }))

test('runtime smoke preserves user data across restart and keeps it outside resources', async () => {
  assert.ok(fs.existsSync(packagedBackend || python), `缺少测试后端: ${packagedBackend || python}`)
  const installRoot = path.join(tempRoot, 'install')
  const resourcesRoot = path.join(installRoot, 'resources')
  const runtimeRoot = path.join(tempRoot, 'runtime')
  const paths = runtimePaths(runtimeRoot)
  copyReleaseResources(resourcesRoot)
  const resourcesBefore = resourceSnapshot(resourcesRoot)

  const first = await startBackend({ runtimeRoot, resourcesRoot })
  const configResponse = await fetch(`${first.endpoint}/api/config/modules`)
  assert.equal(configResponse.status, 200)
  const project = path.join(tempRoot, 'project')
  ensureDirectory(project)
  const createResponse = await fetch(`${first.endpoint}/api/session/create`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ workspace_path: project }),
  })
  assert.equal(createResponse.status, 200)
  const customConfig = path.join(paths.data, 'config', 'custom.json')
  ensureDirectory(path.dirname(customConfig))
  fs.writeFileSync(customConfig, '{"upgradePreserved":true}\n', 'utf8')
  await stopBackend(first.child)

  const second = await startBackend({ runtimeRoot, resourcesRoot })
  assert.equal(JSON.parse(fs.readFileSync(customConfig, 'utf8')).upgradePreserved, true)
  assert.equal(fs.existsSync(path.join(resourcesRoot, 'agent-home')), false)
  assert.deepEqual(fs.readdirSync(runtimeRoot).sort(), ['cache', 'data', 'log', 'tmp'])
  assert.equal(fs.existsSync(path.join(resourcesRoot, 'log')), false)
  assert.equal(fs.existsSync(path.join(resourcesRoot, 'data')), false)
  assert.equal(fs.existsSync(path.join(paths.data, 'agent-home', 'sessions', 'index.json')), true)
  assert.deepEqual(resourceSnapshot(resourcesRoot), resourcesBefore)
  await stopBackend(second.child)
})

test('runtime root derives four directories and cache cleanup does not touch business data', () => {
  const source = path.join(tempRoot, 'legacy')
  const target = path.join(tempRoot, 'migrated')
  const cache = path.join(tempRoot, 'cache-check')
  ensureDirectory(source)
  ensureDirectory(target)
  ensureDirectory(cache)
  fs.writeFileSync(path.join(source, 'old.txt'), 'legacy', 'utf8')
  fs.writeFileSync(path.join(target, 'new.txt'), 'new', 'utf8')
  fs.writeFileSync(path.join(cache, 'stale.tmp'), 'cache', 'utf8')

  const paths = ensureRuntimeRoot(path.join(tempRoot, 'derived'), path.join(tempRoot, 'resources'))
  assert.deepEqual(fs.readdirSync(paths.runtimeRoot).sort(), ['cache', 'data', 'log', 'tmp'])
  clearDirectoryContents(cache, [source, target])
  assert.equal(fs.existsSync(path.join(cache, 'stale.tmp')), false)
  assert.equal(fs.existsSync(path.join(target, 'new.txt')), true)
})
