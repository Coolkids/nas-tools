<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Edit, Delete, Plus } from '@element-plus/icons-vue'
import { doAction } from '@/api'
import { useModalStore } from '@/stores/modal'
import PageHeader from '@/components/PageHeader.vue'

interface DownloadSetting {
  id: number
  name: string
  category: string
  tags: string
  content_layout: number
  is_paused: number
  upload_limit: number
  download_limit: number
  ratio_limit: number
  seeding_time_limit: number
  downloader: string
}

const modal = useModalStore()
const list = ref<DownloadSetting[]>([])
const loading = ref(false)

const CONTENT_LAYOUTS = [
  { value: 0, label: '全局' },
  { value: 1, label: '原始' },
  { value: 2, label: '创建子文件夹' },
  { value: 3, label: '不建子文件夹' }
]

const DOWNLOADER_TYPES = [
  { value: '', label: '默认' },
  { value: 'Qbittorrent', label: 'Qbittorrent' },
  { value: 'Transmission', label: 'Transmission' }
]

const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({
  sid: '' as string | number,
  name: '',
  category: '',
  tags: '',
  content_layout: 0,
  is_paused: 0,
  upload_limit: 0,
  download_limit: 0,
  ratio_limit: 0,
  seeding_time_limit: 0,
  downloader: ''
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await doAction<{ code: number; data: DownloadSetting[] }>('get_download_setting', {})
    if (res.code === 0) {
      list.value = res.data || []
    }
  } finally {
    loading.value = false
  }
}

function layoutLabel(v: number) {
  return CONTENT_LAYOUTS.find((c) => c.value === v)?.label || '全局'
}

function downloaderLabel(v: string) {
  return DOWNLOADER_TYPES.find((d) => d.value === v)?.label || v || '默认'
}

function openAdd() {
  form.sid = ''
  form.name = ''
  form.category = ''
  form.tags = ''
  form.content_layout = 0
  form.is_paused = 0
  form.upload_limit = 0
  form.download_limit = 0
  form.ratio_limit = 0
  form.seeding_time_limit = 0
  form.downloader = ''
  dialogVisible.value = true
}

function openEdit(row: DownloadSetting) {
  form.sid = row.id
  form.name = row.name
  form.category = row.category || ''
  form.tags = row.tags || ''
  form.content_layout = Number(row.content_layout ?? 0)
  form.is_paused = Number(row.is_paused ?? 0)
  form.upload_limit = Number(row.upload_limit ?? 0)
  form.download_limit = Number(row.download_limit ?? 0)
  form.ratio_limit = Number(row.ratio_limit ?? 0)
  form.seeding_time_limit = Number(row.seeding_time_limit ?? 0)
  form.downloader = row.downloader || ''
  dialogVisible.value = true
}

async function submit() {
  if (!form.name) {
    modal.warning('名称不能为空')
    return
  }
  saving.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('update_download_setting', {
      sid: form.sid,
      name: form.name,
      category: form.category,
      tags: form.tags,
      content_layout: form.content_layout,
      is_paused: form.is_paused,
      upload_limit: form.upload_limit,
      download_limit: form.download_limit,
      ratio_limit: form.ratio_limit,
      seeding_time_limit: form.seeding_time_limit,
      downloader: form.downloader
    })
    if (res.code === 0) {
      dialogVisible.value = false
      modal.success('保存成功')
      load()
    } else {
      modal.error(res.msg || '保存失败')
    }
  } finally {
    saving.value = false
  }
}

async function remove(row: DownloadSetting) {
  const ok = await modal.confirm(`确认删除下载设置「${row.name}」？`)
  if (!ok) return
  const res = await doAction<{ code: number }>('delete_download_setting', { sid: row.id })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  }
}
</script>

<template>
  <div class="download-setting" v-loading="loading">
    <PageHeader title="下载设置" description="管理下载任务的默认参数">
      <template #actions>
        <el-button type="primary" :icon="Plus" @click="openAdd">新增下载设置</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <el-table :data="list" stripe>
        <el-table-column label="名称" min-width="140">
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <el-tag v-if="row.id === -1" size="small" type="info" style="margin-left: 6px">预设</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="下载器" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ downloaderLabel(row.downloader) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="100" prop="category" />
        <el-table-column label="标签" min-width="140">
          <template #default="{ row }">
            <el-tag
              v-for="t in (row.tags || '').split(';').filter(Boolean)"
              :key="t"
              size="small"
              style="margin-right: 4px"
            >
              {{ t }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="布局" width="120">
          <template #default="{ row }">{{ layoutLabel(Number(row.content_layout)) }}</template>
        </el-table-column>
        <el-table-column label="动作" width="100">
          <template #default="{ row }">
            <el-tag :type="Number(row.is_paused) ? 'warning' : 'success'" size="small">
              {{ Number(row.is_paused) ? '添加后暂停' : '添加后开始' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="限速/比率" min-width="160">
          <template #default="{ row }">
            <div class="limit-line" v-if="row.upload_limit || row.download_limit">
              上传{{ row.upload_limit || 0 }} / 下载{{ row.download_limit || 0 }} KB/s
            </div>
            <div class="limit-line" v-if="row.ratio_limit || row.seeding_time_limit">
              比率{{ row.ratio_limit || 0 }} / 做种{{ row.seeding_time_limit || 0 }}分钟
            </div>
            <span v-if="!row.upload_limit && !row.download_limit && !row.ratio_limit && !row.seeding_time_limit" class="muted">无限制</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-button :icon="Edit" link @click="openEdit(row)" :disabled="row.id <= 0">编辑</el-button>
            <el-button :icon="Delete" link type="danger" @click="remove(row)" :disabled="row.id <= 0">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="form.sid ? '编辑下载设置' : '新增下载设置'"
      width="640px"
      :close-on-click-modal="false"
    >
      <el-form label-width="120px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="别名" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="下载器" required>
              <el-select v-model="form.downloader">
                <el-option v-for="d in DOWNLOADER_TYPES" :key="d.value" :label="d.label" :value="d.value" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="分类">
              <el-input v-model="form.category" placeholder="仅Qbittorrent" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="标签">
              <el-input v-model="form.tags" placeholder="多个用;分隔" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="上传限速">
              <el-input v-model.number="form.upload_limit" placeholder="KB/s，0为不限" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="下载限速">
              <el-input v-model.number="form.download_limit" placeholder="KB/s，0为不限" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="布局">
              <el-select v-model="form.content_layout">
                <el-option v-for="c in CONTENT_LAYOUTS" :key="c.value" :label="c.label" :value="c.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="动作">
              <el-select v-model="form.is_paused">
                <el-option :value="0" label="添加后开始" />
                <el-option :value="1" label="添加后暂停" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="分享率限制">
              <el-input v-model.number="form.ratio_limit" placeholder="0为无限制" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="做种时间限制">
              <el-input v-model.number="form.seeding_time_limit" placeholder="分钟，0为无限制" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.download-setting {
  padding: 16px;
}
.limit-line {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.muted {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
</style>
