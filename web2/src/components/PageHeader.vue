<script setup lang="ts">
defineProps<{
  title: string
  description?: string
}>()
</script>

<template>
  <div class="page-header">
    <div class="header-left">
      <h2 class="page-title">{{ title }}</h2>
      <span v-if="description" class="page-desc">{{ description }}</span>
    </div>
    <div class="header-right">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}
.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}
.page-desc {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-left: 8px;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
