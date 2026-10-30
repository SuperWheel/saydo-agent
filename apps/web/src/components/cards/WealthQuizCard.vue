<script setup>
// 风险测评问卷（F-WM-01）：用户逐题点选，答完才能提交。答案只经由选择回传给后端，由系统算分；AI 不能代填。
import { computed, reactive, ref } from 'vue'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])

const answers = reactive({})
const submitted = ref(false)
const done = computed(() => props.card.questions.every((q) => answers[q.id]))
const count = computed(() => props.card.questions.filter((q) => answers[q.id]).length)

function submit() {
  if (!done.value || submitted.value) return
  submitted.value = true
  emit('select', {
    label: '提交风险测评答案',
    detail: `${props.card.questions.length} 题已全部作答`,
    selection: { type: 'risk_answers', qid: props.card.qid, answers: { ...answers } },
  })
}
</script>

<template>
  <div class="card quiz">
    <div class="card-title"><span>风险测评问卷</span><van-tag plain type="primary">本人作答</van-tag></div>
    <div v-if="card.reason" class="reason">{{ card.reason }}</div>
    <div v-else-if="card.profile" class="profile">
      当前：{{ card.profile.level }}（{{ card.profile.valid ? `有效期至 ${card.profile.expires_on}` : `已于 ${card.profile.expires_on} 过期` }}）
    </div>
    <div v-for="(q, i) in card.questions" :key="q.id" class="q">
      <div class="qt">{{ i + 1 }}. {{ q.text }}</div>
      <div class="opts">
        <button v-for="o in q.options" :key="o.id" type="button" class="opt"
                :class="{ on: answers[q.id] === o.id }" :disabled="submitted" @click="answers[q.id] = o.id">
          {{ o.label }}
        </button>
      </div>
    </div>
    <van-button block size="small" type="danger" :disabled="!done || submitted" class="submit" @click="submit">
      {{ submitted ? '已提交，等待确认' : done ? '提交答案' : `还差 ${card.questions.length - count} 题` }}
    </van-button>
    <div class="meta">{{ card.note }}</div>
    <div class="meta">{{ card.disclaimer }}</div>
  </div>
</template>

<style scoped>
.quiz { border-top: 3px solid var(--brand); }
.reason { background: var(--yellow-soft); color: #874d00; border-radius: 8px; padding: 8px 10px; font-size: 13px; line-height: 1.5; margin-bottom: 8px; }
.profile { color: var(--muted); font-size: 13px; margin-bottom: 6px; }
.q + .q { margin-top: 10px; }
.qt { font-size: 14px; font-weight: 600; margin-bottom: 6px; }
.opts { display: flex; flex-wrap: wrap; gap: 6px; }
.opt { border: 1px solid var(--line); background: #fff; color: var(--text); border-radius: 16px; padding: 5px 11px; font-size: 13px; cursor: pointer; }
.opt.on { border-color: var(--brand); background: var(--brand-soft); color: var(--brand); font-weight: 600; }
.opt:disabled { cursor: default; }
.submit { margin-top: 12px; }
</style>
