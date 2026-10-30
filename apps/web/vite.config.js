import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = process.env.API_BASE || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    // 显式监听 IPv4：Vite 默认的 localhost 在 macOS 上只绑 [::1]，文档里的 127.0.0.1:5173 会打不开
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': backend, '/admin': backend, '/dev': backend },
  },
})
