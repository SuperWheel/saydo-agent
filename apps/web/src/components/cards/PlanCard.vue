<script setup>
// 计划卡（WS7）：多步任务的 DAG 和每步状态。确认计划只是同意按这个顺序办，
// 每一笔存入、下单、转账仍会单独出确认卡。按钮通过 select 回传给对话（selection.type = plan）。
import { computed, ref } from 'vue'
import { showToast } from 'vant'
import { api } from '../../api'
import { icon } from '../../icons'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select', 'outcome'])
const busy = ref(false)

const STATUS_TAG = { draft: 'primary', running: 'warning', waiting: 'primary', rolling_back: 'warning',
  awaiting_funds: 'warning', done: 'success', cancelled: 'default', expired: 'default', handoff: 'danger' }
const ICON = { pending: '○', awaiting: '◔', awaiting_funds: '¥', expired: '×', done: '✓', failed: '!', cancelled: '–', removed: '✕' }
const OPS = {
  confirm: { label: '确认计划', type: 'danger', detail: '按步骤办理，每笔资金操作仍单独确认' },
  advance: { label: '继续', type: 'primary', detail: '推进下一步' },
  cancel: { label: '取消计划', type: 'default', detail: '已办的步骤会按相反顺序回退' },
  handoff: { label: '转人工', type: 'default', detail: '坐席接手，办理暂停' },
  resume: { label: '补款后继续', type: 'danger', detail: '重新核对余额后按原授权下单' },
}
const ops = computed(() => props.card.ops.filter((o) => OPS[o]))
const live = computed(() => ['draft', 'running', 'waiting', 'awaiting_funds', 'rolling_back'].includes(props.card.status))

function lockBudget() { // 活期付款的计划：先把预算存进心愿罐，到期从罐里付，不怕那天钱不够
  const b = props.card.budget_lock
  emit('select', { label: `先存 ¥${b.amount} 进心愿罐`, detail: `用途「${b.purpose}」，计划改为从心愿罐付款`,
    selection: { type: 'plan', plan_id: props.card.plan_id, op: 'lock_budget' } })
}

function act(op) {
  emit('select', { label: OPS[op].label, detail: OPS[op].detail,
    selection: { type: 'plan', plan_id: props.card.plan_id, op } })
}

async function fastForward() { // 演示：快进到临期时间，触发自动询价
  busy.value = true
  try {
    const r = await api.fastForward(props.card.due_at)
    if (!r.events.length) showToast('没有到期的任务')
    for (const e of r.events) emit('outcome', { message: e.text, cards: e.cards, trace: e.trace_id })
  } catch (e) {
    showToast(e.message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card plan" data-testid="plan-card">
    <div class="card-title">
      <span><IconText :text="`📋 ${card.goal}`" /></span>
      <van-tag :type="STATUS_TAG[card.status]" size="medium">{{ card.status_label }}</van-tag>
    </div>
    <div v-if="card.event_date" class="row"><span class="k">日期</span><span class="v">{{ card.event_date }}</span></div>
    <div v-if="card.note" class="note">{{ card.note }}</div>
    <ol class="steps">
      <li v-for="s in card.steps" :key="s.id" :class="s.status">
        <span class="icon">{{ ICON[s.status] }}</span>
        <div class="body">
          <div class="line">
            <span class="title">{{ s.title }}</span>
            <span class="risk" :class="s.kind"><IconText :text="s.kind === 'compensate' ? '↩ 回退' : s.risk" /></span>
          </div>
          <div class="sub">
            <span>{{ s.status_label }}</span>
            <span v-if="s.at_label"> · {{ s.at_label }}</span>
            <span v-if="s.detail"> · {{ s.detail }}</span>
          </div>
        </div>
      </li>
    </ol>
    <div class="hint">{{ card.hint }}</div>
    <div v-if="live && card.budget_lock && card.ops.includes('lock_budget')" class="lock" data-testid="plan-lock-budget">
      <div class="lock-text">现在从活期付款，到期那天余额不够就付不了。可以先把 ¥{{ card.budget_lock.amount }} 存进心愿罐专款专用。</div>
      <van-button size="small" plain type="danger" block @click="lockBudget">先存入心愿罐锁定预算</van-button>
    </div>
    <div v-if="live && (ops.length || card.ops.includes('fast_forward'))" class="btns">
      <van-button v-for="o in ops" :key="o" size="small" :type="OPS[o].type" :data-testid="`plan-op-${o}`" @click="act(o)">{{ OPS[o].label }}</van-button>
    </div>
    <van-button v-if="live && card.ops.includes('fast_forward')" class="ff" size="small" plain block :icon="icon('fast-forward', 'clock-o')"
                :loading="busy" @click="fastForward">快进到 {{ card.due_label }}（演示）</van-button>
    <div class="meta">计划 {{ card.plan_id }} · 第 {{ card.version }} 版 · {{ card.source === 'llm' ? '模型规划，已通过校验' : '规则模板' }}</div>
  </div>
</template>

<style scoped>
.note { font-size: var(--fs-sm); background: var(--brand-soft); color: var(--brand); border-radius: var(--radius-sm); padding: 6px 10px; margin: 4px 0 6px; }
.steps { list-style: none; padding: 0; margin: 6px 0 0; }
.steps li { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line-soft); }
.steps li:last-child { border-bottom: none; }
.icon { flex-shrink: 0; width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
  font-size: var(--fs-xs); font-weight: 700; color: var(--muted); background: var(--surface-2); }
.done .icon { color: var(--green-ink); background: var(--green-soft); }
.awaiting .icon, .failed .icon, .awaiting_funds .icon { color: var(--yellow-ink); background: var(--yellow-soft); }
.body { flex: 1; min-width: 0; }
.line { display: flex; justify-content: space-between; gap: 8px; font-size: var(--fs-body); line-height: 22px; }
.title { word-break: break-all; }
.removed .title, .cancelled .title, .expired .title { text-decoration: line-through; color: var(--muted); }
.risk { flex-shrink: 0; font-size: var(--fs-xs); color: var(--yellow-ink); }
.risk.read { color: var(--green-ink); }
.risk.compensate { color: var(--brand); }
.sub { font-size: var(--fs-xs); color: var(--muted); margin-top: 2px; line-height: 1.5; }
.hint { font-size: var(--fs-xs); color: var(--muted); background: var(--surface-2); border-radius: var(--radius-sm); padding: 6px 10px; margin-top: 8px; }
.ff { margin-top: 8px; }
.lock { margin-top: 8px; padding: 8px 10px; border-radius: var(--radius-sm); background: var(--yellow-soft); }
.lock-text { font-size: var(--fs-xs); color: var(--yellow-ink); line-height: 1.5; margin-bottom: 6px; }
</style>
