<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Plus, Edit, Delete } from '@element-plus/icons-vue'
import { doAction } from '@/api'
import { useModalStore } from '@/stores/modal'
import PageHeader from '@/components/PageHeader.vue'

interface SyncPath {
  id: number
  from: string
  to: string
  unknown: string
  syncmod: string
  syncmod_name: string
  rename: number
  enabled: number
}

const modal = useModalStore()
const list = ref<SyncPath[]>([])
const loading = ref(false)

const SYNC_MODES = [
  { value: 'link', label: '硬链接' },
  { value: 'softlink', label: '软链接' },
  { value: 'copy', label: '复制' },
  { value: 'move', label: '移动' },
  { value: 'rclonecopy', label: 'Rclone复制' },
  { value: 'rclone', label: 'Rclone移动' },
  { value: 'miniocopy', label: 'Minio复制' },
  { value: 'minio', label: 'Minio移动' }
]

const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({
  sid: '' as string | number,
  from: '',
  to: '',
  unknown: '',
  syncmod: 'link',
  rename: true,
  enabled: true
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await doAction<{ code: number; result: SyncPath[] }>('get_directorysync', {})
    if (res.code === 0) {
      list.value = res.result || []
    }
  } finally {
    loading.value = false
  }
}

function modeLabel(value: string) {
  return SYNC_MODES.find((m) => m.value === value)?.label || value
}

function openAdd() {
  form.sid = ''
  form.from = ''
  form.to = ''
  form.unknown = ''
  form.syncmod = 'link'
  form.rename = true
  form.enabled = true
  dialogVisible.value = true
}

async function openEdit(row: SyncPath) {
  const res = await doAction<{ code: number; msg?: string; data: SyncPath }>('get_sync_path', { sid: row.id })
  if (res.code === 0) {
    const d = res.data
    form.sid = d.id
    form.from = d.from
    form.to = d.to
    form.unknown = d.unknown
    form.syncmod = d.syncmod
    form.rename = d.rename === 1
    form.enabled = d.enabled === 1
    dialogVisible.value = true
  } else {
    modal.error(res.msg || '查询失败')
  }
}

async function submit() {
  if (!form.from) {
    modal.warning('源目录不能为空')
    return
  }
  if (!form.rename && !form.to) {
    modal.warning('未启用识别重命名时，目的目录不能为空')
    return
  }
  saving.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('add_or_edit_sync_path', {
      sid: form.sid,
      from: form.from,
      to: form.to,
      unknown: form.unknown,
      syncmod: form.syncmod,
      rename: form.rename,
      enabled: form.enabled
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

async function remove(row: SyncPath) {
  const ok = await modal.confirm(`确认删除源目录「${row.from}」的同步配置？`)
  if (!ok) return
  const res = await doAction<{ code: number }>('delete_sync_path', { sid: row.id })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  }
}

async function toggle(row: SyncPath, flag: 'rename' | 'enable', checked: boolean) {
  const res = await doAction<{ code: number }>('check_sync_path', { flag, sid: row.id, checked })
  if (res.code === 0) {
    if (flag === 'rename') row.rename = checked ? 1 : 0
    else row.enabled = checked ? 1 : 0
  } else {
    modal.error('更新失败')
    load()
  }
}
</script>

<template>
  <div class="directory-sync" v-loading="loading">
    <PageHeader title="目录同步" description="配置源目录到目的目录的自动同步策略">
      <template #actions>
        <el-button type="primary" :icon="Plus" @click="openAdd">新增同步目录</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <el-table :data="list" stripe>
        <el-table-column label="源目录" prop="from" min-width="200" show-overflow-tooltip />
        <el-table-column label="目的目录" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.to || '-' }}</span>
            <div v-if="row.unknown && row.rename" class="unknown-tip">未识别：{{ row.unknown }}</div>
          </template>
        </el-table-column>
        <el-table-column label="同步方式" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ row.syncmod_name || modeLabel(row.syncmod) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="识别重命名" width="110" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.rename === 1"
              @change="(v: boolean) => toggle(row, 'rename', v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled === 1"
              @change="(v: boolean) => toggle(row, 'enable', v)"
            />
          </template>
        </el-table-column>
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
      :title="form.sid ? '编辑同步目录' : '新增同步目录'"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form label-width="110px">
        <el-form-item label="同步目录" required>
          <div class="path-row">
            <el-input v-model="form.from" placeholder="源目录" />
            <el-input v-model="form.to" placeholder="目的目录" />
            <el-input v-model="form.unknown" placeholder="未识别目录" />
          </div>
        </el-form-item>
        <el-form-item label="同步方式" required>
          <el-select v-model="form.syncmod" placeholder="选择同步方式">
            <el-option v-for="m in SYNC_MODES" :key="m.value" :label="m.label" :value="m.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="选项">
          <el-switch v-model="form.rename" active-text="识别并重命名" />
          <el-switch v-model="form.enabled" active-text="开启同步" style="margin-left: 24px" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.directory-sync {
  padding: 16px;
}
.unknown-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.path-row {
  display: flex;
  gap: 8px;
  width: 100%;
}
.path-row .el-input {
  flex: 1;
}
</style>
