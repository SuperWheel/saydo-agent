<script setup>
// 运营后台 · 按 trace 回放（技术文档 §5.8）：一轮对话从用户消息到执行结果的审计事件，
// 按「用户消息 → 意图 / 工具 → 策略定级 → 确认 → 验证 → 令牌 → 执行」分段展示。
// 确认、验证是之后单独的请求，但沿用出卡时的 trace_id，所以能串成一条完整的链。
import { computed, onMounted, ref, watch } from 'vue'
import { showToast } from 'vant'
import { api } from '../api'

const props = defineProps({ trace: { type: String, default: null } })

const LEVEL = { GREEN: '🟢 自动执行', YELLOW: '🟡 需要确认', RED: '🔴 需要强验证' }
const BREAKER = { readonly: '会话只读', freeze: '暂停办理', handoff: '转人工' }
const FACTOR = { face: '人脸', sms: '短信验证码' }
const SOURCE = { llm: 'AI 生成', rule: '规则引擎', template: '系统模板' }
const OUTCOME = {
  succeeded: ['执行成功', 'success'], failed: ['执行失败', 'danger'], cancelled: ['已取消', 'default'],
  expired: ['已过期', 'default'], invalidated: ['授权作废', 'warning'], blocked: ['已拦截', 'danger'],
  awaiting_auth: ['待验证', 'warning'], pending: ['待确认', 'warning'], replied: ['仅回复', 'default'],
  verified: ['核身通过', 'success'], auth_failed: ['核身未通过', 'danger'],
}
const flags = (p) => (p.flags?.length ? `标记 ${p.flags.join('、')}` : '')
// 免密额度：payload 里可能是「元」字符串或「分」整数
const yuan = (s, fen) => s ?? (fen != null ? (fen / 100).toLocaleString('en-US') : '')
const limits = (p) => [yuan(p.single_max, p.single_max_fen) && `单笔 ${yuan(p.single_max, p.single_max_fen)} 元`,
  yuan(p.daily_max, p.daily_max_fen) && `每日 ${yuan(p.daily_max, p.daily_max_fen)} 元`, p.card_label].filter(Boolean).join(' · ')
const contact = (p) => [p.label || p.relation, p.name_masked, p.tail && `尾号 ${p.tail}`, p.intimacy].filter(Boolean).join(' · ')

// 事件类型 → [标题, 详情]
const EVENTS = {
  user_message: ['用户消息', (p) => [p.text || '（选择了选项）', flags(p)].filter(Boolean).join(' · ')],
  injection_suspected: ['疑似注入，风险上浮', flags],
  intent: ['规则引擎识别意图', (p) => `${p.intent}${Object.keys(p.slots || {}).length ? ' ' + JSON.stringify(p.slots) : ''}`],
  llm_call: ['调用模型', (p) => `第 ${p.step + 1} 步 · ${p.model}${p.tool_calls?.length ? ' → ' + p.tool_calls.join('、') : ' → 直接回复'}`],
  tool_call: ['工具调用', (p) => `${p.tool} · ${p.status}${p.card ? ' · 出卡 ' + p.card : ''}`],
  llm_error: ['模型异常，降级为规则引擎', (p) => p.error],
  provenance_reject: ['金额来源校验不通过', (p) => `「${p.amount_text}」不在用户原话里`],
  grounding_failed: ['事实校验不通过', (p) => p.problem],
  grounding_retry: ['重写后再校验', (p) => (p.ok ? '通过' : p.problem)],
  assistant_reply: ['助手回复', (p) => `${p.workflow ? '确定性工作流' : SOURCE[p.source] || p.source}`
    + `${p.elapsed_ms != null ? `（${p.elapsed_ms} 毫秒）` : ''}：${p.text}`],
  policy_decision: ['策略引擎定级', (p) => (p.block ? `拒绝：${p.block_reason}` : `${LEVEL[p.level]} · ${p.reasons.join('；')}`)],
  breaker_tripped: ['触发熔断', (p) => `${p.reason} → ${BREAKER[p.level]}${p.scope === 'session' ? '（本会话）' : '（用户级）'}`],
  breaker_blocked: ['熔断拦截', (p) => `${p.reason}${p.level in BREAKER ? ' · ' + BREAKER[p.level] : ''}`],
  action_created: ['出示确认卡', (p) => `${p.summary} · ${LEVEL[p.level]} · digest ${p.digest?.slice(0, 8)}`],
  action_confirmed: ['用户点击确认', () => '提交的 digest 与确认卡一致'],
  digest_mismatch: ['确认内容与原动作不一致，已拒绝', () => ''],
  action_cancelled: ['用户取消', () => '已释放预占额度'],
  action_expired: ['确认卡过期', () => '已释放预占额度'],
  action_invalidated: ['授权作废', (p) => p.reason],
  auth_challenge: ['发起强验证', (p) => p.factors.map((f) => FACTOR[f] || f).join(' + ')],
  auth_failed: ['验证未通过', (p) => `${FACTOR[p.factor] || p.factor} · 第 ${p.attempts} 次`],
  auth_passed: ['验证通过', (p) => FACTOR[p.factor] || p.factor],
  card_number_viewed: ['查看完整卡号', (p) => `尾号 ${p.card_tail} · 人脸验证通过后返回一次，卡号不入审计`],
  entry_face_passed: ['进入前人脸核身通过', (p) => `模拟人脸 · 签发进入凭证 ${p.ttl_s / 60} 分钟有效 · jti ${p.jti}`],
  entry_face_failed: ['进入前人脸核身未通过', () => '模拟人脸 · 未签发进入凭证'],
  policy_revalidated: ['执行前重新校验', (p) => (p.block ? `拒绝：${p.block_reason}` : `${LEVEL[p.level]}，与出卡时一致`)],
  token_issued: ['签发执行令牌', (p) => `一次性 · 60 秒有效 · jti ${p.jti}`],
  execution: ['工具网关执行', (p) => [p.status === 'succeeded' ? '成功' : p.status, p.txn_no && `流水号 ${p.txn_no}`].filter(Boolean).join(' · ')],
  execution_failed: ['执行失败', (p) => p.error],
  circuit_open: ['断路器断开', (p) => `${p.tool} · ${p.failures}/${p.calls} 次失败`],
  payment_agreement_signed: ['签署支付服务协议', (p) => (p.version ? `版本 ${p.version}` : '')],
  payment_agreement_declined: ['拒绝支付服务协议', (p) => (p.action_id ? '被拦的那笔动作已取消' : '')],
  mandate_offer_dismissed: ['暂不开通免密', () => '30 天内不再提示'],
  mandate_signed: ['开通免密支付', limits],
  mandate_updated: ['修改免密额度', limits],
  mandate_revoked: ['关闭免密支付', () => '之后每笔按原规则验证'],
  contact_added: ['新增联系人', contact],
  contact_updated: ['修改联系人', (p) => [contact(p), p.fields?.length && `改了 ${p.fields.join('、')}`].filter(Boolean).join(' · ')],
  contact_deleted: ['删除联系人', (p) => [contact(p), '对 TA 的免密支付立即失效'].filter(Boolean).join(' · ')],
  contact_intimacy_bound: ['绑定亲密关系', contact],
  contact_intimacy_unbound: ['解除亲密关系', (p) => [contact(p), '免密支付对 TA 立即失效'].filter(Boolean).join(' · ')],
}
const BAD = new Set(['injection_suspected', 'llm_error', 'provenance_reject', 'grounding_failed', 'breaker_tripped',
  'breaker_blocked', 'digest_mismatch', 'action_cancelled', 'action_expired', 'action_invalidated', 'auth_failed',
  'execution_failed', 'circuit_open', 'entry_face_failed', 'payment_agreement_declined'])

const STAGES = [
  ['input', '用户消息', ['user_message', 'injection_suspected']],
  ['intent', '意图 / 工具', ['intent', 'llm_call', 'tool_call', 'llm_error', 'provenance_reject', 'grounding_failed', 'grounding_retry', 'assistant_reply']],
  ['policy', '策略定级', ['policy_decision', 'breaker_tripped', 'breaker_blocked']],
  ['confirm', '确认', ['action_created', 'action_confirmed', 'digest_mismatch', 'action_cancelled', 'action_expired', 'action_invalidated',
    'payment_agreement_signed', 'payment_agreement_declined', 'mandate_offer_dismissed']],
  ['auth', '验证', ['auth_challenge', 'auth_failed', 'auth_passed', 'card_number_viewed', 'entry_face_passed', 'entry_face_failed']],
  ['token', '令牌', ['policy_revalidated', 'token_issued']],
  ['exec', '执行', ['execution', 'execution_failed', 'circuit_open', 'mandate_signed', 'mandate_updated', 'mandate_revoked',
    'contact_added', 'contact_updated', 'contact_deleted', 'contact_intimacy_bound', 'contact_intimacy_unbound']],
]
const KNOWN = new Set(STAGES.flatMap((s) => s[2]))

const traces = ref([])
const selected = ref(props.trace)
const rows = ref([])
const chain = ref(null)
const open = ref(new Set())
const loading = ref(false)

const current = computed(() => traces.value.find((t) => t.trace_id === selected.value))

function bad(e) {
  return BAD.has(e.event) || (e.event === 'policy_decision' && e.payload.block)
    || (e.event === 'policy_revalidated' && e.payload.block) || (e.event === 'tool_call' && e.payload.status === 'error')
}

function emptyHint(key) {
  const level = rows.value.find((e) => e.event === 'action_created')?.payload.level
  const hasAction = rows.value.some((e) => e.event === 'policy_decision' || e.event === 'breaker_blocked')
  if (rows.value.some((e) => e.event.startsWith('entry_face'))) return key === 'input' ? '进入 App 前核身，没有对话' : '不涉及'
  if (key === 'policy' && !hasAction) return '本轮没有资金或账户变更'
  if (key === 'auth' && level === 'YELLOW') return '🟡 只需确认，无需强验证'
  return '未发生'
}

const stages = computed(() => {
  const out = STAGES.map(([key, label, types]) => {
    const evs = rows.value.filter((e) => types.includes(e.event))
    return { key, label, events: evs, state: !evs.length ? 'skip' : evs.some(bad) ? 'warn' : 'done' }
  })
  const other = rows.value.filter((e) => !KNOWN.has(e.event))
  if (other.length) out.push({ key: 'other', label: '其他事件', events: other, state: 'done' })
  return out
})

function title(e) { return EVENTS[e.event]?.[0] || e.event }
function detail(e) {
  try { return EVENTS[e.event]?.[1](e.payload) ?? '' } catch { return '' }
}
function toggle(id) {
  const s = new Set(open.value)
  s.has(id) ? s.delete(id) : s.add(id)
  open.value = s
}

async function loadTraces(want = selected.value) {
  loading.value = true
  try {
    traces.value = await api.traces()
    const next = traces.value.some((t) => t.trace_id === want) ? want : traces.value[0]?.trace_id || null
    if (next === selected.value) await loadRows()
    else selected.value = next // watch 会加载这一条的事件
  } catch (e) {
    showToast(e.message)
  } finally {
    loading.value = false
  }
}

async function loadRows() {
  open.value = new Set()
  rows.value = selected.value ? await api.audit(selected.value) : []
}

async function verifyChain() {
  chain.value = await api.verifyAudit()
}

watch(selected, loadRows)
watch(() => props.trace, (t) => t && loadTraces(t))
onMounted(() => loadTraces(props.trace))
defineExpose({ refresh: () => loadTraces() })
</script>

<template>
  <div class="timeline">
    <div class="toolbar">
      <span class="hint">选择一轮对话回放</span>
      <div class="actions">
        <van-button size="mini" plain icon="replay" :loading="loading" @click="loadTraces()">刷新</van-button>
        <van-button size="mini" type="primary" plain @click="verifyChain">校验哈希链</van-button>
      </div>
    </div>
    <div v-if="chain" class="chain" :class="{ bad: !chain.ok }">
      {{ chain.ok ? `哈希链完整，共 ${chain.count} 条，逐条重算无篡改` : `哈希链在第 ${chain.broken_at} 条断开，该条之后的记录不可信` }}
    </div>

    <div class="traces">
      <div v-for="t in traces" :key="t.trace_id" class="trace" :class="{ on: t.trace_id === selected }"
           @click="selected = t.trace_id">
        <div class="t-text">{{ t.text || '（选项 / 无文字）' }}</div>
        <div class="t-meta">
          <span>{{ t.started_at.slice(11, 19) }}</span>
          <span v-if="t.level"><IconText :text="LEVEL[t.level]?.slice(0, 2)" /></span>
          <span :class="['out', OUTCOME[t.outcome]?.[1]]">{{ OUTCOME[t.outcome]?.[0] || t.outcome }}</span>
          <span v-if="t.breaker" class="out danger">熔断</span>
        </div>
      </div>
      <div v-if="!traces.length && !loading" class="empty">还没有对话记录</div>
    </div>

    <template v-if="current">
      <div class="summary">
        <div class="s-text">「{{ current.text || '选项' }}」</div>
        <div class="s-meta">
          trace {{ current.trace_id }} · {{ current.events }} 条事件
          <span v-if="current.flags.length" class="flag">注入标记：{{ current.flags.join('、') }}</span>
        </div>
      </div>

      <ol class="stages">
        <li v-for="(s, i) in stages" :key="s.key" class="stage" :class="s.state">
          <div class="dot">{{ s.state === 'skip' ? '·' : s.state === 'warn' ? '!' : i + 1 }}</div>
          <div class="body">
            <div class="s-label">{{ s.label }}<span v-if="!s.events.length" class="skip-hint"><IconText :text="emptyHint(s.key)" /></span></div>
            <div v-for="e in s.events" :key="e.id" class="ev" :class="{ bad: bad(e) }" @click="toggle(e.id)">
              <div class="ev-line"><span class="ev-title">{{ title(e) }}</span><span class="ev-at">{{ e.at.slice(11, 19) }}</span></div>
              <div v-if="detail(e)" class="ev-detail"><IconText :text="detail(e)" /></div>
              <div v-if="open.has(e.id)" class="ev-raw">{{ JSON.stringify(e.payload, null, 1) }}</div>
              <div class="ev-hash">{{ e.event }} · hash {{ e.hash }}</div>
            </div>
          </div>
        </li>
      </ol>
    </template>
  </div>
</template>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.hint { font-size: var(--fs-xs); color: var(--muted); }
.actions { display: flex; gap: 6px; }
.chain { margin: 10px 0 0; padding: 8px 10px; border-radius: var(--radius-sm); background: var(--green-soft); color: var(--green-ink); font-size: var(--fs-sm); }
.chain.bad { background: var(--red-soft); color: var(--red); }
.traces { display: flex; gap: 8px; overflow-x: auto; padding: 10px 0 4px; scrollbar-width: thin; }
.trace {
  flex: 0 0 150px; border: 1px solid var(--line); border-radius: var(--radius-md); padding: 8px; background: var(--surface);
  cursor: pointer; font-size: var(--fs-xs);
}
.trace.on { border-color: var(--brand); box-shadow: 0 0 0 1px var(--brand) inset; }
.t-text { font-size: var(--fs-sm); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.t-meta { display: flex; gap: 6px; flex-wrap: wrap; color: var(--muted); margin-top: 4px; }
.out.success { color: var(--green-ink); }
.out.danger { color: var(--red); }
.out.warning { color: var(--yellow-ink); }
.empty { color: var(--muted); font-size: var(--fs-sm); padding: 8px 0; }
.summary { margin: 8px 0 4px; padding: 8px 10px; background: var(--surface-2); border-radius: var(--radius-sm); }
.s-text { font-size: var(--fs-body); font-weight: 600; word-break: break-all; }
.s-meta { font-size: var(--fs-2xs); color: var(--muted); margin-top: 3px; word-break: break-all; }
.flag { color: var(--red); margin-left: 4px; }
.stages { list-style: none; margin: 8px 0 0; padding: 0; }
.stage { display: flex; gap: 10px; position: relative; padding-bottom: 12px; }
.stage:not(:last-child)::before {
  content: ''; position: absolute; left: 10px; top: 22px; bottom: 0; width: 2px; background: var(--line);
}
.dot {
  flex: 0 0 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: var(--fs-xs); font-weight: 700; color: var(--on-brand); background: var(--green); z-index: 1;
}
.warn .dot { background: var(--red); }
.skip .dot { background: var(--faint); }
.body { flex: 1; min-width: 0; }
.s-label { font-size: var(--fs-body); font-weight: 600; line-height: 22px; }
.skip .s-label { color: var(--muted); font-weight: 400; }
.skip-hint { font-size: var(--fs-xs); color: var(--muted); font-weight: 400; margin-left: 8px; }
.ev { margin-top: 6px; padding: 7px 9px; border-radius: var(--radius-sm); background: var(--surface-2); cursor: pointer; }
.ev.bad { background: var(--red-soft); }
.ev-line { display: flex; justify-content: space-between; gap: 8px; font-size: var(--fs-sm); }
.ev-title { font-weight: 600; }
.ev.bad .ev-title { color: var(--red); }
.ev-at { color: var(--muted); font-size: var(--fs-2xs); flex-shrink: 0; }
.ev-detail { font-size: var(--fs-xs); color: var(--ink-2); margin-top: 2px; word-break: break-all; line-height: 1.5; }
.ev-raw {
  font-size: var(--fs-2xs); white-space: pre-wrap; word-break: break-all; margin-top: 4px; padding: 6px; border-radius: var(--radius-sm);
  background: var(--surface); font-family: var(--font-mono); color: var(--ink-2);
}
.ev-hash { font-size: var(--fs-2xs); color: var(--faint); margin-top: 3px; font-family: var(--font-mono); }
</style>
