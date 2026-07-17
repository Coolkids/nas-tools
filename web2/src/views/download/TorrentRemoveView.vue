<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Plus, Edit, Delete, VideoPlay, Lightning } from '@element-plus/icons-vue'
import { doAction } from '@/api'
import { useModalStore } from '@/stores/modal'
import PageHeader from '@/components/PageHeader.vue'

interface TaskConfig {
  ratio?: number | string
  seeding_time?: number | string
  upload_avs?: number | string
  size?: number[]
  tags?: string[]
  savepath_key?: string
  tracker_key?: string
  qb_category?: string[]
  qb_state?: string[]
  tr_error_key?: string
  tr_state?: string[]
}
interface RemoveTask {
  id: number
  name: string
  enabled: number
  action: number
  interval: number | string
  samedata: number
  onlynastool: number
  downloader: string
  config: TaskConfig
}

const modal = useModalStore()
const list = ref<RemoveTask[]>([])
const loading = ref(false)

const ACTION_OPTIONS = [
  { value: 1, label: '暂停种子', type: 'primary' },
  { value: 2, label: '删除种子', type: 'warning' },
  { value: 3, label: '删除种子及文件', type: 'danger' }
]
const DOWNLOADER_OPTIONS = [
  { value: 'Qb', label: 'Qbittorrent' },
  { value: 'Tr', label: 'Transmission' }
]

const QB_STATES = [
  'downloading', 'stalledDL', 'uploading', 'stalledUP', 'error', 'pausedDL', 'pausedUP',
  'missingFiles', 'checkingDL', 'checkingUP', 'checkingResumeData', 'forcedDL', 'queuedDL',
  'forcedUP', 'queuedUP', 'allocating', 'metaDL', 'moving', 'unknown'
]
const TR_STATES = [
  'downloading', 'seeding', 'download_pending', 'seed_pending', 'checking', 'check_pending', 'stopped'
]

const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({
  tid: '' as string | number,
  name: '',
  action: 1,
  interval: '' as string | number,
  enabled: 0,
  samedata: 0,
  onlynastool: 1,
  ratio: '',
  seeding_time: '',
  upload_avs: '',
  size: '',
  tags: '',
  savepath_key: '',
  tracker_key: '',
  downloader: 'Qb',
  qb_state: '',
  qb_category: '',
  tr_state: '',
  tr_error_key: ''
})

const previewVisible = ref(false)
const previewLoading = ref(false)
const previewList = ref<Array<{ name: string; site: string; size: number }>>([])

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await doAction<{ code: number; detail: Record<string, RemoveTask> }>('get_torrent_remove_task', {})
    if (res.code === 0 && res.detail) {
      list.value = Object.values(res.detail)
    }
  } finally {
    loading.value = false
  }
}

function actionLabel(v: number) {
  return ACTION_OPTIONS.find((a) => a.value === v)?.label || ''
}
function actionType(v: number) {
  return ACTION_OPTIONS.find((a) => a.value === v)?.type || 'info'
}

function resetForm() {
  form.tid = ''
  form.name = ''
  form.action = 1
  form.interval = ''
  form.enabled = 0
  form.samedata = 0
  form.onlynastool = 1
  form.ratio = ''
  form.seeding_time = ''
  form.upload_avs = ''
  form.size = ''
  form.tags = ''
  form.savepath_key = ''
  form.tracker_key = ''
  form.downloader = 'Qb'
  form.qb_state = ''
  form.qb_category = ''
  form.tr_state = ''
  form.tr_error_key = ''
}

function openAdd() {
  resetForm()
  dialogVisible.value = true
}

async function openEdit(row: RemoveTask) {
  const res = await doAction<{ code: number; detail: RemoveTask }>('get_torrent_remove_task', { tid: row.id })
  if (res.code === 0 && res.detail) {
    const d = res.detail
    const cfg = d.config || {}
    resetForm()
    form.tid = d.id
    form.name = d.name
    form.action = d.action
    form.interval = d.interval
    form.enabled = d.enabled
    form.samedata = d.samedata
    form.onlynastool = d.onlynastool
    form.downloader = d.downloader || 'Qb'
    form.ratio = cfg.ratio != null ? String(cfg.ratio) : ''
    form.seeding_time = cfg.seeding_time != null ? String(cfg.seeding_time) : ''
    form.upload_avs = cfg.upload_avs != null ? String(cfg.upload_avs) : ''
    if (Array.isArray(cfg.size) && cfg.size.length) {
      form.size = `${cfg.size[0]}-${cfg.size[cfg.size.length - 1]}`
    }
    form.tags = Array.isArray(cfg.tags) ? cfg.tags.join(';') : ''
    form.savepath_key = cfg.savepath_key || ''
    form.tracker_key = cfg.tracker_key || ''
    form.qb_state = Array.isArray(cfg.qb_state) ? cfg.qb_state.join(';') : ''
    form.qb_category = Array.isArray(cfg.qb_category) ? cfg.qb_category.join(';') : ''
    form.tr_state = Array.isArray(cfg.tr_state) ? cfg.tr_state.join(';') : ''
    form.tr_error_key = cfg.tr_error_key || ''
    dialogVisible.value = true
  } else {
    modal.error('获取任务详情失败')
  }
}

async function submit() {
  if (!form.name) {
    modal.warning('名称不能为空')
    return
  }
  if (!form.interval || isNaN(Number(form.interval))) {
    modal.warning('运行间隔必须为数字')
    return
  }
  if (form.ratio && isNaN(Number(form.ratio))) {
    modal.warning('分享率必须为数字')
    return
  }
  if (form.seeding_time && isNaN(Number(form.seeding_time))) {
    modal.warning('做种时间必须为数字')
    return
  }
  if (form.upload_avs && isNaN(Number(form.upload_avs))) {
    modal.warning('平均上传速度必须为数字')
    return
  }
  saving.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('update_torrent_remove_task', {
      tid: form.tid,
      name: form.name,
      action: form.action,
      interval: form.interval,
      enabled: form.enabled,
      samedata: form.samedata,
      onlynastool: form.onlynastool,
      ratio: form.ratio,
      seeding_time: form.seeding_time,
      upload_avs: form.upload_avs,
      size: form.size,
      tags: form.tags,
      savepath_key: form.savepath_key,
      tracker_key: form.tracker_key,
      downloader: form.downloader,
      qb_state: form.qb_state,
      qb_category: form.qb_category,
      tr_state: form.tr_state,
      tr_error_key: form.tr_error_key
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

async function remove(row: RemoveTask) {
  const ok = await modal.confirm(`确认删除删种任务「${row.name}」？`)
  if (!ok) return
  const res = await doAction<{ code: number }>('delete_torrent_remove_task', { tid: row.id })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  }
}

async function run(row: RemoveTask) {
  modal.info(`正在运行任务「${row.name}」...`)
  const res = await doAction<{ code: number }>('auto_remove_torrents', { tid: row.id })
  if (res.code === 0) {
    modal.success('任务运行完成！')
    load()
  }
}

async function preview(row: RemoveTask) {
  previewVisible.value = true
  previewLoading.value = true
  try {
    const res = await doAction<{ code: number; msg?: string; data: Array<{ name: string; site: string; size: number }> }>('get_remove_torrents', { tid: row.id })
    if (res.code === 0) {
      previewList.value = res.data || []
    } else {
      previewList.value = []
      modal.warning(res.msg || '未获取到符合处理条件种子')
    }
  } finally {
    previewLoading.value = false
  }
}

function fmtSize(size: number) {
  return (size / 1024 / 1024 / 1024).toFixed(2) + ' GB'
}
</script>

<template>
  <div class="torrent-remove-view" v-loading="loading">
    <PageHeader title="自动删种任务" description="按条件自动暂停/删除下载器中的种子">
      <template #actions>
        <el-button type="primary" :icon="Plus" @click="openAdd">新增删种任务</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <el-table :data="list" stripe>
        <el-table-column label="状态" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'danger'" size="small">
              {{ row.enabled ? '运行' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="任务名称" prop="name" min-width="160" />
        <el-table-column label="下载器" width="130">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ DOWNLOADER_OPTIONS.find((d) => d.value === row.downloader)?.label || row.downloader }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="动作" width="140">
          <template #default="{ row }">
            <el-tag :type="actionType(row.action)" size="small">{{ actionLabel(row.action) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="刷新间隔" width="110">
          <template #default="{ row }">{{ row.interval }} 分钟</template>
        </el-table-column>
        <el-table-column label="分享率" width="90">
          <template #default="{ row }">
            <span v-if="row.config?.ratio">{{ row.config.ratio }} +</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="做种时间" width="110">
          <template #default="{ row }">
            <span v-if="row.config?.seeding_time">{{ row.config.seeding_time }} 小时 +</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="大小(GB)" width="140">
          <template #default="{ row }">
            <span v-if="row.config?.size && row.config.size.length">{{ row.config.size[0] }}-{{ row.config.size[row.config.size.length - 1] }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" align="center" fixed="right">
          <template #default="{ row }">
            <el-button :icon="Lightning" link type="success" @click="run(row)">运行</el-button>
            <el-button :icon="VideoPlay" link @click="preview(row)">预览</el-button>
            <el-button :icon="Edit" link @click="openEdit(row)">编辑</el-button>
            <el-button :icon="Delete" link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="form.tid ? '编辑删种任务' : '新增删种任务'"
      width="820px"
      :close-on-click-modal="false"
    >
      <el-form label-width="180px">
        <el-divider content-position="left">基础设置</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="别名" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="动作">
              <el-select v-model="form.action">
                <el-option v-for="a in ACTION_OPTIONS" :key="a.value" :value="a.value" :label="a.label" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="运行间隔(分钟)" required>
              <el-input v-model="form.interval" placeholder="分钟" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="状态">
              <el-select v-model="form.enabled">
                <el-option :value="1" label="启用" />
                <el-option :value="0" label="停用" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="处理辅种">
              <el-select v-model="form.samedata">
                <el-option :value="0" label="否" />
                <el-option :value="1" label="是" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="只管理NAStool添加">
              <el-select v-model="form.onlynastool">
                <el-option :value="0" label="否" />
                <el-option :value="1" label="是" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">条件设置</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="分享率">
              <el-input v-model="form.ratio" placeholder="保留一位小数" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="做种时间(小时)">
              <el-input v-model="form.seeding_time" placeholder="小时" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="平均上传速度(KB/s)">
              <el-input v-model="form.upload_avs" placeholder="KB/s" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="种子大小(GB)">
              <el-input v-model="form.size" placeholder="如 1-10" />
            </el-form-item>
          </el-col>
          <el-col :span="16">
            <el-form-item label="标签">
              <el-input v-model="form.tags" placeholder="多个标签用;分隔" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="保存路径关键词">
              <el-input v-model="form.savepath_key" placeholder="支持正则表达式" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="tracker关键词">
              <el-input v-model="form.tracker_key" placeholder="支持正则表达式" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">下载器</el-divider>
        <el-form-item label="下载器" required>
          <el-radio-group v-model="form.downloader">
            <el-radio-button v-for="d in DOWNLOADER_OPTIONS" :key="d.value" :value="d.value">{{ d.label }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.downloader === 'Qb'">
          <el-row :gutter="12">
            <el-col :span="14">
              <el-form-item label="种子状态">
                <el-select
                  v-model="form.qb_state"
                  placeholder="多个状态用;分隔"
                  filterable
                  multiple
                  :reserve-keyword="true"
                >
                  <el-option v-for="s in QB_STATES" :key="s" :value="s" :label="s" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="10">
              <el-form-item label="分类">
                <el-input v-model="form.qb_category" placeholder="多个分类用;分隔" />
              </el-form-item>
            </el-col>
          </el-row>
        </template>
        <template v-else>
          <el-row :gutter="12">
            <el-col :span="14">
              <el-form-item label="种子状态">
                <el-select
                  v-model="form.tr_state"
                  placeholder="多个状态用;分隔"
                  filterable
                  multiple
                  :reserve-keyword="true"
                >
                  <el-option v-for="s in TR_STATES" :key="s" :value="s" :label="s" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="10">
              <el-form-item label="错误信息关键词">
                <el-input v-model="form.tr_error_key" placeholder="支持正则表达式" />
              </el-form-item>
            </el-col>
          </el-row>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="previewVisible" title="预处理种子列表" width="700px">
      <div v-loading="previewLoading">
        <div v-if="previewList.length" class="preview-count">共 {{ previewList.length }} 个种子</div>
        <el-table :data="previewList" v-if="previewList.length" max-height="420">
          <el-table-column label="种子名称" prop="name" min-width="240" show-overflow-tooltip />
          <el-table-column label="站点" prop="site" width="140" />
          <el-table-column label="大小" width="120">
            <template #default="{ row }">{{ fmtSize(row.size) }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="未获取到符合处理条件的种子" />
      </div>
      <template #footer>
        <el-button type="primary" @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.torrent-remove-view {
  padding: 16px;
}
.muted {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.preview-count {
  margin-bottom: 8px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}
</style>
