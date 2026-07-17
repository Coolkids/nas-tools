<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const keyword = ref('')

function onSearch() {
  const q = keyword.value.trim()
  if (!q) return
  router.push({ path: '/search', query: { q } })
}
</script>

<template>
  <el-input
    v-model="keyword"
    class="app-searchbar"
    placeholder="搜索资源..."
    clearable
    @keyup.enter="onSearch"
  >
    <template #prefix>
      <el-icon><Search /></el-icon>
    </template>
    <template #append>
      <el-button @click="onSearch">搜索</el-button>
    </template>
  </el-input>
</template>

<style scoped>
.app-searchbar {
  max-width: 520px;
  width: 100%;
}
</style>
