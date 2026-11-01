<script setup>
// 《支付服务协议》卡：首次付款前被拦下时出，或用户说「查看支付服务协议」时出（action_id 为空）。
// 卡面列要点，「查看完整条款」展开全文。同意后回到对话，后端记录同意并接着出被拦那笔的确认卡；
// 不同意则那笔动作取消。已签署时只读展示。
import { ref } from 'vue'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])

const full = ref(false)
const done = ref('')

function pick(action, label, detail, mask) {
  if (done.value) return
  done.value = mask
  const selection = { type: 'payment_agreement', action, action_id: props.card.action_id ?? null }
  if (action === 'sign') selection.version = props.card.version
  emit('select', { label, detail, selection })
}
const sign = () => pick('sign', '同意《支付服务协议》', props.card.version, '已同意，请在下方核对交易')
const decline = () => pick('decline', '暂不同意《支付服务协议》', props.card.pending_summary ? '这笔交易不办理' : props.card.version,
  '未同意，付款类业务暂不办理')
</script>

<template>
  <div class="card agreement" :class="{ closed: done }" data-testid="payment-agreement-card">
    <div class="card-title">
      <span>《{{ card.title || '支付服务协议' }}》</span>
      <van-tag v-if="card.status === 'signed'" plain type="success">已签署</van-tag>
      <van-tag v-else plain type="warning">待同意</van-tag>
    </div>
    <div v-if="card.pending_summary && card.status !== 'signed'" class="pending">
      <span class="k">待办理</span><span>{{ card.pending_summary }}</span>
    </div>
    <div v-if="card.intro" class="intro">{{ card.intro }}</div>
    <div class="points">
      <div v-for="p in card.points" :key="p.title" class="point">
        <span class="p-icon"><IconText :text="p.icon || '·'" /></span>
        <div class="p-body"><div class="p-title">{{ p.title }}</div><div class="p-text">{{ p.text }}</div></div>
      </div>
    </div>
    <div v-if="card.sections?.length" class="toggle" data-testid="agreement-full-toggle" @click="full = !full">
      {{ full ? '收起完整条款' : '查看完整条款' }} <van-icon :name="full ? 'arrow-up' : 'arrow-down'" />
    </div>
    <div v-if="full" class="sections" data-testid="agreement-sections">
      <div v-for="s in card.sections" :key="s.title" class="section">
        <div class="s-title">{{ s.title }}</div>
        <div class="s-body">{{ s.body }}</div>
      </div>
    </div>
    <div v-if="card.status === 'signed'" class="done-mask signed">已于 {{ card.signed_at }} 签署 · 版本 {{ card.version }}</div>
    <div v-else-if="done" class="done-mask">{{ done }}</div>
    <template v-else>
      <div class="btns">
        <van-button size="small" data-testid="agreement-decline" @click="decline">暂不同意</van-button>
        <van-button size="small" type="danger" data-testid="agreement-sign" @click="sign">我已阅读并同意</van-button>
      </div>
      <div class="meta">版本 {{ card.version }} · 同意后可在「我的支付授权」里随时查看</div>
    </template>
  </div>
</template>

<style scoped>
.pending { display: flex; gap: var(--space-2); font-size: var(--fs-sm); line-height: 1.5; background: var(--yellow-soft); color: var(--yellow-ink);
  border-radius: var(--radius-sm); padding: 6px 10px; margin-bottom: 8px; }
.pending .k { flex-shrink: 0; font-weight: 600; }
.intro { font-size: var(--fs-sm); color: var(--ink-2); line-height: 1.6; }
.points { margin-top: 8px; }
.point { display: flex; gap: var(--space-2); padding: 7px 0; border-bottom: 1px solid var(--line-soft); }
.point:last-child { border-bottom: 0; }
.p-icon { flex-shrink: 0; width: 20px; font-size: var(--fs-body); line-height: 21px; text-align: center; }
.p-body { min-width: 0; }
.p-title { font-size: var(--fs-body); font-weight: 600; line-height: 21px; }
.p-text { font-size: var(--fs-sm); color: var(--muted); line-height: 1.55; }
.toggle { display: flex; align-items: center; gap: 2px; font-size: var(--fs-sm); color: var(--info); padding: 8px 0 0; cursor: pointer; }
.sections { margin-top: 6px; max-height: 280px; overflow-y: auto; overscroll-behavior: contain; background: var(--surface-2);
  border-radius: var(--radius-sm); padding: 8px 10px; }
.section + .section { margin-top: 10px; }
.s-title { font-size: var(--fs-sm); font-weight: 600; }
.s-body { font-size: var(--fs-xs); color: var(--ink-2); line-height: 1.7; white-space: pre-wrap; margin-top: 2px; }
.done-mask.signed { color: var(--green-ink); }
</style>
