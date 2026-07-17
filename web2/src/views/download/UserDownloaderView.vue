<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Edit, Delete, Plus } from '@element-plus/icons-vue'
import { doAction, postJson } from '@/api'
import { useModalStore } from '@/stores/modal'
import PageHeader from '@/components/PageHeader.vue'

interface UserDownloader {
  id: number
  name: string
  type: string
  host: string
  port: number | string
  username: string
  password: string
  save_dir: string
}

const modal = useModalStore()
const list = ref<UserDownloader[]>([])
const loading = ref(false)

const DOWNLOADER_TYPES = [
  { value: 'qbittorrent', label: 'Qbittorrent' },
  { value: 'transmission', label: 'Transmission' }
]

const dialogVisible = ref(false)
const saving = ref(false)
const testing = ref(false)
const form = reactive({
  id: '' as string | number,
  name: '',
  type: 'qbittorrent',
  host: '',
  port: '' as number | string,
  username: '',
  password: '',
  save_dir: ''
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await postJson<{
      code: number
      data?: { downloaders: UserDownloader[] }
    }>('/api/v1/brushtask/downloader/list')
    if (res.code === 0) {
      list.value = res.data?.downloaders || []
    } else {
      list.value = []
    }
  } finally {
    loading.value = false
  }
}

function typeLabel(v: string) {
  return DOWNLOADER_TYPES.find((d) => d.value === v)?.label || v
}

function openAdd() {
  form.id = ''
  form.name = ''
  form.type = 'qbittorrent'
  form.host = ''
  form.port = ''
  form.username = ''
  form.password = ''
  form.save_dir = ''
  dialogVisible.value = true
}

function openEdit(row: UserDownloader) {
  form.id = row.id
  form.name = row.name
  form.type = row.type
  form.host = row.host
  form.port = row.port
  form.username = row.username
  form.password = row.password
  form.save_dir = row.save_dir
  dialogVisible.value = true
}

async function submit(test = false) {
  if (!form.name) {
    modal.warning('名称不能为空')
    return
  }
  if (!form.host) {
    modal.warning('IP地址或域名不能为空')
    return
  }
  if (!form.port || isNaN(Number(form.port))) {
    modal.warning('端口不正确')
    return
  }
  if (!form.save_dir) {
    modal.warning('保存目录不能为空')
    return
  }
  if (test) {
    testing.value = true
  } else {
    saving.value = true
  }
  try {
    const res = await doAction<{ code: number; msg?: string }>('add_downloader', {
      test: test ? 1 : 0,
      id: form.id,
      name: form.name,
      type: form.type,
      host: form.host,
      port: form.port,
      username: form.username,
      password: form.password,
      save_dir: form.save_dir
    })
    if (test) {
      if (res.code === 0) modal.success('测试成功')
      else modal.error('测试失败')
    } else {
      if (res.code === 0) {
        dialogVisible.value = false
        modal.success('保存成功')
        load()
      } else {
        modal.error(res.msg || '保存失败')
      }
    }
  } finally {
    testing.value = false
    saving.value = false
  }
}

async function remove(row: UserDownloader) {
  const ok = await modal.confirm(`确认删除下载器「${row.name}」？`)
  if (!ok) return
  const res = await doAction<{ code: number }>('delete_downloader', { id: row.id })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  }
}
</script>

<template>
  <div class="user-downloader" v-loading="loading">
    <PageHeader title="自定义下载器" description="管理刷流任务使用的下载器">
      <template #actions>
        <el-button type="primary" :icon="Plus" @click="openAdd">新增下载器</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <el-table :data="list" stripe>
        <el-table-column label="名称" prop="name" min-width="160" />
        <el-table-column label="类型" width="140">
          <template #default="{ row }">
            <el-tag size="small">{{ typeLabel(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="地址" min-width="220">
          <template #default="{ row }">{{ row.host }}:{{ row.port }}</template>
        </el-table-column>
        <el-table-column label="保存目录" prop="save_dir" min-width="200" show-overflow-tooltip />
        <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-button :icon="Edit" link @click="openEdit(row)">编辑</el-button>
            <el-button :icon="Delete" link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="form.id ? '编辑下载器' : '新增下载器'"
      width="640px"
      :close-on-click-modal="false"
    >
      <el-form label-width="110px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="别名" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="类型" required>
              <el-select v-model="form.type">
                <el-option v-for="d in DOWNLOADER_TYPES" :key="d.value" :label="d.label" :value="d.value" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="IP/域名" required>
              <el-input v-model="form.host" placeholder="ip/http/https" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="端口" required>
              <el-input v-model="form.port" placeholder="端口" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="用户名" required>
              <el-input v-model="form.username" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="密码" required>
              <el-input v-model="form.password" type="password" show-password />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="保存目录" required>
          <el-input v-model="form.save_dir" placeholder="下载器使用路径" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :loading="testing" @click="submit(true)">测试</el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit(false)">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.user-downloader {
  padding: 16px;
}
</style>
