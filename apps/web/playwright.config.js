// 端到端测试（演示主线）：只跑规则模式，不调模型、不花 API 费用。
// 自己起一套前后端（8765 / 5174），不碰 make dev 的 8000 / 5173，也不读仓库根目录的 .env。
import { mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { defineConfig, devices } from '@playwright/test'

const API_PORT = 8765
const WEB_PORT = 5174
const tmp = join(tmpdir(), 'saydo-e2e')
mkdirSync(tmp, { recursive: true })
const envFile = join(tmp, 'empty.env') // 空配置文件：SAYDO_ENV_FILE 指到这里，真实 .env 不会被读到
writeFileSync(envFile, '')

const API_BASE = `http://127.0.0.1:${API_PORT}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1, // 前后端只有一套，每个用例开头都重置演示数据，不能并行
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    viewport: { width: 430, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{
    name: 'chromium',
    // E2E_CHROME=1 改用本机安装的 Google Chrome（Playwright 自带的 Chromium 下载不下来时）
    use: { ...devices['Desktop Chrome'], viewport: { width: 430, height: 900 }, ...(process.env.E2E_CHROME ? { channel: 'chrome' } : {}) },
  }],
  webServer: [
    {
      command: `uv run uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: '../api',
      url: `${API_BASE}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        SAYDO_ENV_FILE: envFile,
        DATABASE_URL: `sqlite:///${join(tmp, 'e2e.db')}`,
        LLM_ENABLED: 'false',
        LLM_API_KEY: '',
        DEEPSEEK_API_KEY: '',
        ZHIPUAI_API_KEY: '',
        GLM_API_KEY: '',
        ADMIN_PASSWORD: '',
        DEMO_MODE: 'true',
      },
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { API_BASE },
    },
  ],
})
