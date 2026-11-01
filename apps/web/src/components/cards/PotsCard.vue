<script setup>
// 心愿罐管理（说「我的心愿罐」）：每个罐的余额、到期日、已支出和待执行计划，顶部是活期余额。
// 转入 / 转出 / 新建点开小弹窗填金额，转成 selection（pot_manage）回到对话出确认卡：
// 转入 🔴 人脸或短信任选其一，转出到活期 🟡 只需确认。「明细」直接打开收支明细弹层，不走对话。
// 金额上限只是前端先拦一道：转入不超过活期余额、转出不超过罐余额；被待执行计划预占的部分由后端拒绝并说明。
import { computed, ref } from 'vue'
import PotDetailSheet from './PotDetailSheet.vue'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])

const done = ref(false)
const pots = computed(() => props.card.pots || [])
const debit = computed(() => props.card.debit)
const fmtFen = (fen) => (fen / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const detail = ref(null) // 正在看明细的罐

function pick(selection, label, text) {
  if (done.value) return
  done.value = true
  emit('select', { label, detail: text, selection })
}

// —— 金额小弹窗：op 为 deposit / withdraw；pot 为空 = 新建心愿罐 ——
const edit = ref(null) // { op, pot }
const amount = ref('')
const purpose = ref('')
function openEdit(op, pot = null) {
  edit.value = { op, pot }
  amount.value = ''
  purpose.value = ''
}
const maxFen = computed(() => {
  if (!edit.value) return 0
  return edit.value.op === 'withdraw' ? edit.value.pot.balance_fen : (debit.value?.balance_fen ?? Infinity)
})
const editTitle = computed(() => {
  const e = edit.value
  if (!e) return ''
  if (!e.pot) return '新建心愿罐'
  return `${e.op === 'withdraw' ? '转出' : '转入'}「${e.pot.purpose}」`
})
// 元转分：最多两位小数，按字符串拆避免浮点误差
const amountFen = computed(() => {
  const s = String(amount.value).trim()
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return null
  const [y, f = ''] = s.split('.')
  return Number(y) * 100 + Number(f.padEnd(2, '0'))
})
const editProblem = computed(() => {
  const e = edit.value
  if (!e) return ''
  if (!e.pot && !purpose.value.trim()) return '请填写心愿罐用途'
  if (!String(amount.value).trim()) return '请填写金额'
  if (amountFen.value == null) return '金额格式不对，最多两位小数'
  if (amountFen.value <= 0) return '金额要大于 0'
  if (amountFen.value > maxFen.value) {
    return e.op === 'withdraw' ? `最多可转出 ${fmtFen(maxFen.value)} 元（罐内余额）` : `最多可转入 ${fmtFen(maxFen.value)} 元（活期余额）`
  }
  return ''
})

function submitEdit() {
  if (editProblem.value) return
  const { op, pot } = edit.value
  const fen = amountFen.value
  edit.value = null
  if (op === 'withdraw') {
    pick({ type: 'pot_manage', op: 'withdraw', pot_id: pot.pot_id, amount_fen: fen }, '心愿罐转出', `「${pot.purpose}」${fmtFen(fen)} 元转回活期`)
  } else if (pot) {
    pick({ type: 'pot_manage', op: 'deposit', pot_id: pot.pot_id, amount_fen: fen }, '转入心愿罐', `「${pot.purpose}」${fmtFen(fen)} 元`)
  } else {
    const p = purpose.value.trim()
    pick({ type: 'pot_manage', op: 'deposit', pot_id: null, purpose: p, amount_fen: fen }, '新建心愿罐', `「${p}」存入 ${fmtFen(fen)} 元`)
  }
}
</script>

<template>
  <div class="card pot pots" :class="{ closed: done }" data-testid="pots-card">
    <div class="card-title"><span>{{ card.title || '我的心愿罐' }}</span><span class="count">{{ pots.length }} 个</span></div>
    <div v-if="debit" class="debit">
      <span class="k">活期 · {{ debit.label }}</span><span class="v">¥{{ debit.balance }}</span>
    </div>

    <div v-for="p in pots" :key="p.pot_id" class="pot-item" data-testid="pot-item">
      <div class="p-top">
        <span class="p-name">「{{ p.purpose }}」</span>
        <span class="p-until">{{ p.until ? `${p.until} 到期` : '不设到期日' }}</span>
      </div>
      <div class="p-bal"><span class="money">¥{{ p.balance }}</span><span class="p-spent">已支出 ¥{{ p.spent }}</span></div>
      <div v-for="(s, i) in p.plans || []" :key="i" class="p-plan"><van-icon name="clock-o" />待执行：{{ s.label }}</div>
      <div class="p-ops">
        <van-button size="small" round plain type="danger" :disabled="done" data-testid="pot-deposit" @click="openEdit('deposit', p)">转入</van-button>
        <van-button size="small" round plain :disabled="done || p.balance_fen <= 0" data-testid="pot-withdraw"
                    @click="openEdit('withdraw', p)">转出</van-button>
        <van-button size="small" round plain data-testid="pot-detail-open" @click="detail = p">明细</van-button>
      </div>
    </div>
    <div v-if="!pots.length" class="empty">{{ card.empty_text || '还没有心愿罐' }}</div>

    <div v-if="done" class="done-mask">已提交，请在下方确认卡里核对</div>
    <van-button v-else block plain size="small" icon="plus" class="add" data-testid="pot-new" @click="openEdit('deposit')">新建心愿罐</van-button>
    <div v-if="card.verify_note" class="meta">{{ card.verify_note }}</div>

    <!-- 金额小弹窗 -->
    <van-popup :show="!!edit" round teleport="body" class="pot-pop" @update:show="(v) => { if (!v) edit = null }">
      <div v-if="edit" class="pot-box" data-testid="pot-amount-popup">
        <div class="pb-title">{{ editTitle }}</div>
        <div class="pb-sub">
          <template v-if="edit.op === 'withdraw'">罐内余额 ¥{{ edit.pot.balance }}，转回{{ debit?.label || '活期' }}</template>
          <template v-else-if="debit">从{{ debit.label }}转入，活期余额 ¥{{ debit.balance }}</template>
        </div>
        <van-field v-if="!edit.pot" v-model="purpose" class="pb-field" label="用途" maxlength="12" placeholder="如 妈妈生日"
                   data-testid="pot-purpose-input" />
        <van-field v-model="amount" class="pb-field" label="金额" type="number" placeholder="0.00" data-testid="pot-amount-input">
          <template #button>
            <span v-if="edit.op === 'withdraw'" class="pb-all" @click="amount = fmtFen(edit.pot.balance_fen).replace(/,/g, '')">全部</span>
            <span v-else class="pb-unit">元</span>
          </template>
        </van-field>
        <div v-if="editProblem && amount" class="pb-tip bad">{{ editProblem }}</div>
        <div v-else-if="edit.op === 'withdraw' && edit.pot.plans?.length" class="pb-tip">有待执行的计划会用到这笔钱，被预占的部分不能转出</div>
        <div v-else class="pb-tip">{{ edit.op === 'withdraw' ? '转出到活期只需确认' : '提交后需人脸或短信验证（任选其一）' }}</div>
        <div class="btns">
          <van-button size="small" @click="edit = null">取消</van-button>
          <van-button size="small" type="danger" :disabled="!!editProblem" data-testid="pot-amount-ok" @click="submitEdit">确定</van-button>
        </div>
      </div>
    </van-popup>

    <van-popup :show="!!detail" position="bottom" round closeable class="sheet-pop" teleport="body" :style="{ height: '70%' }"
               @update:show="(v) => { if (!v) detail = null }">
      <PotDetailSheet v-if="detail" :pot-id="detail.pot_id" :purpose="detail.purpose" />
    </van-popup>
  </div>
</template>

<style scoped>
.card.pots .card-title .count { font-size: var(--fs-xs); font-weight: 400; color: var(--muted); }
.debit { display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-2); font-size: var(--fs-sm);
  background: var(--surface-2); border-radius: var(--radius-sm); padding: 8px 10px; }
.debit .k { color: var(--muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.debit .v { font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.pot-item { padding: 10px 0; }
.pot-item + .pot-item { border-top: 1px solid var(--line-soft); }
.p-top { display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-2); }
.p-name { font-size: var(--fs-body); font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p-until { font-size: var(--fs-xs); color: var(--muted); flex-shrink: 0; font-variant-numeric: tabular-nums; }
.p-bal { display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-2); }
.p-spent { font-size: var(--fs-xs); color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.p-plan { display: flex; align-items: center; gap: 4px; font-size: var(--fs-xs); color: var(--yellow-ink); background: var(--yellow-soft);
  border-radius: var(--radius-xs); padding: 3px 8px; margin-top: 4px; line-height: 1.5; }
.p-plan .van-icon { flex-shrink: 0; }
.p-ops { display: flex; gap: var(--space-2); margin-top: var(--space-2); }
.p-ops .van-button { flex: 1; height: 30px; }
.empty { font-size: var(--fs-sm); color: var(--muted); text-align: center; padding: 12px 0; line-height: 1.6; }
.add { margin-top: 10px; }
.pot-pop { width: min(88vw, 340px); }
.pot-box { padding: var(--space-4); }
.pb-title { font-size: var(--fs-sheet); font-weight: 600; text-align: center; }
.pb-sub { font-size: var(--fs-xs); color: var(--muted); text-align: center; margin: 4px 0 var(--space-3); line-height: 1.5; }
.pb-field { background: var(--surface-2); border-radius: var(--radius-sm); }
.pb-field + .pb-field { margin-top: var(--space-2); }
.pb-field :deep(.van-field__label) { width: 3em; }
.pb-field :deep(.van-field__control) { font-variant-numeric: tabular-nums; }
.pb-unit { color: var(--muted); font-size: var(--fs-sm); }
.pb-all { color: var(--info); font-size: var(--fs-sm); cursor: pointer; }
.pb-tip { font-size: var(--fs-xs); color: var(--muted); line-height: 1.5; margin-top: var(--space-2); }
.pb-tip.bad { color: var(--red-ink); }
</style>
