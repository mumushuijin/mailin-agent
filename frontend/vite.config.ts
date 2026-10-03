import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'
import { defineConfig } from 'vitest/config'

const isElectron = process.env.ELECTRON === 'true'
const enableVueDevTools = process.env.VUE_DEVTOOLS === '1'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    // 默认关闭；需要时设 VUE_DEVTOOLS=1
    ...(enableVueDevTools ? [vueDevTools()] : []),
  ],
  base: isElectron ? './' : '/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    open: false,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.{test,spec}.{js,ts}'],
  },
})
