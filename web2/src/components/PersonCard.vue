<script setup lang="ts">
defineProps<{
  personId?: string | number
  image?: string
  name?: string
  role?: string
}>()
</script>

<template>
  <div class="person-card">
    <div class="person-image-wrap">
      <img
        v-if="image"
        :src="image"
        :alt="name"
        class="person-image"
        @error="($event.target as HTMLImageElement).style.display = 'none'"
      />
      <div v-else class="person-placeholder">
        <el-icon :size="28"><User /></el-icon>
      </div>
    </div>
    <h3 class="person-name">{{ name }}</h3>
    <div class="person-role">{{ role }}</div>
  </div>
</template>

<style scoped>
.person-card {
  text-align: center;
  padding: 16px 12px;
  border-radius: 8px;
  background-color: var(--el-bg-color);
  cursor: pointer;
  transition: transform 0.15s;
}
.person-card:hover {
  transform: translateY(-2px);
}
.person-image-wrap {
  width: 80px;
  height: 80px;
  margin: 0 auto 12px;
  border-radius: 50%;
  overflow: hidden;
  background-color: var(--el-fill-color-light);
}
.person-image {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.person-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-placeholder);
}
.person-name {
  margin: 0 0 4px;
  font-size: 14px;
  line-height: 1.3;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.person-role {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
