<script setup>
// 《免密支付授权》卡。两种模式：
// - invite：首次支付成功后附在回执后面的一句邀请，「去设置」让后端回设置表单，「暂不开通」30 天内不再弹；
// - form：选默认付款卡，点单笔 / 每日金额弹出小弹窗用五列滚轮设额度，选授权有效期，看适用对象和规则，提交后回到对话出确认卡
//   （开通、调高、换卡、延长有效期 🔴 人脸 + 短信；只调低 / 缩短 🟡）。已开通时多显示当前设置和「关闭免密支付」。
//   「取消」纯前端：卡片收起，不调后端。
// 额度校验和后端一致：0 < 单笔 ≤ 系统上限，0 < 每日 ≤ 系统上限，单笔 ≤ 每日；后端会再校验一遍。
import { computed, ref } from 'vue'
import DigitWheel from '../DigitWheel.vue'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])

const done = ref('')
const rulesOpen = ref(false)
const fmt = (n) => n.toLocaleString('en-US')

const cur = computed(() => props.card.current)
const cards = computed(() => props.card.cards || [])
const capSingle = computed(() => Math.floor((props.card.caps?.single_max_fen || 0) / 100))
const capDaily = computed(() => Math.floor((props.card.caps?.daily_max_fen || 0) / 100))
const init = cur.value || props.card.defaults || {}
const firstPayable = () => (cards.value.find((c) => c.payable) || {}).id
const cardId = ref(cur.value?.card_id ?? (cards.value.find((c) => c.id === props.card.default_card_id && c.payable) ? props.card.default_card_id : firstPayable()))
const single = ref(Math.floor((init.single_max_fen || 0) / 100))
const daily = ref(Math.floor((init.daily_max_fen || 0) / 100))

// 有效期：旧卡片没有 validity_options 时不显示选择器、不回传 valid_days（后端按原规则 365 天）
const validityOptions = computed(() => props.card.validity_options || [])
const curDays = cur.value?.valid_days ?? null
const validDays = ref(curDays ?? props.card.defaults?.valid_days ?? 365)
const daysChanged = computed(() => !cur.value || validDays.value !== curDays)
// 到期日预览：今天 + N 天（本地日期）；修改时没换期限就保持原到期日
const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const newUntil = computed(() => {
  if (cur.value && !daysChanged.value) return cur.value.valid_until
  const d = new Date()
  d.setDate(d.getDate() + validDays.value)
  return ymd(d)
})

// 第一条不满足的原因；有原因就禁用提交
const problem = computed(() => {
  if (!cards.value.some((c) => c.id === cardId.value && c.payable)) return '请选择一张能付款的默认卡'
  if (single.value <= 0) return '单笔额度不能为 0'
  if (daily.value <= 0) return '每日额度不能为 0'
  if (single.value > capSingle.value) return `单笔额度最高 ${fmt(capSingle.value)} 元`
  if (daily.value > capDaily.value) return `每日额度最高 ${fmt(capDaily.value)} 元`
  if (single.value > daily.value) return '单笔额度不能高于每日额度'
  if (unchanged.value) return '和当前设置相同，调一下额度、有效期或换张卡再提交'
  return ''
})
// 已开通且什么都没改：只是不能提交，不算错，提示用灰色
const unchanged = computed(() => !!cur.value && cardId.value === cur.value.card_id && !daysChanged.value
  && single.value * 100 === cur.value.single_max_fen && daily.value * 100 === cur.value.daily_max_fen)
const singleBad = computed(() => single.value <= 0 || single.value > capSingle.value || single.value > daily.value)
const dailyBad = computed(() => daily.value <= 0 || daily.value > capDaily.value)

// 开通、调高任一额度、换卡、延长有效期（新到期日晚于原到期日）要人脸 + 短信；只调低 / 缩短只需确认
const strong = computed(() => !cur.value || cardId.value !== cur.value.card_id
  || single.value * 100 > cur.value.single_max_fen || daily.value * 100 > cur.value.daily_max_fen
  || (daysChanged.value && newUntil.value > (cur.value.valid_until || '')))

// —— 额度小弹窗：点金额才弹出滚轮，「确定」后才写回 ——
const edit = ref(null) // { key: 'single' | 'daily', label, max }
const draft = ref(0)
function openAmount(key) {
  edit.value = key === 'single' ? { key, label: '单笔限额', max: capSingle.value } : { key, label: '每日限额', max: capDaily.value }
  draft.value = key === 'single' ? single.value : daily.value
}
const draftProblem = computed(() => {
  if (!edit.value) return ''
  if (draft.value <= 0) return `${edit.value.label}不能为 0`
  if (draft.value > edit.value.max) return `${edit.value.label}最高 ${fmt(edit.value.max)} 元`
  return ''
})
// 单笔高于每日不拦弹窗（可能接着改另一项），只提示一句
const draftHint = computed(() => {
  if (!edit.value || draftProblem.value) return ''
  if (edit.value.key === 'single' && draft.value > daily.value) return `高于每日限额 ${fmt(daily.value)} 元，提交前还要调高每日限额`
  if (edit.value.key === 'daily' && draft.value < single.value) return `低于单笔限额 ${fmt(single.value)} 元，提交前还要调低单笔限额`
  return ''
})
function okAmount() {
  if (draftProblem.value) return
  if (edit.value.key === 'single') single.value = draft.value
  else daily.value = draft.value
  edit.value = null
}

function pick(selection, label, detail, mask = '已提交，请在下方确认卡里核对') {
  if (done.value) return
  done.value = mask
  emit('select', { label, detail, selection })
}

function submit() {
  if (problem.value) return
  const sel = { type: 'mandate_setup', card_id: cardId.value, single_max_fen: single.value * 100, daily_max_fen: daily.value * 100 }
  // 开通时总带有效期；修改时只在换了期限时带（不带 = 保持原期限）
  const withDays = validityOptions.value.length && daysChanged.value
  if (withDays) sel.valid_days = validDays.value
  pick(sel, cur.value ? '修改免密设置' : '开通免密支付',
    `单笔 ${fmt(single.value)} 元 / 每日 ${fmt(daily.value)} 元${withDays ? ` / 有效期 ${validDays.value} 天` : ''}`)
}
const cancel = () => { if (!done.value) done.value = '已取消，未做任何修改' }
const revoke = () => pick({ type: 'mandate_change', action: 'revoke' }, '关闭免密支付', '之后每笔按原规则验证')
const openForm = () => pick({ type: 'mandate_offer', action: 'open' }, '去设置免密支付', '选默认卡和额度', '已打开设置')
const dismiss = () => pick({ type: 'mandate_offer', action: 'dismiss' }, '暂不开通免密支付', '30 天内不再提示', '好的，30 天内不再提示')
</script>

<template>
  <!-- 邀请：首次支付成功后 -->
  <div v-if="card.mode === 'invite'" class="card mandate" :class="{ closed: done }" data-testid="mandate-invite">
    <div class="card-title"><span>{{ card.title || '免密支付授权' }}</span><van-tag plain type="primary">可选</van-tag></div>
    <div class="invite">{{ card.invite_text }}</div>
    <div v-if="done" class="done-mask">{{ done }}</div>
    <div v-else class="btns">
      <van-button size="small" data-testid="mandate-dismiss" @click="dismiss">暂不开通</van-button>
      <van-button size="small" type="danger" data-testid="mandate-open" @click="openForm">去设置</van-button>
    </div>
  </div>

  <!-- 设置表单 -->
  <div v-else class="card mandate" :class="{ closed: done }" data-testid="mandate-card">
    <div class="card-title">
      <span>{{ card.title || '免密支付授权' }}</span>
      <van-tag v-if="cur" plain type="success">已开通</van-tag>
      <van-tag v-else plain type="default">未开通</van-tag>
    </div>

    <template v-if="cur">
      <div class="sec">当前设置</div>
      <div class="row"><span class="k">默认付款卡</span><span class="v">{{ cur.card_label }}</span></div>
      <div class="row"><span class="k">额度</span><span class="v">单笔 ¥{{ cur.single_max }} · 每日 ¥{{ cur.daily_max }}</span></div>
      <div class="row"><span class="k">今日已用</span><span class="v">¥{{ cur.used_today }}</span></div>
      <div class="row"><span class="k">有效期</span><span class="v">{{ cur.signed_at?.slice(0, 10) }} 至 {{ cur.valid_until }}<template v-if="cur.valid_days">（{{ cur.valid_days }} 天）</template></span></div>
    </template>

    <template v-if="!done">
      <div class="sec">默认付款卡</div>
      <van-radio-group v-model="cardId">
        <div v-for="c in cards" :key="c.id" class="pay-card" :class="{ on: c.id === cardId, off: !c.payable }"
             data-testid="mandate-pay-card" @click="c.payable && (cardId = c.id)">
          <van-radio :name="c.id" :disabled="!c.payable" />
          <div class="pc-body">
            <div class="pc-label">{{ c.label }}</div>
            <div v-if="!c.payable && c.reason" class="pc-why">{{ c.reason }}</div>
          </div>
        </div>
      </van-radio-group>

      <div class="sec">免密额度 <small>点金额修改</small></div>
      <div class="amt-row" :class="{ bad: singleBad }" data-testid="mandate-single-amount" @click="openAmount('single')">
        <span class="k">单笔限额</span><span class="v"><b>{{ fmt(single) }}</b> 元<van-icon name="arrow" class="more" /></span>
      </div>
      <div class="amt-row" :class="{ bad: dailyBad }" data-testid="mandate-daily-amount" @click="openAmount('daily')">
        <span class="k">每日限额</span><span class="v"><b>{{ fmt(daily) }}</b> 元<van-icon name="arrow" class="more" /></span>
      </div>
      <div class="caps">系统上限：单笔 {{ fmt(capSingle) }} 元、每日 {{ fmt(capDaily) }} 元；超过您设的额度一律升级为强验证</div>

      <template v-if="validityOptions.length">
        <div class="sec">授权有效期</div>
        <div class="days" data-testid="mandate-validity">
          <span v-for="n in validityOptions" :key="n" class="day-chip" :class="{ on: n === validDays }"
                data-testid="mandate-validity-option" @click="validDays = n">{{ n }} 天</span>
        </div>
        <div class="caps">
          {{ cur && !daysChanged ? `保持原到期日 ${newUntil}` : `到期日 ${newUntil}` }}，到期后免密自动失效，需要重新开通
        </div>
      </template>
    </template>

    <div class="sec">适用对象</div>
    <div class="scope">
      <div class="sc-k">亲密联系人</div>
      <div v-if="card.scope?.contacts?.length" class="chips">
        <span v-for="c in card.scope.contacts" :key="c.id" class="chip">{{ c.label }}<em>{{ c.intimacy }}</em></span>
      </div>
      <div v-else class="sc-none">还没有绑定亲密关系的联系人，可以说「我的联系人」去绑定</div>
      <div class="sc-k">合作商户</div>
      <div class="chips"><span v-for="m in card.scope?.merchants || []" :key="m" class="chip">{{ m }}</span></div>
    </div>

    <div class="toggle" data-testid="mandate-rules-toggle" @click="rulesOpen = !rulesOpen">
      规则详情 <van-icon :name="rulesOpen ? 'arrow-up' : 'arrow-down'" />
    </div>
    <ul v-if="rulesOpen" class="rules"><li v-for="r in card.rules" :key="r">{{ r }}</li></ul>

    <div v-if="done" class="done-mask">{{ done }}</div>
    <template v-else>
      <div v-if="problem" class="problem" :class="{ soft: unchanged }" data-testid="mandate-problem">{{ problem }}</div>
      <div v-else class="verify-hint">
        <IconText :text="strong ? '🔴 提交后需人脸 + 短信验证' : '🟡 只调低额度或缩短有效期，提交后点确认即可'" />
      </div>
      <div class="btns">
        <van-button size="small" data-testid="mandate-cancel" @click="cancel">取消</van-button>
        <van-button size="small" type="danger" :disabled="!!problem" data-testid="mandate-submit" @click="submit">
          {{ cur ? '修改免密设置' : '开通免密支付' }}
        </van-button>
      </div>
      <van-button v-if="cur" block plain size="small" class="revoke" data-testid="mandate-revoke" @click="revoke">关闭免密支付</van-button>
    </template>

    <!-- 额度小弹窗 -->
    <van-popup :show="!!edit" round teleport="body" class="amount-pop" @update:show="(v) => { if (!v) edit = null }">
      <div v-if="edit" class="amount-box" data-testid="amount-popup">
        <div class="ab-title">修改{{ edit.label }}</div>
        <DigitWheel v-model="draft" label="金额" :max="edit.max" :error="!!draftProblem" />
        <div v-if="draftProblem" class="ab-tip bad">{{ draftProblem }}</div>
        <div v-else-if="draftHint" class="ab-tip">{{ draftHint }}</div>
        <div class="btns">
          <van-button size="small" data-testid="amount-popup-cancel" @click="edit = null">取消</van-button>
          <van-button size="small" type="danger" :disabled="!!draftProblem" data-testid="amount-popup-ok" @click="okAmount">确定</van-button>
        </div>
      </div>
    </van-popup>
  </div>
</template>

<style scoped>
.invite { font-size: var(--fs-body); line-height: 1.6; }
.sec { font-size: var(--fs-sm); font-weight: 600; margin: 14px 0 6px; }
.card-title + .sec { margin-top: 0; }
.pay-card { display: flex; align-items: flex-start; gap: var(--space-2); border: 1px solid var(--line); border-radius: var(--radius-md);
  padding: 8px 12px; cursor: pointer; transition: border-color .15s, background .15s; }
.pay-card + .pay-card { margin-top: 6px; }
.pay-card.on { border-color: var(--brand); background: var(--brand-soft); }
.pay-card.off { cursor: not-allowed; background: var(--surface-2); }
.pay-card.off .pc-label { color: var(--faint); }
.pay-card .van-radio { margin-top: 1px; }
.pc-body { min-width: 0; }
.pc-label { font-size: var(--fs-body); line-height: 20px; }
.pc-why { font-size: var(--fs-xs); color: var(--muted); line-height: 1.5; margin-top: 2px; }
.sec small { font-weight: 400; color: var(--muted); font-size: var(--fs-xs); margin-left: 4px; }
.amt-row { display: flex; justify-content: space-between; align-items: center; gap: var(--space-2); border: 1px solid var(--line);
  border-radius: var(--radius-md); padding: 8px 12px; cursor: pointer; }
.amt-row + .amt-row { margin-top: 6px; }
.amt-row:active { background: var(--surface-2); }
.amt-row .k { font-size: var(--fs-body); color: var(--muted); }
.amt-row .v { display: inline-flex; align-items: center; gap: 2px; font-size: var(--fs-sm); color: var(--muted); }
.amt-row b { font-size: var(--fs-money-md); color: var(--ink); font-variant-numeric: tabular-nums; margin-right: 2px; }
.amt-row.bad { border-color: var(--red); background: var(--red-soft); }
.amt-row.bad b { color: var(--red); }
.more { color: var(--faint); font-size: var(--fs-xs); }
.days { display: flex; flex-wrap: wrap; gap: 6px; }
.day-chip { font-size: var(--fs-sm); line-height: 18px; padding: 6px 14px; border-radius: var(--radius-pill); background: var(--surface-2);
  color: var(--ink); cursor: pointer; font-variant-numeric: tabular-nums; }
.day-chip.on { background: var(--brand); color: var(--on-brand); }
.revoke { margin-top: var(--space-2); }
.amount-pop { width: min(88vw, 340px); }
.amount-box { padding: var(--space-4); }
.ab-title { font-size: var(--fs-sheet); font-weight: 600; text-align: center; margin-bottom: var(--space-3); }
.ab-tip { font-size: var(--fs-xs); color: var(--muted); line-height: 1.5; margin-top: var(--space-2); }
.ab-tip.bad { color: var(--red-ink); }
.caps { font-size: var(--fs-xs); color: var(--muted); line-height: 1.6; margin-top: 6px; }
.scope { background: var(--surface-2); border-radius: var(--radius-sm); padding: 8px 10px; }
.sc-k { font-size: var(--fs-xs); color: var(--muted); }
.sc-k + .chips, .sc-k + .sc-none { margin-top: 4px; }
.chips + .sc-k, .sc-none + .sc-k { margin-top: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: var(--fs-xs); line-height: 18px; padding: 2px 8px; border-radius: var(--radius-pill); background: var(--surface); color: var(--ink); }
.chip em { font-style: normal; color: var(--green-ink); margin-left: 4px; }
.sc-none { font-size: var(--fs-xs); color: var(--muted); line-height: 1.5; }
.toggle { display: flex; align-items: center; gap: 2px; font-size: var(--fs-sm); color: var(--info); padding: 10px 0 0; cursor: pointer; }
.rules { margin: 6px 0 0; padding-left: 18px; list-style: disc; font-size: var(--fs-xs); color: var(--ink-2); line-height: 1.7; }
.problem { margin-top: 12px; font-size: var(--fs-sm); color: var(--red-ink); background: var(--red-soft); border-radius: var(--radius-sm); padding: 6px 10px; line-height: 1.5; }
.problem.soft { color: var(--muted); background: var(--surface-2); }
.verify-hint { margin-top: 12px; font-size: var(--fs-xs); color: var(--muted); }
</style>
