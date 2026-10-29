<script setup>
// 只展示、不发起动作的卡片：余额、回单、提示、澄清
defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])
</script>

<template>
  <div v-if="card.type === 'balance'" class="card">
    <div class="card-title"><span>账户余额</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
    <div v-for="a in card.accounts" :key="a.tail" class="row">
      <span class="k">{{ a.name }}（尾号 {{ a.tail }}）</span><span class="v">¥{{ a.balance }}</span>
    </div>
    <div class="row total"><span class="k">合计</span><span class="money">¥{{ card.total }}</span></div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
  </div>

  <div v-else-if="card.type === 'receipt'" class="card receipt">
    <div class="watermark">{{ card.watermark }}</div>
    <div class="status" :class="card.status">
      <van-icon :name="card.status === 'succeeded' ? 'checked' : 'warning'" size="28" />
      <span>{{ card.status_label }}</span>
    </div>
    <div class="money center">¥{{ card.amount }}</div>
    <div class="row"><span class="k">收款人</span><span class="v">{{ card.payee }} · {{ card.bank }} 尾号 {{ card.tail }}</span></div>
    <div v-if="card.memo" class="row"><span class="k">备注</span><span class="v">{{ card.memo }}</span></div>
    <div class="row"><span class="k">流水号</span><span class="v">{{ card.txn_no }}</span></div>
    <div class="row"><span class="k">时间</span><span class="v">{{ card.time }}</span></div>
    <div v-if="card.balance_after" class="row"><span class="k">转账后余额</span><span class="v">¥{{ card.balance_after }}</span></div>
    <div class="meta">回执由交易系统签发，非 AI 生成 · {{ card.source }}</div>
  </div>

  <div v-else-if="card.type === 'clarify'" class="card">
    <div class="card-title"><span>{{ card.question }}</span></div>
    <van-cell v-for="o in card.options" :key="JSON.stringify(o.selection)" :title="o.label" :label="o.detail"
              is-link @click="emit('select', o)" />
  </div>

  <div v-else-if="card.type === 'notice'" class="card notice" :class="card.level">
    <van-icon :name="card.level === 'error' ? 'close' : card.level === 'warning' ? 'warning-o' : 'info-o'" />
    <span>{{ card.text }}</span>
  </div>
</template>

<style scoped>
.total { border-top: 1px dashed var(--line); margin-top: 4px; padding-top: 8px; align-items: center; }
.receipt { border-top: 3px solid var(--green); }
.watermark {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: rgba(200, 16, 46, 0.08); font-size: 26px; font-weight: 800; transform: rotate(-18deg); pointer-events: none;
}
.status { display: flex; align-items: center; justify-content: center; gap: 6px; font-weight: 600; font-size: 16px; color: var(--green); }
.status:not(.succeeded) { color: var(--yellow); }
.center { text-align: center; margin: 6px 0 8px; }
.notice { display: flex; gap: 8px; align-items: flex-start; font-size: 14px; line-height: 1.5; background: #f4f6fa; }
.notice.warning { background: var(--yellow-soft); color: #874d00; }
.notice.error { background: var(--red-soft); color: var(--red); }
</style>
