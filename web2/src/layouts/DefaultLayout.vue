<script setup lang="ts">
import AppSidebar from '@/components/AppSidebar.vue'
import AppHeader from '@/components/AppHeader.vue'
import GlobalOverlays from '@/components/GlobalOverlays.vue'
</script>

<template>
  <el-container class="app-layout">
    <el-aside class="app-aside" :width="'var(--app-aside-width)'">
      <AppSidebar />
    </el-aside>
    <el-container>
      <el-header class="app-header">
        <AppHeader />
      </el-header>
      <el-main class="app-main">
        <RouterView v-slot="{ Component }">
          <component :is="Component" />
        </RouterView>
      </el-main>
    </el-container>
    <GlobalOverlays />
  </el-container>
</template>
