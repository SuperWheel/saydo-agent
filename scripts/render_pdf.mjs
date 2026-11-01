// 把 export_docx.py 生成的 HTML 打印成 A4 PDF（赛方提交页能直接预览 PDF）。
// 用法：node scripts/render_pdf.mjs <in.html> <out.pdf> <页眉文字>；浏览器用 apps/web 里端到端测试装好的 Playwright Chromium。
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const { chromium } = createRequire(join(root, 'apps/web/package.json'))('playwright')
const [input, output, title] = process.argv.slice(2)

const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto(pathToFileURL(input).href, { waitUntil: 'load' })
// 模板里的 style 用双引号包，字体名只能用单引号
const small = "font-size:8px;color:#8a8f98;width:100%;padding:0 18mm;font-family:'PingFang SC','Microsoft YaHei',sans-serif"
await page.pdf({
  path: output, format: 'A4', printBackground: true,
  margin: { top: '18mm', bottom: '18mm', left: '18mm', right: '18mm' },
  displayHeaderFooter: true,
  headerTemplate: `<div style="${small};text-align:right">${title}</div>`,
  footerTemplate: `<div style="${small};text-align:center"><span class="pageNumber"></span> / <span class="totalPages"></span></div>`,
})
await browser.close()
