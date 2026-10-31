<script setup>
// 模拟人工客服（熔断提示卡上「联系人工客服」点开）：排队 → 客服接入 → 核对触发记录、确认是否本人 →（需要时）模拟人脸识别
// → 核实通过由「客服」解除（后端 security/support.py 调 breaker.release，审计记操作人和核实记录）；说不是本人就保持冻结，建议锁卡 / 挂失。
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../../api'

const props = defineProps({ tripId: { type: String, required: true } })
const emit = defineEmits(['released', 'say', 'close'])

const s = ref(null) // 核实脚本
const msgs = ref([])
const step = ref('queue') // queue / question / identity / face / busy / released / kept / retry / gone
const ahead = ref(0)
const result = ref(null)
const body = ref(null)
let timers = []
const wait = (ms) => new Promise((r) => timers.push(setTimeout(r, ms)))
onUnmounted(() => timers.forEach(clearTimeout))

async function scrollEnd() {
  await nextTick()
  body.value?.scrollTo({ top: body.value.scrollHeight, behavior: 'smooth' })
}

async function add(from, text, extra = {}) {
  msgs.value.push({ from, text, ...extra })
  await scrollEnd()
}

onMounted(async () => {
  try {
    s.value = await api.breakerSupport(props.tripId)
  } catch (e) {
    step.value = 'gone'
    add('agent', e.status === 409 ? e.message : `暂时无法接入人工客服：${e.message}`)
    return
  }
  ahead.value = s.value.queue_ahead
  while (ahead.value > 0) { await wait(450); ahead.value -= 1 }
  await add('system', `人工客服 ${s.value.agent} 已接入`)
  await add('agent', s.value.greeting)
  if (s.value.evidence.length) await add('agent', '触发这次安全保护的记录：', { evidence: s.value.evidence })
  await add('agent', s.value.question)
  step.value = 'question'
})

async function answer(yes) {
  await add('user', yes ? s.value.yes : s.value.no)
  if (!yes) return submit(false)
  if (!s.value.need_identity) return submit(true)
  await add('agent', s.value.identity_prompt)
  step.value = 'identity'
  scrollEnd()
}

async function face() {
  step.value = 'face'
  await wait(900) // 模拟比对
  await add('user', '已完成模拟人脸识别')
  submit(true, `face-ok:${props.tripId}`)
}

async function submit(selfOperated, identity = '') {
  step.value = 'busy'
  try {
    result.value = await api.breakerReview(props.tripId, { self_operated: selfOperated, identity })
  } catch (e) {
    step.value = 'gone'
    return add('agent', e.message)
  }
  await add('agent', result.value.message)
  step.value = result.value.status === 'released' ? 'released' : result.value.outcome === 'identity_failed' ? 'retry' : 'kept'
  scrollEnd() // 底部按钮换了，聊天区变矮，最后一条消息别被挡住
}
</script>

<template>
  <div class="sheet" data-testid="support-sheet">
    <div class="title">人工客服</div>
    <div class="banner">模拟人工客服 · 演示环境（不会真的转接坐席）</div>
    <div v-if="s" class="info">
      <div><span class="k">当前状态</span>{{ s.level_label }} · {{ s.reason }}</div>
      <div><span class="k">恢复方式</span>{{ s.recover }}</div>
      <div v-if="s.auto_recover_at" class="auto">不联系客服也可以：{{ s.auto_recover_at }} 自动恢复</div>
    </div>

    <div ref="body" class="chat">
      <div v-if="step === 'queue'" class="queue" data-testid="support-queue">
        <van-loading size="18" />
        <span>正在为你接入人工客服{{ ahead ? `，前面还有 ${ahead} 位` : '…' }}</span>
      </div>
      <template v-for="(m, i) in msgs" :key="i">
        <div v-if="m.from === 'system'" class="sys">{{ m.text }}</div>
        <div v-else class="msg" :class="m.from">
          <div v-if="m.from === 'agent'" class="who">{{ s?.agent || '客服' }} · 模拟</div>
          <div class="bubble">
            {{ m.text }}
            <div v-for="(e, j) in m.evidence || []" :key="j" class="ev"><span class="at">{{ e.at }}</span>{{ e.text }}</div>
          </div>
        </div>
      </template>
    </div>

    <div class="actions">
      <template v-if="step === 'question'">
        <van-button block round type="danger" data-testid="support-yes" @click="answer(true)">{{ s.yes }}</van-button>
        <van-button block round plain data-testid="support-no" @click="answer(false)">{{ s.no }}</van-button>
      </template>
      <van-button v-else-if="step === 'identity' || step === 'face' || step === 'retry'" block round type="danger"
                  icon="contact" :loading="step === 'face'" loading-text="人脸比对中…" data-testid="support-face"
                  @click="face">{{ step === 'retry' ? '重新验证' : '开始模拟人脸识别' }}</van-button>
      <van-loading v-else-if="step === 'busy'" size="20" class="center">客服核实中…</van-loading>
      <template v-else-if="step === 'released'">
        <div class="done" data-testid="support-released">✓ 安全保护已解除，可以继续办理</div>
        <van-button block round type="danger" data-testid="support-continue" @click="emit('released', result)">继续办理</van-button>
      </template>
      <template v-else-if="step === 'kept'">
        <div class="kept">安全保护保持开启，期间不会有资金转出</div>
        <van-button v-for="a in result.advice || []" :key="a.label" block round type="danger" plain
                    @click="emit('say', a.text)">{{ a.label }}</van-button>
        <van-button block round plain @click="emit('close')">关闭</van-button>
      </template>
      <van-button v-else-if="step === 'gone'" block round plain @click="emit('close')">关闭</van-button>
    </div>
    <div class="meta">核实记录会写入审计，运营后台「熔断与风险」可查</div>
  </div>
</template>

<style scoped>
.sheet { padding: 4px 16px 16px; display: flex; flex-direction: column; height: 100%; }
.title { font-size: 17px; font-weight: 600; padding-right: 32px; }
.banner { margin-top: 6px; font-size: 12px; padding: 4px 8px; border-radius: 6px; background: var(--yellow-soft); color: #874d00; }
.info { margin-top: 8px; font-size: 13px; line-height: 1.6; background: #f6f7fa; border-radius: 10px; padding: 8px 10px; }
.info .k { color: var(--muted); margin-right: 8px; }
.info .auto { color: var(--green); }
.chat { flex: 1; overflow-y: auto; padding: 8px 0; }
.queue { display: flex; align-items: center; gap: 8px; justify-content: center; color: var(--muted); font-size: 13px; padding: 24px 0; }
.sys { text-align: center; font-size: 12px; color: var(--muted); margin: 8px 0; }
.msg { display: flex; flex-direction: column; margin: 8px 0; }
.msg.user { align-items: flex-end; }
.who { font-size: 11px; color: var(--muted); margin-bottom: 2px; }
.bubble { max-width: 86%; padding: 8px 10px; border-radius: 10px; font-size: 14px; line-height: 1.55; background: #f2f3f5; }
.msg.user .bubble { background: var(--user-bubble); color: #fff; }
.ev { font-size: 12px; margin-top: 4px; padding-top: 4px; border-top: 1px dashed #dcdfe6; }
.ev .at { color: var(--muted); margin-right: 6px; font-variant-numeric: tabular-nums; }
.actions { display: flex; flex-direction: column; gap: 8px; padding-top: 8px; }
.center { justify-content: center; }
.done { text-align: center; color: var(--green); font-weight: 600; }
.kept { text-align: center; color: var(--red); font-size: 13px; }
.meta { font-size: 11px; color: var(--muted); margin-top: 8px; text-align: center; }
</style>
