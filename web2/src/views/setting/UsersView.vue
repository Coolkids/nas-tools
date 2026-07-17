<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import { doAction } from '@/api'

interface UserItem {
  id: number
  name: string
  pris: string[]
}

interface GetUsersResult {
  code: number
  result?: UserItem[]
}

const ALL_PRIS = [
  '我的媒体库',
  '资源搜索',
  '探索',
  '站点管理',
  '订阅管理',
  '下载管理',
  '媒体整理',
  '服务',
  '系统设置'
]

const modal = useModalStore()

const users = ref<UserItem[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const submitting = ref(false)
const form = reactive({
  name: '',
  password: '',
  pris: ['资源搜索', '探索'] as string[]
})

async function loadUsers() {
  loading.value = true
  try {
    const res = await doAction<GetUsersResult>('get_users', {})
    if (res.code === 0 && Array.isArray(res.result)) {
      users.value = res.result
    }
  } catch {
    modal.error('获取用户列表失败')
  } finally {
    loading.value = false
  }
}

function openDialog() {
  form.name = ''
  form.password = ''
  form.pris = ['资源搜索', '探索']
  dialogVisible.value = true
}

async function handleAdd() {
  if (!form.name) {
    modal.warning('请输入用户名')
    return
  }
  if (!form.password) {
    modal.warning('请输入密码')
    return
  }
  if (form.pris.length === 0) {
    modal.warning('请选择权限')
    return
  }
  submitting.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('user_manager', {
      oper: 'add',
      name: form.name,
      password: form.password,
      pris: form.pris
    })
    if (res.code === 0) {
      modal.success('添加成功')
      dialogVisible.value = false
      await loadUsers()
    } else {
      modal.error(res.msg || '添加失败')
    }
  } catch {
    modal.error('添加失败')
  } finally {
    submitting.value = false
  }
}

async function handleDelete(name: string) {
  const ok = await modal.confirm(`确定删除用户 "${name}" ？`)
  if (!ok) return
  try {
    const res = await doAction<{ code: number; msg?: string }>('user_manager', {
      oper: 'del',
      name
    })
    if (res.code === 0) {
      modal.success('删除成功')
      await loadUsers()
    } else {
      modal.error(res.msg || '删除失败')
    }
  } catch {
    modal.error('删除失败')
  }
}

onMounted(loadUsers)
</script>

<template>
  <div v-loading="loading" class="users-view">
    <PageHeader title="用户管理" description="管理系统用户及访问权限">
      <template #actions>
        <el-button type="primary" :icon="Plus" @click="openDialog">新增用户</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <template #header>
        <span class="text-muted">共 {{ users.length }} 条记录</span>
      </template>
      <el-table :data="users" empty-text="没有数据">
        <el-table-column prop="name" label="用户名" width="180" />
        <el-table-column label="权限">
          <template #default="{ row }">
            <el-tag
              v-for="pri in row.pris"
              :key="pri"
              size="small"
              class="pri-tag"
              effect="plain"
            >
              {{ pri }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="" width="80" align="right">
          <template #default="{ row }">
            <el-button :icon="Delete" type="danger" link @click="handleDelete(row.name)" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" title="新增用户" width="600px">
      <el-form label-width="80px">
        <div class="form-row">
          <el-form-item label="用户名" required>
            <el-input v-model="form.name" />
          </el-form-item>
          <el-form-item label="密码" required>
            <el-input v-model="form.password" type="password" show-password />
          </el-form-item>
        </div>
        <el-form-item label="权限" required>
          <el-checkbox-group v-model="form.pris" class="pri-group">
            <el-checkbox v-for="p in ALL_PRIS" :key="p" :label="p">{{ p }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleAdd">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.text-muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.pri-tag {
  margin: 0 6px 6px 0;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}
.pri-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
}
</style>
