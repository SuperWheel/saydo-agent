<script setup>
// 进入前人脸核身（模拟）：打开 App 先过这一页，后端签发 30 分钟有效的进入凭证后才进入聊天页。
// 演示环境不调用摄像头：取景框和扫描动画只是示意，「识别」由后端核对固定的模拟活体断言（auth/entry.py）。
import { computed, onUnmounted, ref } from 'vue'
import { showToast } from 'vant'
import { api, entryPass } from '../api'
import { iconUrl } from '../icons'

const props = defineProps({
  notice: { type: String, default: '' }, // 回到这一页的原因：过期、重置演示数据
  handoff: { type: Boolean, default: false }, // 从企业微信引导链接打开
})
const emit = defineEmits(['passed'])

const STEPS = ['请正对取景框', '请缓慢眨眼', '正在比对…']
const state = ref('idle') // idle → scanning → passed
const step = ref(0)
let timer

const hint = computed(() => {
  if (state.value === 'passed') return '验证通过'
  if (state.value === 'scanning') return STEPS[step.value]
  return '准备好后点「开始验证」'
})

const wait = (ms) => new Promise((r) => { timer = setTimeout(r, ms) })
onUnmounted(() => clearTimeout(timer))

async function start() {
  if (state.value !== 'idle') return
  state.value = 'scanning'
  step.value = 0
  try {
    for (let i = 1; i < STEPS.length; i++) { await wait(550); step.value = i }
    const out = await api.entryFace()
    entryPass.set(out.token, out.ttl_s)
    state.value = 'passed'
    await wait(500)
    emit('passed')
  } catch (e) {
    state.value = 'idle'
    showToast(e.message || '验证未通过，请重试')
  }
}
</script>

<template>
  <div class="entry" data-testid="entry-face">
    <header class="brand">
      <div class="name">言行 <small>AI 银行助手</small></div>
      <van-tag type="danger" plain size="mini">演示环境 · 模拟数据</van-tag>
    </header>

    <main class="body">
      <h1 class="title">人脸验证</h1>
      <p class="desc">为保护你的账户安全，请先完成人脸验证<br>（演示环境为模拟）</p>
      <div v-if="notice" class="notice">{{ notice }}</div>

      <div class="frame" :class="state" aria-hidden="true">
        <i class="corner tl" /><i class="corner tr" /><i class="corner bl" /><i class="corner br" />
        <div class="lens">
          <svg class="face" viewBox="0 0 120 120">
            <ellipse cx="60" cy="54" rx="27" ry="33" />
            <path d="M18 120c4-24 21-36 42-36s38 12 42 36" />
          </svg>
          <div v-if="state === 'scanning'" class="scan" />
          <img v-if="state === 'passed'" class="ok" :src="iconUrl('shield-check')" alt="">
        </div>
      </div>
      <div class="hint" :class="state" role="status">{{ hint }}</div>

      <div v-if="handoff" class="handoff">来自企业微信的请求：验证后自动带入，核对后再发送</div>
    </main>

    <footer class="foot">
      <ul class="notes">
        <li>演示环境不调用摄像头，不采集、不上传任何图像</li>
        <li>验证通过后 30 分钟内无需重复验证，关闭页面需重验</li>
        <li>转账、锁卡等操作仍会按风险单独确认或强验证</li>
      </ul>
      <van-button block round type="danger"
                  :loading="state === 'scanning'" loading-text="正在验证…" :disabled="state === 'passed'"
                  @click="start">{{ state === 'passed' ? '验证通过' : '开始验证' }}</van-button>
    </footer>
  </div>
</template>

<style scoped>
.entry {
  flex: 1; min-height: 0; display: flex; flex-direction: column; background: var(--surface); overflow-y: auto;
}
.brand {
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
  padding: calc(14px + env(safe-area-inset-top)) var(--space-5) 0;
}
.name { font-size: 19px; font-weight: 700; color: var(--brand); line-height: 26px; letter-spacing: 0.5px; }
.name small { font-size: var(--fs-xs); color: var(--muted); font-weight: 400; margin-left: 6px; letter-spacing: 0; }
.body { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: var(--space-5) var(--space-5) 0; text-align: center; }
.title { margin: 0; font-size: 22px; font-weight: 700; color: var(--ink); }
.desc { margin: 10px 0 0; max-width: 300px; font-size: var(--fs-body); line-height: 1.6; color: var(--muted); }
.notice {
  margin-top: var(--space-3); padding: 6px var(--space-3); border-radius: var(--radius-pill);
  background: var(--yellow-soft); color: var(--yellow-ink); font-size: var(--fs-xs);
}

/* 取景框：四角括号 + 圆形镜头，扫描时一条光带上下扫 */
.frame { position: relative; width: 220px; height: 220px; margin-top: 28px; flex-shrink: 0; }
.corner { position: absolute; width: 38px; height: 38px; border: 4px solid var(--brand); transition: border-color .3s; }
.corner.tl { top: 0; left: 0; border-right: 0; border-bottom: 0; border-top-left-radius: 18px; }
.corner.tr { top: 0; right: 0; border-left: 0; border-bottom: 0; border-top-right-radius: 18px; }
.corner.bl { bottom: 0; left: 0; border-right: 0; border-top: 0; border-bottom-left-radius: 18px; }
.corner.br { bottom: 0; right: 0; border-left: 0; border-top: 0; border-bottom-right-radius: 18px; }
.frame.passed .corner { border-color: var(--green); }
.lens {
  position: absolute; inset: 22px; border-radius: 50%; overflow: hidden; background: var(--brand-soft);
  box-shadow: inset 0 0 0 1px rgba(200, 16, 46, 0.08); transition: background .3s;
}
.frame.scanning .lens { animation: breathe 1.6s ease-in-out infinite; }
.frame.passed .lens { background: var(--green-soft); }
.face { position: absolute; inset: 0; width: 100%; height: 100%; }
.face ellipse, .face path { fill: var(--surface); opacity: 0.9; }
.frame.passed .face { opacity: 0.35; }
.scan {
  position: absolute; left: 0; right: 0; height: 46px; top: -46px;
  background: linear-gradient(180deg, rgba(200, 16, 46, 0) 0%, rgba(200, 16, 46, 0.22) 85%, var(--brand) 100%);
  animation: scan 1.1s ease-in-out infinite alternate;
}
.ok { position: absolute; left: 50%; top: 50%; width: 76px; height: 76px; transform: translate(-50%, -50%); animation: pop .3s ease-out; }
@keyframes scan { from { top: -46px; } to { top: 100%; } }
@keyframes breathe { 50% { box-shadow: inset 0 0 0 6px rgba(200, 16, 46, 0.10); } }
@keyframes pop { from { transform: translate(-50%, -50%) scale(.6); opacity: 0; } }
@media (prefers-reduced-motion: reduce) {
  .scan, .frame.scanning .lens, .ok { animation: none; }
  .scan { top: 40%; }
}

.hint { margin-top: var(--space-4); font-size: var(--fs-title); font-weight: 600; color: var(--ink); min-height: 22px; }
.hint.idle { color: var(--muted); font-weight: 400; font-size: var(--fs-body); }
.hint.passed { color: var(--green-ink); }
.handoff {
  margin-top: var(--space-4); padding: 8px var(--space-3); border-radius: var(--radius-sm);
  background: var(--info-soft); color: var(--info); font-size: var(--fs-sm); line-height: 1.5;
}
.foot { padding: var(--space-5) var(--space-5) calc(var(--space-5) + env(safe-area-inset-bottom)); }
.notes {
  list-style: none; margin: 0 0 var(--space-4); padding: 10px var(--space-3); border-radius: var(--radius-sm);
  background: var(--surface-2); font-size: var(--fs-xs); line-height: 1.7; color: var(--muted); text-align: left;
}
.notes li { position: relative; padding-left: 12px; }
.notes li::before { content: ''; position: absolute; left: 2px; top: 9px; width: 4px; height: 4px; border-radius: 50%; background: var(--faint); }
.foot .van-button { height: 46px; font-size: var(--fs-title); font-weight: 600; }
</style>
