// 把 mermaid 图渲染成 PNG，给 export_docx.py 用（Word 里不认 mermaid 代码）。
// 用法：node scripts/render_mermaid.mjs <jobs.json>，jobs.json 是 [{"code": "...", "out": "/abs/path.png"}, ...]
// 浏览器用 apps/web 里端到端测试装好的 Playwright Chromium；mermaid 从 jsDelivr 加载（需要联网）。
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const { chromium } = createRequire(join(root, 'apps/web/package.json'))('playwright')
const MERMAID = 'https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js'

const jobs = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const browser = await chromium.launch()
const page = await browser.newPage({ deviceScaleFactor: 2, viewport: { width: 1100, height: 800 } })
await page.setContent('<!doctype html><meta charset="utf-8"><body style="margin:0;background:#fff"><div id="box" style="display:inline-block;padding:16px"></div></body>')
await page.addScriptTag({ url: MERMAID })
await page.evaluate(() => window.mermaid.initialize({
  startOnLoad: false, theme: 'default', securityLevel: 'strict',
  fontFamily: '"PingFang SC", "Microsoft YaHei", sans-serif',
  flowchart: { htmlLabels: true }, sequence: { useMaxWidth: false },
}))

let failed = 0
for (const [i, job] of jobs.entries()) {
  try {
    await page.evaluate(async ({ code, id }) => {
      const { svg } = await window.mermaid.render(id, code)
      const box = document.getElementById('box')
      box.innerHTML = svg
      const el = box.querySelector('svg')
      // 按图本身的尺寸出图（流程图默认 width=100%，放在自适应宽度的容器里会缩成 300px 左右）；Word 里再按页宽缩放
      const { width, height } = el.viewBox.baseVal
      el.setAttribute('width', width)
      el.setAttribute('height', height)
      el.style.maxWidth = 'none'
    }, { code: job.code, id: `m${i}` })
    await page.locator('#box').screenshot({ path: job.out })
  } catch (e) {
    failed += 1
    console.error(`第 ${i + 1} 张图渲染失败（${job.out}）：${e.message.split('\n')[0]}`)
  }
}
await browser.close()
console.log(`mermaid：${jobs.length - failed} / ${jobs.length} 张`)
process.exit(failed ? 1 : 0)
