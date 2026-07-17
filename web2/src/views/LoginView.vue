<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { User, Lock, Film } from '@element-plus/icons-vue'
import { login } from '@/api/auth'

const router = useRouter()
const route = useRoute()
const loading = ref(false)
const form = ref({ username: '', password: '', remember: true })
const errorMsg = ref('')

onMounted(() => {
  form.value.username = ''
  form.value.password = ''
})

async function handleLogin() {
  errorMsg.value = ''
  if (!form.value.username || !form.value.password) {
    errorMsg.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  try {
    const res = await login(form.value.username, form.value.password, form.value.remember)
    if (res.success) {
      const redirect = (route.query.redirect as string) || '/index'
      router.replace(redirect)
    } else {
      errorMsg.value = res.message || '登录失败'
    }
  } catch (e) {
    errorMsg.value = '网络错误，请确认后端服务已启动'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-box">
      <div class="login-logo">
        <el-icon :size="40" color="var(--el-color-primary)"><Film /></el-icon>
        <h1>NAStool</h1>
      </div>
      <el-form @submit.prevent="handleLogin" size="large">
        <el-form-item>
          <el-input
            v-model="form.username"
            placeholder="用户名"
            :prefix-icon="User"
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            show-password
            :prefix-icon="Lock"
            @keyup.enter="handleLogin"
          />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.remember">记住我</el-checkbox>
        </el-form-item>
        <el-alert v-if="errorMsg" :title="errorMsg" type="error" :closable="false" show-icon style="margin-bottom: 12px" />
        <el-button type="primary" :loading="loading" style="width: 100%" @click="handleLogin">登录</el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
}
.login-box {
  width: 360px;
  background: var(--el-bg-color);
  border-radius: 12px;
  padding: 40px 32px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.login-logo {
  text-align: center;
  margin-bottom: 24px;
}
.login-logo h1 {
  margin: 12px 0 0;
  font-size: 24px;
  color: var(--el-text-color-primary);
}
</style>