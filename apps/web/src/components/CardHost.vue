<script setup>
// 卡片注册表：后端卡片的 type → 前端组件。新场景加卡片：在 cards/ 下新建组件，再在 CARDS 里加一行。
// 组件统一接收 card；需要回传结果的组件按需发出 outcome（确认 / 验证结果）、modify、select（澄清选项）。
import AuthCard from './cards/AuthCard.vue'
import BalanceCard from './cards/BalanceCard.vue'
import ClarifyCard from './cards/ClarifyCard.vue'
import ConfirmCard from './cards/ConfirmCard.vue'
import NoticeCard from './cards/NoticeCard.vue'
import ReceiptCard from './cards/ReceiptCard.vue'

const CARDS = {
  confirm: ConfirmCard,
  auth: AuthCard,
  balance: BalanceCard,
  receipt: ReceiptCard,
  clarify: ClarifyCard,
  notice: NoticeCard,
}

defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['outcome', 'modify', 'select'])
</script>

<template>
  <component :is="CARDS[card.type]" v-if="CARDS[card.type]" :card="card"
             @outcome="emit('outcome', $event)" @modify="emit('modify')" @select="emit('select', $event)" />
  <NoticeCard v-else :card="{ level: 'warning', text: `这张卡片（${card.type}）当前版本还不能显示` }" />
</template>
