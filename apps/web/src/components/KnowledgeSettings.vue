<script setup>
// 知识检索设置：「向量检索」「重排模型」两张卡片，点开分别管理。两项都是可选的，不强制：
// 都不配就用关键词检索（BM25）；只配向量 = 关键词 + 向量混合；只配重排 = 关键词候选 + 重排；两项都配 = 混合候选 + 重排。
// 下面还有一张「没查到时换说法重查」（agentic 重查）开关卡：原话没查到时让对话模型改写问题再查一次，默认开，要对话模型可用。
import { computed, onMounted, ref } from 'vue'
import { showToast } from 'vant'
import { api } from '../api'
import EmbeddingSettings from './EmbeddingSettings.vue'
import RerankSettings from './RerankSettings.vue'

const open = ref('')
const emb = ref(null)
const rr = ref(null)
const ag = ref(null)
const agSaving = ref(false)

onMounted(async () => {
  const [e, r, a] = await Promise.allSettled([api.embeddingConfig(), api.rerankConfig(), api.agenticConfig()])
  if (e.status === 'fulfilled') emb.value = e.value
  if (r.status === 'fulfilled') rr.value = r.value
  if (a.status === 'fulfilled') ag.value = a.value
})

async function toggleAgentic(on) {
  agSaving.value = true
  try {
    ag.value = await api.saveAgenticConfig(on)
    showToast(on ? '已开启：没查到时换个说法再查' : '已关闭：没查到就直接说没查到')
  } catch (e) {
    showToast(e.message)
  } finally {
    agSaving.value = false
  }
}

const pipeline = computed(() => {
  const parts = ['关键词']
  if (emb.value?.ready) parts.push('向量')
  const base = parts.join(' + ')
  const flow = rr.value?.ready ? `${base}检索 → 重排` : `${base}检索`
  return ag.value?.active ? `${flow} → 没查到时换说法重查` : flow
})
const tone = (c) => ({ ready: 'on', off: 'off', idle: 'wait', building: 'wait' })[c?.state] || (c ? 'bad' : 'off')
const embSummary = computed(() => (emb.value?.ready ? `${emb.value.model}${emb.value.dimensions ? ` · ${emb.value.dimensions} 维` : ''}`
  : '能听懂换了说法的问题；不配置也能用'))
const rrSummary = computed(() => (rr.value?.ready ? `${rr.value.model} · 门槛 ${rr.value.min_score}`
  : '给候选逐条打分，答得更准、拒答更稳；不配置也能用'))
const agSummary = computed(() => {
  if (!ag.value) return '读取中…'
  if (!ag.value.llm_ready) return '要先在「模型」页配好对话模型才会生效；规则模式不改写'
  return ag.value.enabled ? `原话没查到时，用 ${ag.value.llm} 把问题改写成书面说法再查一次`
    : '原话没查到就直接说没查到'
})
</script>

<template>
  <div class="kb-settings">
    <div class="intro">
      <b>当前：{{ pipeline }}</b>
      <span>向量检索、重排模型和换说法重查都是<em>可选的</em>，不强制选择：都不配就用关键词检索；可以只配其中一项（只配重排时候选来自关键词检索），也可以都配。</span>
    </div>
    <van-collapse v-model="open" accordion :border="false" class="cards">
      <van-collapse-item name="embedding" class="kcard" :border="false">
        <template #title>
          <div class="head">
            <span class="name">向量检索</span><span class="opt">可选</span>
            <span class="state" :class="tone(emb)">{{ emb?.state_text || '读取中…' }}</span>
          </div>
          <div class="sub">{{ embSummary }}</div>
        </template>
        <EmbeddingSettings v-if="open === 'embedding'" @changed="(c) => (emb = c)" />
      </van-collapse-item>
      <van-collapse-item name="rerank" class="kcard" :border="false">
        <template #title>
          <div class="head">
            <span class="name">重排模型</span><span class="opt">可选</span>
            <span class="state" :class="tone(rr)">{{ rr?.state_text || '读取中…' }}</span>
          </div>
          <div class="sub">{{ rrSummary }}</div>
        </template>
        <RerankSettings v-if="open === 'rerank'" @changed="(c) => (rr = c)" />
      </van-collapse-item>
    </van-collapse>
    <div class="kcard static">
      <div class="head">
        <span class="name">没查到时换说法重查</span><span class="opt">可选</span><span class="opt">agentic</span>
        <van-switch class="sw" :model-value="ag?.enabled" :loading="agSaving" :disabled="!ag" size="20"
                    active-color="var(--brand)" @update:model-value="toggleAgentic" />
      </div>
      <div class="sub">{{ agSummary }}</div>
      <div class="note">只在原话没查到时触发，「查到」的门槛不变；会多等一次模型调用（实测约 2 秒），超时（{{ ag?.timeout_s ?? 6 }} 秒）或出错就照常说没查到。知识卡上会注明是按哪句查到的。</div>
      <div v-for="w in ag?.warnings || []" :key="w" class="warn">{{ w }}</div>
    </div>
  </div>
</template>

<style scoped>
.intro { margin: 0 16px 10px; padding: 10px 12px; border-radius: var(--radius-md); background: var(--surface-2);
  display: flex; flex-direction: column; gap: 4px; }
.intro b { font-size: var(--fs-sm); color: var(--ink); }
.intro span { font-size: var(--fs-xs); color: var(--muted); line-height: 1.5; }
.intro em { font-style: normal; color: var(--ink-2); font-weight: 600; }
.cards { display: flex; flex-direction: column; gap: 10px; }
.kcard { margin: 0 16px; border-radius: var(--radius-md); overflow: hidden; background: var(--surface);
  box-shadow: var(--shadow-card); border: 1px solid var(--line-soft); }
.kcard :deep(.van-collapse-item__content) { padding: 12px 0 8px; background: var(--surface); }
.kcard :deep(.van-cell) { align-items: flex-start; }
.head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.name { font-weight: 600; color: var(--ink); }
.opt { font-size: var(--fs-2xs); color: var(--muted); border: 1px solid var(--line); border-radius: var(--radius-pill);
  padding: 0 6px; line-height: 16px; }
.state { margin-left: auto; font-size: var(--fs-2xs); padding: 1px 8px; border-radius: var(--radius-pill); line-height: 18px; }
.state.on { background: var(--green-soft); color: var(--green-ink); }
.state.off { background: var(--surface-2); color: var(--muted); }
.state.wait { background: var(--yellow-soft); color: var(--yellow-ink); }
.state.bad { background: var(--red-soft); color: var(--red); }
.sub { font-size: var(--fs-xs); color: var(--muted); margin-top: 2px; line-height: 1.5; word-break: break-all; }
.static { margin-top: 10px; padding: 10px 16px 12px; }
.sw { margin-left: auto; }
.note { font-size: var(--fs-2xs); color: var(--muted); margin-top: 6px; line-height: 1.5; }
.warn { font-size: var(--fs-2xs); color: var(--yellow-ink); margin-top: 4px; }
</style>
