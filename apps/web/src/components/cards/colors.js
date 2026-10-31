// 消费分类的配色：环形图和分类详情共用，同一类在哪儿都是同一个颜色
export const CATEGORY_COLORS = {
  餐饮: '#f5803e', 购物: '#4e79e6', 交通: '#2bb3a3', 居住: '#8c6cd9', 生活缴费: '#e3b341', 医疗: '#e05d7a',
  教育: '#5fa8d3', 娱乐: '#d35fb7', 旅行: '#4caf7a', 订阅: '#c8102e', 其他: '#b7bfcc',
}
export const colorOf = (category) => CATEGORY_COLORS[category] || CATEGORY_COLORS.其他

// 环比的颜色：涨红、跌绿，±0.0% 和「不可比」不着色
export function trend(change) {
  if (!change || /^[+-]0\.0%$/.test(change)) return ''
  return change.startsWith('+') ? 'up' : change.startsWith('-') ? 'down' : ''
}
