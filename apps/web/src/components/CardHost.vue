<script setup>
// 卡片注册表：后端卡片的 type → 前端组件。新场景加卡片：在 cards/ 下新建组件，再在 CARDS 里加一行。
// 组件统一接收 card；需要回传结果的组件按需发出 outcome（确认 / 验证结果）、modify、select（澄清选项）。
import AACard from './cards/AACard.vue'
import AnomalyCard from './cards/AnomalyCard.vue'
import AuthCard from './cards/AuthCard.vue'
import BalanceCard from './cards/BalanceCard.vue'
import CardListCard from './cards/CardListCard.vue'
import CategoryCard from './cards/CategoryCard.vue'
import ClarifyCard from './cards/ClarifyCard.vue'
import ConfirmCard from './cards/ConfirmCard.vue'
import NoticeCard from './cards/NoticeCard.vue'
import AutomationCard from './cards/AutomationCard.vue'
import PlanCard from './cards/PlanCard.vue'
import ReceiptCard from './cards/ReceiptCard.vue'
import ReportCard from './cards/ReportCard.vue'
import ShopOptionsCard from './cards/ShopOptionsCard.vue'
import TxnListCard from './cards/TxnListCard.vue'
import WealthCompareCard from './cards/WealthCompareCard.vue'
import WealthHoldingsCard from './cards/WealthHoldingsCard.vue'
import WealthQuizCard from './cards/WealthQuizCard.vue'
import WealthRecommendCard from './cards/WealthRecommendCard.vue'
import SubGuideCard from './cards/SubGuideCard.vue'
import SubscriptionCard from './cards/SubscriptionCard.vue'
import AnalysisCard from './cards/AnalysisCard.vue'
import KnowledgeCard from './cards/KnowledgeCard.vue'

const CARDS = {
  confirm: ConfirmCard,
  auth: AuthCard,
  balance: BalanceCard,
  receipt: ReceiptCard,
  clarify: ClarifyCard,
  notice: NoticeCard,
  aa: AACard,
  bill_txns: TxnListCard,
  bill_categories: CategoryCard,
  bill_anomalies: AnomalyCard,
  bill_report: ReportCard,
  card_list: CardListCard,
  plan: PlanCard,
  automation: AutomationCard,
  shop_options: ShopOptionsCard,
  wealth_quiz: WealthQuizCard,
  wealth_recommend: WealthRecommendCard,
  wealth_compare: WealthCompareCard,
  wealth_holdings: WealthHoldingsCard,
  sub_overview: SubscriptionCard,
  sub_upcoming: SubscriptionCard,
  sub_guide: SubGuideCard,
  bill_analysis: AnalysisCard,
  knowledge: KnowledgeCard,
}

defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['outcome', 'modify', 'select'])
</script>

<template>
  <component :is="CARDS[card.type]" v-if="CARDS[card.type]" :card="card"
             @outcome="emit('outcome', $event)" @modify="emit('modify')" @select="emit('select', $event)" />
  <NoticeCard v-else :card="{ level: 'warning', text: `这张卡片（${card.type}）当前版本还不能显示` }" />
</template>
