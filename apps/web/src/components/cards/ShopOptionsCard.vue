<script setup>
// 候选商品（买花、订蛋糕、买鞋、买耳机……）：几款不同平台、价位、送达时间的商品，点一款回到对话出订单确认卡，
// 和说「要第二个」走同一条下单流程。价格、平台、送达时间都来自后端报价，这里只负责排版。
defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])
// 平台标签后面跟店名；平台自营店（「京东自营」）店名里已经有平台名，只写「自营」
const shopName = (p) => (p.shop.startsWith(p.platform) ? p.shop.slice(p.platform.length) : p.shop)

function pick(p) {
  emit('select', { label: p.name, detail: `${p.shop_label} · ¥${p.price}`, selection: { type: 'shop_pick', sku: p.sku } })
}
</script>

<template>
  <div class="card shop" data-testid="shop-options-card">
    <div class="card-title"><span>{{ card.category_label }} · 候选 {{ card.items.length }} 款</span></div>
    <div v-for="(p, i) in card.items" :key="p.sku" class="item" data-testid="shop-item" @click="pick(p)">
      <div class="top">
        <span class="name"><i class="rank">{{ i + 1 }}</i>{{ p.name }}</span>
        <span class="price">¥{{ p.price }}</span>
      </div>
      <div class="note">
        <van-tag v-if="i === 0" type="danger" plain>{{ card.badge }}</van-tag>
        <span>{{ p.note }}</span>
      </div>
      <div class="shop-line">
        <span class="platform">{{ p.platform }}</span>
        <span class="shop-name">{{ shopName(p) }}</span>
        <span v-if="p.whitelisted" class="partner">合作商户</span>
      </div>
      <div class="delivery">{{ p.delivery }}</div>
      <div class="more">选这款 ›</div>
    </div>
    <div class="how">{{ card.hint }}</div>
  </div>
</template>

<style scoped>
.item { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 12px; cursor: pointer; transition: border-color .15s, background .15s; }
.item:active { border-color: var(--brand); background: var(--brand-soft); }

.item + .item { margin-top: 8px; }
.top { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-weight: 600; font-size: var(--fs-body); }
.price { color: var(--brand); font-variant-numeric: tabular-nums; flex-shrink: 0; }
.rank { display: inline-block; width: 18px; height: 18px; line-height: 18px; text-align: center; font-style: normal;
  font-size: var(--fs-2xs); border-radius: 50%; background: var(--brand); color: var(--on-brand); margin-right: 6px; }
.note { display: flex; align-items: center; gap: 6px; font-size: var(--fs-xs); color: var(--muted); margin-top: 4px; }
.shop-line { display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm); margin-top: 6px; }
.platform { font-size: var(--fs-2xs); padding: 1px 6px; border-radius: var(--radius-xs); background: var(--brand-soft); color: var(--brand); }
.partner { font-size: var(--fs-2xs); color: var(--muted); border: 1px solid var(--line); border-radius: var(--radius-xs); padding: 0 4px; }
.delivery { font-size: var(--fs-xs); color: var(--muted); margin-top: 4px; }
.more { font-size: var(--fs-xs); color: var(--brand); font-weight: 500; text-align: right; margin-top: 2px; }
.how { font-size: var(--fs-xs); color: var(--muted); margin-top: 8px; }
</style>
