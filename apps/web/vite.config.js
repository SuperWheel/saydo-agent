import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = process.env.API_BASE || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    // 显式监听 IPv4：Vite 默认的 localhost 在 macOS 上只绑 [::1]，文档里的 127.0.0.1:5173 会打不开。
    // WEB_HOST=0.0.0.0（make dev-lan）时对局域网开放：手机连电脑热点后，点企业微信里的 App 链接能打开
    host: process.env.WEB_HOST || '127.0.0.1',
    port: 5173,
    proxy: { '/api': backend, '/admin': backend, '/dev': backend },
  },
})
