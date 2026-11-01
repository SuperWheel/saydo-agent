<script setup>
// 联系人管理：列表（称呼、打码姓名、开户行尾号、打码手机号、亲密关系徽标）和增 / 删 / 改 / 绑定或解除亲密关系。
// 每个操作都转成 selection 回到对话，后端出确认卡（新增、修改、删除、解除 🟡；绑定亲密关系 🔴 人脸，可改短信），确认后才生效。
// 完整卡号、手机号只放在 selection 里，label / detail 只用打码值（它们会作为用户消息显示在对话里）。
// 修改时卡号、手机号不预填明文，留空表示不改；只传改了的字段。
import { computed, reactive, ref } from 'vue'
import { showConfirmDialog, showToast } from 'vant'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])

const done = ref(false)
const items = computed(() => props.card.items || [])
const banks = computed(() => props.card.banks || [])
const who = (c) => [c.relation, c.name_masked].filter(Boolean).join(' · ')

function pick(selection, label, detail) {
  if (done.value) return
  done.value = true
  emit('select', { label, detail, selection })
}

// 姓名打码：张三 → 张*三，王秀英 → 王*英（和后端口径一致）
function maskName(n) {
  const s = [...n]
  if (s.length <= 1) return n
  return `${s[0]}${'*'.repeat(Math.max(1, s.length - 2))}${s.at(-1)}`
}

// —— 新增 / 修改表单 ——
const formOpen = ref(false)
const editing = ref(null) // 正在修改的联系人；新增时为 null
const form = reactive({ name: '', account_no: '', bank: '', phone: '', relation: '' })

function openAdd() {
  editing.value = null
  Object.assign(form, { name: '', account_no: '', bank: banks.value[0] || '', phone: '', relation: '' })
  formOpen.value = true
}
function openEdit(c) {
  editing.value = c
  Object.assign(form, { name: '', account_no: '', bank: c.bank || '', phone: '', relation: c.relation || '' })
  formOpen.value = true
}

const clean = (s) => s.replace(/\s+/g, '')
const errors = computed(() => {
  const e = {}
  const name = form.name.trim()
  const acct = clean(form.account_no)
  const phone = clean(form.phone)
  if (!editing.value && !name) e.name = '请填写收款人姓名'
  else if (name && !/^[一-龥·A-Za-z ]{2,20}$/.test(name)) e.name = '姓名为 2–20 个汉字或字母'
  if (!editing.value && !acct) e.account_no = '请填写收款卡号'
  else if (acct && !/^\d{12,19}$/.test(acct)) e.account_no = '卡号为 12–19 位数字'
  if (!form.bank) e.bank = '请选择开户行'
  if (phone && !/^1\d{10}$/.test(phone)) e.phone = '手机号为 11 位、以 1 开头'
  if (form.relation.trim().length > 10) e.relation = '称呼不超过 10 个字'
  return e
})

// 修改时只放改了的字段
const changes = computed(() => {
  const c = editing.value
  if (!c) return null
  const out = {}
  if (form.name.trim()) out.name = form.name.trim()
  if (clean(form.account_no)) out.account_no = clean(form.account_no)
  if (form.bank !== (c.bank || '')) out.bank = form.bank
  if (clean(form.phone)) out.phone = clean(form.phone)
  if (form.relation.trim() !== (c.relation || '')) out.relation = form.relation.trim()
  return out
})
const formProblem = computed(() => {
  const first = Object.values(errors.value)[0]
  if (first) return first
  if (editing.value && !Object.keys(changes.value).length) return '还没有修改任何内容'
  return ''
})

function submitForm() {
  if (formProblem.value) { showToast(formProblem.value); return }
  formOpen.value = false
  if (!editing.value) {
    const acct = clean(form.account_no)
    const sel = { type: 'contact_add', name: form.name.trim(), account_no: acct, bank: form.bank }
    if (clean(form.phone)) sel.phone = clean(form.phone)
    if (form.relation.trim()) sel.relation = form.relation.trim()
    pick(sel, '新增联系人', `${maskName(sel.name)} · ${sel.bank} 尾号 ${acct.slice(-4)}`)
    return
  }
  const ch = changes.value
  const what = []
  if (ch.name) what.push(`姓名改为 ${maskName(ch.name)}`)
  if (ch.account_no) what.push(`卡号改为尾号 ${ch.account_no.slice(-4)}`)
  if (ch.bank) what.push(`开户行改为${ch.bank}`)
  if (ch.phone) what.push(`手机号改为尾号 ${ch.phone.slice(-4)}`)
  if ('relation' in ch) what.push(ch.relation ? `称呼改为${ch.relation}` : '清空称呼')
  pick({ type: 'contact_update', contact_id: editing.value.id, ...ch }, '修改联系人', `${who(editing.value)} · ${what.join('，')}`)
}

// —— 删除 / 亲密关系 ——
async function remove(c) {
  try {
    await showConfirmDialog({
      title: '删除联系人',
      message: `确定删除「${who(c)}」吗？删除后对该联系人的免密支付立即失效，重新添加后要再次绑定亲密关系才恢复。`,
      confirmButtonText: '删除', confirmButtonColor: 'var(--brand)', teleport: 'body',
    })
  } catch { return }
  pick({ type: 'contact_delete', contact_id: c.id }, '删除联系人', `${who(c)} · ${c.bank} 尾号 ${c.tail}`)
}

const bindFor = ref(null)
const bindActions = computed(() => (props.card.intimacy_options || []).map((x) => ({ name: x })))
function onBind(a) {
  const c = bindFor.value
  bindFor.value = null
  pick({ type: 'contact_bind_intimacy', contact_id: c.id, intimacy: a.name }, '绑定亲密关系', `${who(c)} · ${a.name}`)
}

async function unbind(c) {
  try {
    await showConfirmDialog({
      title: '解除亲密关系',
      message: `解除「${who(c)}」的亲密关系（${c.intimacy}）后，免密支付对 TA 立即失效，要重新绑定（人脸或短信验证）才恢复。`,
      confirmButtonText: '解除', confirmButtonColor: 'var(--brand)', teleport: 'body',
    })
  } catch { return }
  pick({ type: 'contact_unbind_intimacy', contact_id: c.id }, '解除亲密关系', `${who(c)} · ${c.intimacy}`)
}
</script>

<template>
  <div class="card contacts" :class="{ closed: done }" data-testid="contacts-card">
    <div class="card-title"><span>我的联系人</span><span class="count">{{ items.length }} 位</span></div>
    <div v-for="c in items" :key="c.id" class="contact" data-testid="contact-item">
      <div class="c-top">
        <span class="c-name"><b v-if="c.relation">{{ c.relation }}</b><span>{{ c.name_masked }}</span></span>
        <van-tag v-if="c.intimacy && c.intimacy_verified" type="success" plain>{{ c.intimacy }} · 已验证</van-tag>
        <van-tag v-else-if="c.intimacy" type="warning" plain>{{ c.intimacy }} · 未验证</van-tag>
      </div>
      <div class="c-sub">{{ c.bank }} 尾号 {{ c.tail }}<template v-if="c.phone_masked"> · {{ c.phone_masked }}</template></div>
      <div v-if="!done" class="c-ops">
        <span class="op" data-testid="contact-edit" @click="openEdit(c)">修改</span>
        <span v-if="c.intimacy" class="op" data-testid="contact-unbind" @click="unbind(c)">解除亲密关系</span>
        <span v-else class="op" data-testid="contact-bind" @click="bindFor = c">绑定亲密关系</span>
        <span class="op del" data-testid="contact-delete" @click="remove(c)">删除</span>
      </div>
    </div>
    <div v-if="!items.length" class="empty">还没有联系人</div>
    <div v-if="done" class="done-mask">已提交，请在下方确认卡里核对</div>
    <van-button v-else block plain size="small" icon="plus" class="add" data-testid="contact-add" @click="openAdd">新增联系人</van-button>
    <div class="meta">绑定亲密关系（父母、伴侣、子女等）后，对 TA 的转账可享受免密支付。每项变更都要你确认后才生效</div>

    <van-popup v-model:show="formOpen" position="bottom" round closeable class="sheet-pop" teleport="body" :style="{ maxHeight: '86%' }">
      <div class="sheet-frame" data-testid="contact-form">
        <div class="sheet-head"><div class="sheet-title">{{ editing ? `修改联系人 · ${who(editing)}` : '新增联系人' }}</div></div>
        <div class="sheet-body">
          <van-cell-group inset class="fields">
            <van-field v-model="form.name" label="姓名" :placeholder="editing ? `${editing.name_masked}，留空表示不改` : '收款人真实姓名'"
                       maxlength="20" :error-message="form.name && errors.name" data-testid="contact-name" />
            <van-field v-model="form.account_no" label="卡号" type="digit" maxlength="19"
                       :placeholder="editing ? `尾号 ${editing.tail}，留空表示不改` : '12–19 位数字'"
                       :error-message="form.account_no && errors.account_no" data-testid="contact-account" />
            <van-field v-model="form.phone" label="手机号" type="tel" maxlength="11"
                       :placeholder="editing ? (editing.phone_masked ? `${editing.phone_masked}，留空表示不改` : '未填，可补充') : '选填'"
                       :error-message="form.phone && errors.phone" data-testid="contact-phone" />
            <van-field v-model="form.relation" label="称呼" maxlength="10" placeholder="选填，如 妈妈、同事" data-testid="contact-relation" />
          </van-cell-group>
          <div class="f-sec">开户行</div>
          <div class="bank-chips">
            <span v-for="b in banks" :key="b" class="bank-chip" :class="{ on: b === form.bank }" @click="form.bank = b">{{ b }}</span>
          </div>
          <div v-if="editing" class="f-note">改了卡号后，下次转账按新收款人处理，亲密关系也要重新绑定。</div>
          <van-button type="danger" round block :disabled="!!formProblem" data-testid="contact-submit" @click="submitForm">
            {{ formProblem || (editing ? '提交修改' : '提交新增') }}
          </van-button>
          <div class="f-note">提交后会出一张确认卡，你确认后才保存。</div>
        </div>
      </div>
    </van-popup>

    <van-action-sheet :show="!!bindFor" :actions="bindActions" cancel-text="取消" teleport="body"
                      :description="bindFor ? `为「${who(bindFor)}」绑定亲密关系。需要人脸识别，也可以改用短信验证码` : ''"
                      @update:show="(v) => { if (!v) bindFor = null }" @select="onBind" />
  </div>
</template>

<style scoped>
.card.contacts .card-title .count { font-size: var(--fs-xs); font-weight: 400; color: var(--muted); }
.contact { padding: 8px 0; }
.contact + .contact { border-top: 1px solid var(--line-soft); }
.c-top { display: flex; justify-content: space-between; align-items: center; gap: var(--space-2); }
.c-name { display: flex; gap: 6px; align-items: baseline; font-size: var(--fs-body); min-width: 0; }
.c-name b { font-weight: 600; }
.c-name span { color: var(--ink-2); }
.c-sub { font-size: var(--fs-xs); color: var(--muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
.c-ops { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 4px; }
.op { font-size: var(--fs-xs); color: var(--info); cursor: pointer; line-height: 22px; }
.op.del { color: var(--red-ink); margin-left: auto; }
.empty { font-size: var(--fs-sm); color: var(--muted); text-align: center; padding: 12px 0; }
.add { margin-top: 10px; }
.fields { margin: 4px 0 0; }
.fields.van-cell-group--inset { margin: 4px 0 0; background: var(--surface-2); }
.fields :deep(.van-cell) { background: transparent; }
.fields :deep(.van-field__label) { width: 4.2em; }
.f-sec { font-size: var(--fs-sm); color: var(--muted); margin: var(--space-3) 0 6px; }
.bank-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: var(--space-4); }
.bank-chip { font-size: var(--fs-xs); line-height: 18px; padding: 6px 12px; border-radius: var(--radius-pill); background: var(--surface-2); color: var(--ink); cursor: pointer; }
.bank-chip.on { background: var(--brand); color: var(--on-brand); }
.f-note { font-size: var(--fs-xs); color: var(--muted); margin: var(--space-2) 0; line-height: 1.6; }
</style>
