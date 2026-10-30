<script setup>
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card" data-testid="balance-card">
    <div class="card-title"><span>账户余额</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
    <div v-for="a in card.accounts" :key="a.tail" class="row">
      <span class="k">{{ a.name }}（尾号 {{ a.tail }}）</span><span class="v">¥{{ a.balance }}</span>
    </div>
    <div class="row total"><span class="k">合计</span><span class="money">¥{{ card.total }}</span></div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.total { border-top: 1px dashed var(--line); margin-top: 4px; padding-top: 8px; align-items: center; }
</style>
