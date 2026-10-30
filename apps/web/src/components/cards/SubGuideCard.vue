<script setup>
// 商户侧取消指引（sub_guide）。本行只能停止代扣授权；商户那边的订阅要用户自己取消，卡片上把两件事分开写。
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="primary">ℹ️ 只提供指引</van-tag></div>
    <div class="merchant">{{ card.merchant }}</div>
    <div class="part">
      <div class="label">本行代扣授权</div>
      <div>{{ card.bank_part }}</div>
    </div>
    <div class="part">
      <div class="label">商户侧订阅（需要你自己取消）· {{ card.where }}</div>
      <ol class="steps"><li v-for="(s, i) in card.steps" :key="i">{{ s }}</li></ol>
      <div v-for="(t, i) in card.tips" :key="'t' + i" class="tip">· {{ t }}</div>
    </div>
    <div class="warn">{{ card.warning }}</div>
    <div v-if="card.source" class="meta">出处：{{ card.source }}{{ card.updated ? ` · ${card.updated}` : '' }}</div>
  </div>
</template>

<style scoped>
.merchant { font-weight: 600; font-size: 15px; margin-bottom: 6px; }
.part { font-size: 13px; line-height: 1.6; padding: 6px 0; border-top: 1px solid var(--line); }
.label { color: var(--muted); font-size: 12px; }
.steps { margin: 2px 0 0; padding-left: 18px; }
.tip { color: var(--muted); font-size: 12px; }
.warn { background: var(--yellow-soft); color: #874d00; border-radius: 8px; padding: 8px 10px; font-size: 13px; line-height: 1.5; margin-top: 6px; }
</style>
