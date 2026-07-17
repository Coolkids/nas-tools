<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { logout } from '@/api/auth'
import { doAction } from '@/api'
import { useModalStore } from '@/stores/modal'
import AppSearchbar from '@/components/AppSearchbar.vue'

const appVersion = ref('')
const router = useRouter()
const modal = useModalStore()

onMounted(async () => {
  try {
    const res = await doAction<{ code: number; version: string }>('version')
    if (res.code === 0 && res.version) {
      appVersion.value = res.version
    }
  } catch {
    // 忽略
  }
})

async function handleLogout() {
  const ok = await modal.confirm('确定要退出登录吗？', '退出登录')
  if (!ok) return
  try {
    await logout()
  } catch {
    // 忽略错误，无论后端是否成功都跳回登录页
  }
  router.replace('/login')
}

function goHome() {
  router.push('/index')
}
</script>

<template>
  <div class="app-header-left" @click="goHome">
    <el-icon :size="22" color="var(--el-color-primary)"><Film /></el-icon>
    <span class="app-logo">NAStool</span>
  </div>
  <div class="app-header-center">
    <AppSearchbar />
  </div>
  <div class="app-header-right">
    <el-dropdown trigger="click">
      <span class="user-trigger">
        <el-icon><User /></el-icon>
        <span>账号</span>
        <el-icon><ArrowDown /></el-icon>
      </span>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item @click="router.push('/users')">用户管理</el-dropdown-item>
          <el-dropdown-item @click="router.push('/basic')">系统设置</el-dropdown-item>
          <el-dropdown-item divided disabled class="version-item">{{ appVersion }}</el-dropdown-item>
          <el-dropdown-item divided @click="handleLogout">退出登录</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </div>
</template>

<style scoped>
.app-header-left {
  cursor: pointer;
}
.user-trigger {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  outline: none;
}
:deep(.version-item) {
  opacity: 0.6;
  cursor: default !important;
  font-size: 12px;
}
</style>
