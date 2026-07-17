<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Refresh, Delete, EditPen, MagicStick, Switch } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import {
  getUnknownList,
  delUnknownPath,
  reIdentification,
  renameUdf,
  type UnknownItem,
  type TransferMode,
  type ManualMediaType
} from '@/api/rename'

const modal = useModalStore()

const loading = ref(false)
const list = ref<UnknownItem[]>([])
const selection = ref<UnknownItem[]>([])

const transferModes: { label: string; value: TransferMode }[] = [
  { label: '复制', value: 'copy' },
  { label: '硬链接', value: 'link' },
  { label: '软链接', value: 'softlink' },
  { label: '移动', value: 'move' },
  { label: 'Rclone', value: 'rclone' },
  { label: 'Rclone复制', value: 'rclonecopy' },
  { label: 'MinIO', value: 'minio' },
  { label: 'MinIO复制', value: 'miniocopy' }
]

const mediaTypes: { label: string; value: ManualMediaType }[] = [
  { label: '电影', value: 'movie' },
  { label: '电视剧', value: 'tv' },
  { label: '动漫', value: 'anime' }
]

const modeLabel = (key: string) => {
  return transferModes.find((m) => m.value === key)?.label || key || ''
}

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await getUnknownList()
    if (res.code === 0) list.value = res.items || []
    else modal.error('获取未识别列表失败')
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取未识别列表失败')
  } finally {
    loading.value = false
  }
}

const count = computed(() => list.value.length)

function onSelectionChange(rows: UnknownItem[]) {
  selection.value = rows
}

async function onDelete(row: UnknownItem) {
  const ok = await modal.confirm(`确认删除未识别记录「${row.name}」？`, '删除记录')
  if (!ok) return
  try {
    const res = await delUnknownPath(row.id)
    if (res.retcode === 0) {
      modal.success('删除成功')
      load()
    } else {
      modal.error('删除失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '删除失败')
  }
}

async function onBatchDelete() {
  if (selection.value.length === 0) {
    modal.warning('请先选择要删除的记录')
    return
  }
  const ok = await modal.confirm(`确认删除选中的 ${selection.value.length} 条未识别记录？`, '批量删除')
  if (!ok) return
  try {
    const res = await delUnknownPath(selection.value.map((r) => r.id))
    if (res.retcode === 0) {
      modal.success('删除成功')
      load()
    } else {
      modal.error('删除失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '删除失败')
  }
}

async function onReIdentify(row: UnknownItem) {
  modal.showLoading('重新识别中...')
  try {
    const res = await reIdentification('unidentification', [row.id])
    if (res.retcode === 0) {
      modal.success('重新识别完成')
      load()
    } else {
      modal.error(`识别失败：${res.retmsg || '未知错误'}`)
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '识别失败')
  } finally {
    modal.hideLoading()
  }
}

async function onBatchReIdentify() {
  if (selection.value.length === 0) {
    modal.warning('请先选择要重新识别的记录')
    return
  }
  modal.showLoading('批量重新识别中...')
  try {
    const res = await reIdentification('unidentification', selection.value.map((r) => r.id))
    if (res.retcode === 0) {
      modal.success('批量重新识别完成')
      load()
    } else {
      modal.error(`处理完成：${res.retmsg || '部分识别失败'}`)
      load()
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '识别失败')
  } finally {
    modal.hideLoading()
  }
}

const dialogVisible = ref(false)
const dialogTitle = ref('手动识别')
const submitting = ref(false)
const form = reactive({
  id: '' as string | number,
  inpath: '',
  outpath: '',
  syncmod: 'copy' as TransferMode,
  type: 'movie' as ManualMediaType,
  tmdb: '',
  season: '',
  episode_format: '',
  episode_details: '',
  episode_offset: '',
  min_filesize: ''
})

function openManual(row?: UnknownItem) {
  dialogTitle.value = row ? '手动识别' : '自定义识别'
  form.id = row?.id ?? ''
  form.inpath = row?.path ?? ''
  form.outpath = row?.to ?? ''
  form.syncmod = (row?.sync_mode as TransferMode) || 'copy'
  form.type = 'movie'
  form.tmdb = ''
  form.season = ''
  form.episode_format = ''
  form.episode_details = ''
  form.episode_offset = ''
  form.min_filesize = ''
  dialogVisible.value = true
}

async function submitManual() {
  if (!form.inpath) {
    modal.error('请输入输入路径')
    return
  }
  if (!form.syncmod) {
    modal.error('请选择转移方式')
    return
  }
  submitting.value = true
  try {
    const res = await renameUdf({
      inpath: form.inpath,
      outpath: form.outpath || undefined,
      syncmod: form.syncmod,
      type: form.type,
      tmdb: form.tmdb || undefined,
      season: form.season || undefined,
      episode_format: form.episode_format || undefined,
      episode_details: form.episode_details || undefined,
      episode_offset: form.episode_offset || undefined,
      min_filesize: form.min_filesize || undefined
    })
    if (res.retcode === 0) {
      modal.success('转移成功')
      dialogVisible.value = false
      load()
    } else {
      modal.error(`转移失败：${res.retmsg || '未知错误'}`)
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '转移失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="unidentification">
    <PageHeader title="手动识别" description="处理无法自动识别的媒体文件">
      <template #actions>
        <el-button :icon="MagicStick" type="primary" @click="openManual()">自定义识别</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" v-loading="loading">
      <div class="toolbar">
        <span class="count">共 {{ count }} 条记录</span>
        <div class="batch-actions">
          <el-button :icon="Switch" size="small" :disabled="selection.length === 0" @click="onBatchReIdentify">
            批量重新识别
          </el-button>
          <el-button :icon="Delete" size="small" type="danger" :disabled="selection.length === 0" @click="onBatchDelete">
            删除选中
          </el-button>
        </div>
      </div>
      <el-table
        :data="list"
        style="width: 100%"
        empty-text="没有数据"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="45" />
        <el-table-column label="文件名" min-width="320">
          <template #default="{ row }">
            <div class="file-name" :title="row.path">{{ row.path }}</div>
            <div v-if="row.to" class="file-to">=&gt; {{ row.to }}</div>
          </template>
        </el-table-column>
        <el-table-column label="转移方式" width="120">
          <template #default="{ row }">
            <el-tag size="small" type="info" effect="plain">{{ modeLabel(row.sync_mode) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button :icon="Switch" size="small" @click="onReIdentify(row)">重新识别</el-button>
            <el-button :icon="EditPen" size="small" @click="openManual(row)">手动识别</el-button>
            <el-button :icon="Delete" size="small" type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="640px" destroy-on-close>
      <el-form :model="form" label-width="100px" label-position="right">
        <el-form-item label="输入路径" required>
          <el-input v-model="form.inpath" placeholder="输入文件/目录路径" />
        </el-form-item>
        <el-form-item label="输出路径">
          <el-input v-model="form.outpath" placeholder="留空则按配置自动选择" />
        </el-form-item>
        <el-form-item label="转移方式" required>
          <el-select v-model="form.syncmod" placeholder="选择转移方式" style="width: 100%">
            <el-option
              v-for="m in transferModes"
              :key="m.value"
              :label="m.label"
              :value="m.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="form.type">
            <el-radio v-for="t in mediaTypes" :key="t.value" :value="t.value">{{ t.label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="TMDB ID">
          <el-input v-model="form.tmdb" placeholder="留空则自动识别" />
        </el-form-item>
        <el-form-item v-if="form.type !== 'movie'" label="季">
          <el-input v-model="form.season" placeholder="如：1" />
        </el-form-item>
        <el-form-item v-if="form.type !== 'movie'" label="集数定位">
          <el-input v-model="form.episode_format" placeholder="如：第(\\d+)集" />
        </el-form-item>
        <el-form-item v-if="form.type !== 'movie'" label="集数详情">
          <el-input v-model="form.episode_details" placeholder="如：1-12" />
        </el-form-item>
        <el-form-item v-if="form.type !== 'movie'" label="集数偏移">
          <el-input v-model="form.episode_offset" placeholder="如：0" />
        </el-form-item>
        <el-form-item label="最小文件大小">
          <el-input v-model="form.min_filesize" placeholder="单位 MB，留空则不限" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitManual">开始转移</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.unidentification {
  padding: 16px;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}
.count {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.batch-actions {
  display: flex;
  gap: 8px;
}
.file-name {
  font-size: 13px;
  color: var(--el-text-color-primary);
  word-break: break-all;
}
.file-to {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
  word-break: break-all;
}
</style>
