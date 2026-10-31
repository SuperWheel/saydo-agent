import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = process.env.API_BASE || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    // 默认对局域网开放（0.0.0.0）：同一网络的手机能打开 App，企业微信里的链接也能点开；本机的 127.0.0.1:5173 照常可用。
    // 显式写 IPv4：Vite 默认的 localhost 在 macOS 上只绑 [::1]。WEB_HOST=127.0.0.1 时只给本机用（端到端测试就这样起）
    host: process.env.WEB_HOST || '0.0.0.0',
    port: 5173,
    proxy: { '/api': backend, '/admin': backend, '/dev': backend },
  },
})
