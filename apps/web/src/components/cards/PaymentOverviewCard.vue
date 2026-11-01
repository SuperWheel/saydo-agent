<script setup>
// 「我的支付授权」：《支付服务协议》签署状态、免密支付设置和适用对象。修改 / 关闭 / 查看协议都回到对话处理。
import { computed, ref } from 'vue'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])

const done = ref(false)
const m = computed(() => props.card.mandate)
const ag = computed(() => props.card.agreement || {})

function pick(selection, label, detail) {
  if (done.value) return
  done.value = true
  emit('select', { label, detail, selection })
}
const openForm = () => pick({ type: 'mandate_offer', action: 'open' }, m.value ? '修改免密额度' : '开通免密支付', '选默认卡和额度')
const revoke = () => pick({ type: 'mandate_change', action: 'revoke' }, '关闭免密支付', '之后每笔按原规则验证')
const viewAgreement = () => pick({ type: 'payment_agreement', action: 'view' }, '查看《支付服务协议》', ag.value.version || '当前版本')
</script>

<template>
  <div class="card pay" :class="{ closed: done }" data-testid="payment-overview-card">
    <div class="card-title"><span>我的支付授权</span><van-tag plain type="success"><IconText text="🟢 自动查询" /></van-tag></div>

    <div class="sec">支付服务协议</div>
    <div class="row">
      <span class="k">状态</span>
      <span class="v">
        <van-tag v-if="ag.signed" plain type="success">已签署</van-tag>
        <van-tag v-else plain type="warning">未签署</van-tag>
      </span>
    </div>
    <div v-if="ag.signed" class="row"><span class="k">签署时间</span><span class="v">{{ ag.signed_at }} · 版本 {{ ag.version }}</span></div>
    <div v-else class="hint">首次付款时会请您阅读并同意</div>

    <div class="sec">免密支付</div>
    <template v-if="m">
      <div class="row"><span class="k">默认付款卡</span><span class="v">{{ m.card_label }}</span></div>
      <div class="row"><span class="k">单笔额度</span><span class="v">¥{{ m.single_max }}</span></div>
      <div class="row"><span class="k">每日额度</span><span class="v">¥{{ m.daily_max }}</span></div>
      <div class="row"><span class="k">今日已用</span><span class="v">¥{{ m.used_today }}</span></div>
      <div class="row"><span class="k">有效期至</span><span class="v">{{ m.valid_until }}</span></div>
    </template>
    <div v-else class="hint">未开通。开通后，对亲密联系人和合作商户的小额付款只需点确认，不用刷脸和输验证码</div>

    <template v-if="m">
      <div class="sec">适用对象</div>
      <div class="scope">
        <span v-for="c in card.scope?.contacts || []" :key="c.id" class="chip">{{ c.label }}<em>{{ c.intimacy }}</em></span>
        <span v-for="x in card.scope?.merchants || []" :key="x" class="chip">{{ x }}</span>
        <span v-if="!card.scope?.contacts?.length && !card.scope?.merchants?.length" class="none">暂无</span>
      </div>
    </template>

    <div v-if="done" class="done-mask">已提交，请看下方</div>
    <template v-else>
      <div class="btns">
        <van-button v-if="m" size="small" data-testid="overview-revoke" @click="revoke">关闭免密支付</van-button>
        <van-button size="small" type="danger" data-testid="overview-open" @click="openForm">{{ m ? '修改额度' : '开通免密支付' }}</van-button>
      </div>
      <div class="link" data-testid="overview-agreement" @click="viewAgreement">查看《支付服务协议》</div>
    </template>
  </div>
</template>

<style scoped>
.sec { font-size: var(--fs-sm); font-weight: 600; margin: 12px 0 2px; }
.card-title + .sec { margin-top: 0; }
.hint { font-size: var(--fs-xs); color: var(--muted); line-height: 1.6; background: var(--surface-2); border-radius: var(--radius-sm); padding: 6px 10px; margin-top: 4px; }
.scope { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.chip { font-size: var(--fs-xs); line-height: 18px; padding: 2px 8px; border-radius: var(--radius-pill); background: var(--surface-2); }
.chip em { font-style: normal; color: var(--green-ink); margin-left: 4px; }
.none { font-size: var(--fs-xs); color: var(--muted); }
.link { text-align: center; font-size: var(--fs-sm); color: var(--info); padding-top: 10px; cursor: pointer; }
</style>
