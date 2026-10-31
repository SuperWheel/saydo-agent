<script setup>
// 卡片列表（查卡结果，以及「锁哪张卡」追问时的候选）。卡号只有尾号，内容全部来自服务端。
defineProps({ card: { type: Object, required: true } })

const STATUS_TAG = { active: 'success', locked: 'warning', lost: 'danger' }
</script>

<template>
  <div class="card" data-testid="card-list-card">
    <div class="card-title"><span>我的银行卡</span><van-tag plain type="success"><IconText text="🟢 自动查询" /></van-tag></div>
    <div v-if="card.hint" class="hint">{{ card.hint }}</div>
    <div v-for="c in card.cards" :key="c.kind + c.tail" class="bank-card">
      <div class="face" :class="[c.kind, c.status]">
        <div class="face-top">
          <span class="name">{{ c.name }}</span>
          <van-tag :type="STATUS_TAG[c.status]" size="medium">{{ c.status_label }}</van-tag>
        </div>
        <div class="no">•••• •••• •••• {{ c.tail }}</div>
        <div class="kind">{{ c.kind_label }}</div>
      </div>
      <div class="grid">
        <div v-for="[k, v] in c.rows" :key="k" class="cell">
          <span class="k">{{ k }}</span>
          <span class="v" :class="{ off: v === '已关闭' }">{{ v }}</span>
        </div>
      </div>
    </div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }} · 锁卡、挂失、调整限额直接告诉我</div>
  </div>
</template>

<style scoped>
.hint { background: var(--yellow-soft); color: var(--yellow-ink); border-radius: var(--radius-sm); padding: 8px 10px; font-size: var(--fs-sm); line-height: 1.5; margin-bottom: 8px; }
.bank-card + .bank-card { margin-top: 12px; }
.face { border-radius: var(--radius-md); padding: 10px 12px; color: var(--on-brand); background: linear-gradient(135deg, #c8102e, #8a0b20); }
.face.credit { background: linear-gradient(135deg, #3a3f4b, #1d2330); }
.face.virtual { background: linear-gradient(135deg, #3b6fd4, #23468f); }
.face.locked, .face.lost { background: linear-gradient(135deg, #8d929c, #5f646e); }
.face-top { display: flex; justify-content: space-between; align-items: center; font-weight: 600; font-size: var(--fs-body); }
.no { font-size: var(--fs-sheet); letter-spacing: 1px; margin: 10px 0 2px; font-variant-numeric: tabular-nums; }
.kind { font-size: var(--fs-xs); opacity: 0.8; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 12px; margin-top: 8px; }
.cell { display: flex; justify-content: space-between; gap: 6px; font-size: var(--fs-sm); padding: 3px 0; min-width: 0; }
.cell .k { color: var(--muted); flex-shrink: 0; }
.cell .v { text-align: right; word-break: break-all; }
.cell .v.off { color: var(--muted); }
</style>
