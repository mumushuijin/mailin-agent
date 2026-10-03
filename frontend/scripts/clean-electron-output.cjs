const fs = require('fs')
const path = require('path')

const frontendRoot = path.resolve(__dirname, '..')
const generatedOutputs = [
  path.join(frontendRoot, 'release'),
  path.join(frontendRoot, 'release-check'),
]

for (const output of generatedOutputs) {
  if (!fs.existsSync(output)) continue
  fs.rmSync(output, { recursive: true, force: true })
  console.log(`已清理 Electron 输出目录: ${output}`)
}
