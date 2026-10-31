<script setup>
// 知识问答（knowledge，WS9）：知识库检索到的条文，每条附出处（类别 · 文档 · 条款 · 依据 · 版本 · 生效日期）。
// 监管类条目另带官方原文链接（urls），只在卡片上显示。
// 知识库回答「规则是什么」；条文只是参考资料，不触发任何办理。
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card" data-testid="knowledge-card">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="primary"><IconText text="📖 知识库" /></van-tag></div>
    <div v-for="(it, i) in card.items" :key="it.id" class="item" :class="{ first: i === 0 }">
      <div class="q">{{ it.title }}</div>
      <div class="text">{{ it.text }}</div>
      <div class="meta">出处：{{ it.source }}</div>
      <div v-if="it.urls?.length" class="links">
        <a v-for="(u, j) in it.urls" :key="u" :href="u" target="_blank" rel="noopener noreferrer">官方原文{{ it.urls.length > 1 ? j + 1 : '' }}</a>
      </div>
    </div>
    <div class="disclaimer">{{ card.disclaimer }}</div>
  </div>
</template>

<style scoped>
.item { padding: 8px 0; border-top: 1px solid var(--line); }
.item.first { border-top: none; padding-top: 2px; }
.q { font-weight: 600; font-size: var(--fs-body); margin-bottom: 4px; }
.text { font-size: var(--fs-sm); line-height: 1.6; }
.links { display: flex; gap: 12px; margin-top: 2px; font-size: var(--fs-xs); }
.links a { color: var(--brand); }
.disclaimer { color: var(--muted); font-size: var(--fs-xs); margin-top: 6px; padding-top: 6px; border-top: 1px dashed var(--line); }
</style>
