<script setup>
// 绑定新卡：输入卡号后四位，或在演示卡池里点选一张。选好后转成 selection 回到对话，后端出绑卡确认卡（🔴 人脸 + 短信），
// 和说「绑定尾号1234的卡」走同一个提议工具。卡池只有掩码卡号，完整卡号不下发。
import { computed, ref } from 'vue'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])

const tail = ref('')
const done = ref(false)
const all = ref(false)
const FOLD = 6
const list = computed(() => (all.value ? props.card.candidates : props.card.candidates.slice(0, FOLD)))

function pick(selection, label, detail) {
  if (done.value) return
  done.value = true
  emit('select', { label, detail, selection })
}

function byTail() {
  if (tail.value.length === 4) pick({ type: 'card_bind', tail: tail.value }, `绑定尾号${tail.value}的卡`, '输入后四位')
}
</script>

<template>
  <div class="card" :class="{ closed: done }" data-testid="card-bind-card">
    <div class="card-title">
      <span>绑定新卡</span>
      <van-tag type="danger" plain><IconText text="🔴 人脸 + 短信" /></van-tag>
    </div>
    <div class="count">已绑定 <b>{{ card.count }}</b> / {{ card.max }} 张</div>
    <template v-if="!done">
      <div class="tail-input">
        <van-field v-model="tail" type="digit" maxlength="4" placeholder="输入卡号后四位" data-testid="card-bind-tail"
                   @keyup.enter="byTail" />
        <van-button size="small" type="danger" :disabled="tail.length !== 4" @click="byTail">查找</van-button>
      </div>
      <div class="sec">{{ card.tail ? `尾号${card.tail}的卡` : '你名下可绑定的卡（演示卡池）' }}</div>
      <div v-for="p in list" :key="p.id" class="pool" data-testid="card-bind-option"
           @click="pick({ type: 'card_bind', pool_id: p.id }, `绑定${p.bank}${p.kind_label}`, `尾号${p.tail}`)">
        <div class="bank">
          <span>{{ p.bank }}</span>
          <van-tag plain :type="p.kind === 'credit' ? 'primary' : 'success'">{{ p.card_class }}</van-tag>
        </div>
        <div class="no">{{ p.card_no_masked }}</div>
      </div>
      <div v-if="card.candidates.length > FOLD" class="toggle" @click="all = !all">
        {{ all ? '收起' : `展开全部 ${card.candidates.length} 张` }}
      </div>
    </template>
    <div v-else class="done-mask">已提交，请在下方确认卡里核对</div>
    <div class="meta">{{ card.note }}</div>
  </div>
</template>

<style scoped>
.count { font-size: var(--fs-sm); color: var(--muted); }
.count b { color: var(--ink); }
.tail-input { display: flex; align-items: center; gap: var(--space-2); margin-top: 10px; }
.tail-input .van-field { flex: 1; background: var(--surface-2); border-radius: var(--radius-sm); padding: 6px 10px; }
.sec { font-size: var(--fs-sm); font-weight: 600; margin: 14px 0 6px; }
.pool { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 8px 12px; cursor: pointer; transition: border-color .15s, background .15s; }
.pool + .pool { margin-top: 6px; }
.pool:active { border-color: var(--brand); background: var(--brand-soft); }
.bank { display: flex; justify-content: space-between; align-items: center; gap: var(--space-2); font-size: var(--fs-body); font-weight: 600; }
.no { font-size: var(--fs-sm); color: var(--muted); margin-top: 2px; font-variant-numeric: tabular-nums; letter-spacing: 0.5px; }
.toggle { text-align: center; font-size: var(--fs-sm); color: var(--info); padding: 8px 0 0; cursor: pointer; }
</style>
