// 演示主线用例的公共操作：重置数据、点快捷语 / 输入一句话、等这一轮回复完。
import { expect, test as base } from '@playwright/test'

export const test = base.extend({
  // 每个用例开头重置演示数据（余额、当日额度、熔断记录都回到种子状态），再打开首页
  page: async ({ page, request }, use) => {
    const r = await request.post('/dev/seed') // 走 Vite 代理到测试后端
    expect(r.ok()).toBeTruthy()
    await page.goto('/')
    await expect(page.getByText('你好，我是言行')).toBeVisible()
    await use(page)
  },
})
export { expect }

// 等这一轮回复完：等待气泡消失
export async function settled(page) {
  await expect(page.getByText(/正在理解你的意思|正在查询和核对数据|模型响应较慢/)).toHaveCount(0)
}

// 点输入框上方的快捷语
export async function chip(page, text) {
  await page.locator('.quick').getByText(text, { exact: true }).click()
  await settled(page)
}

// 手动输入一句话，回车发送
export async function say(page, text) {
  const input = page.getByPlaceholder(/说点什么/)
  await input.fill(text)
  await input.press('Enter')
  await settled(page)
}

export const cards = (page, id) => page.getByTestId(id)
