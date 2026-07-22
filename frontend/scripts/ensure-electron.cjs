const fs = require('fs')
const path = require('path')
const { execSync } = require('child_process')

const electronDir = path.join(__dirname, '../node_modules/electron')
const pathFile = path.join(electronDir, 'path.txt')
const installScript = path.join(electronDir, 'install.js')

if (!fs.existsSync(installScript)) {
  process.exit(0)
}

if (fs.existsSync(pathFile)) {
  const exe = fs.readFileSync(pathFile, 'utf-8').trim()
  const distPath = path.join(electronDir, 'dist', exe)
  if (fs.existsSync(distPath)) {
    process.exit(0)
  }
}

console.log('[mailin] Electron 二进制缺失，正在下载...')

const env = {
  ...process.env,
  ELECTRON_MIRROR: process.env.ELECTRON_MIRROR || 'https://npmmirror.com/mirrors/electron/',
}

execSync('node install.js', { cwd: electronDir, stdio: 'inherit', env })
console.log('[mailin] Electron 安装完成')
