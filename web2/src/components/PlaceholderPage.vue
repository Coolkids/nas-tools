<script setup lang="ts">
defineProps<{ title?: string }>()
</script>

<template>
  <div class="page-placeholder">
    <el-empty :description="title ? `页面建设中：${title}` : '页面建设中'" />
  </div>
</template>
