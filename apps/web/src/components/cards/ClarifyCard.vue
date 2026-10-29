<script setup>
defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])
</script>

<template>
  <div class="card">
    <div class="card-title"><span>{{ card.question }}</span></div>
    <van-cell v-for="o in card.options" :key="JSON.stringify(o.selection)" :title="o.label" :label="o.detail"
              is-link @click="emit('select', o)" />
  </div>
</template>
