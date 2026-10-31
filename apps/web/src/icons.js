// 定制图标（assets/icons/*.png）。按名字取地址；文件还没有时返回空串，调用方退回 emoji 或 Vant 图标
const files = import.meta.glob('./assets/icons/*.png', { eager: true, import: 'default' })

export const iconUrl = (name) => files[`./assets/icons/${name}.png`] || ''

// 给 van-icon / van-button 的 icon 用：有定制图标用图片地址，没有就用 Vant 自带图标名
export const icon = (name, fallback) => iconUrl(name) || fallback
