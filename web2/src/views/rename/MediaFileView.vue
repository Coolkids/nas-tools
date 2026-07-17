<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Refresh,
  Folder,
  Document,
  Edit,
  Delete,
  Download,
  Back,
  Files
} from '@element-plus/icons-vue'
import { doAction } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import { bytesToSize } from '@/utils'

interface FileItem {
  path: string
  name: string
  ext: string
  size: number
  type?: string
}

interface SubPathResult {
  code: number
  msg?: string
  count?: number
  data?: FileItem[]
}

const modal = useModalStore()
const loading = ref(false)
const currentDir = ref('/')
const pathInput = ref('/')
const files = ref<FileItem[]>([])

onMounted(() => {
  load(currentDir.value)
})

async function load(dir?: string) {
  const target = dir || currentDir.value
  loading.value = true
  try {
    const res = await doAction<SubPathResult>('get_sub_path', { dir: target, filter: 'MEDIAFILE|SUBFILE' })
    if (res.code === 0) {
      files.value = res.data || []
      currentDir.value = target
      pathInput.value = target
    } else {
      modal.error(res.msg || '获取文件列表失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取文件列表失败')
  } finally {
    loading.value = false
  }
}

const breadcrumbs = computed(() => {
  const parts = currentDir.value.split('/').filter(Boolean)
  const crumbs = [{ name: '根目录', path: '/' }]
  let acc = ''
  for (const p of parts) {
    acc += '/' + p
    crumbs.push({ name: p, path: acc })
  }
  return crumbs
})

function goPath(path: string) {
  load(path)
}

function parentDir() {
  const d = currentDir.value
  const idx = d.lastIndexOf('/')
  const parent = idx <= 0 ? '/' : d.slice(0, idx)
  load(parent)
}

function goInput() {
  load(pathInput.value)
}

function isDir(f: FileItem): boolean {
  return !f.ext || f.type === 'dir'
}

function clickItem(f: FileItem) {
  if (isDir(f)) {
    load(f.path)
  }
}

const renameVisible = ref(false)
const renameTarget = ref('')
const renameNewName = ref('')

function openRename(f: FileItem) {
  renameTarget.value = f.path
  renameNewName.value = f.name
  renameVisible.value = true
}

async function doRename() {
  if (!renameNewName.value) return modal.warning('请填写新文件名')
  renameVisible.value = false
  const res = await doAction<{ code: number; msg?: string }>('rename_file', {
    path: renameTarget.value,
    name: renameNewName.value
  })
  if (res.code === 0) {
    modal.success('重命名成功')
    load()
  } else {
    modal.error(res.msg || '重命名失败')
  }
}

async function deleteFile(f: FileItem) {
  const ok = await modal.confirm(`是否确认删除文件 ${f.name} ？注意：没有其它媒体文件的目录也将被删除。`, '删除文件')
  if (!ok) return
  const res = await doAction<{ code: number; msg?: string }>('delete_files', { files: [f.path] })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  } else {
    modal.error(res.msg || '删除失败')
  }
}

async function downloadSubtitle(f: FileItem) {
  modal.showLoading('下载字幕中...')
  try {
    const res = await doAction<{ code: number; msg?: string }>('download_subtitle', { path: f.path, name: f.name })
    if (res.code === 0) {
      modal.success(res.msg || '下载字幕成功')
      load()
    } else {
      modal.error(res.msg || '下载字幕失败')
    }
  } finally {
    modal.hideLoading()
  }
}
</script>

<template>
  <div class="mediafile" v-loading="loading">
    <PageHeader title="文件管理" description="浏览媒体目录、重命名/删除/下载字幕">
      <template #actions>
        <el-button :icon="Back" @click="parentDir">上级目录</el-button>
        <el-button :icon="Refresh" @click="load()">刷新</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" class="path-card">
      <el-input v-model="pathInput" placeholder="目录路径" @keyup.enter="goInput">
        <template #prefix>
          <el-icon><Folder /></el-icon>
        </template>
        <template #append>
          <el-button :icon="Files" @click="goInput">前往</el-button>
        </template>
      </el-input>

      <el-breadcrumb class="crumbs" separator="/">
        <el-breadcrumb-item
          v-for="(c, idx) in breadcrumbs"
          :key="idx"
          @click="goPath(c.path)"
        >
          <span class="crumb-link">{{ c.name }}</span>
        </el-breadcrumb-item>
      </el-breadcrumb>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="list-header">
          <span>文件列表</span>
          <span class="muted">共 {{ files.length }} 个</span>
        </div>
      </template>

      <el-table :data="files" stripe empty-text="没有文件">
        <el-table-column label="名称" min-width="280">
          <template #default="{ row }">
            <div class="name-cell" :class="{ clickable: isDir(row) }" @click="clickItem(row)">
              <el-icon class="row-icon" :class="isDir(row) ? 'dir-icon' : 'file-icon'">
                <Folder v-if="isDir(row)" />
                <Document v-else />
              </el-icon>
              <span>{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="扩展名" width="100" prop="ext" />
        <el-table-column label="大小" width="120">
          <template #default="{ row }">{{ isDir(row) ? '-' : bytesToSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" align="center">
          <template #default="{ row }">
            <template v-if="!isDir(row)">
              <el-button :icon="Download" size="small" link @click="downloadSubtitle(row)">字幕</el-button>
              <el-button :icon="Edit" size="small" link @click="openRename(row)">重命名</el-button>
              <el-button :icon="Delete" size="small" type="danger" link @click="deleteFile(row)">删除</el-button>
            </template>
            <el-button v-else :icon="Folder" size="small" link @click="clickItem(row)">进入</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="renameVisible" title="文件重命名" width="480px" destroy-on-close>
      <el-form label-width="80px">
        <el-form-item label="新文件名" required>
          <el-input v-model="renameNewName" placeholder="新文件名" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" @click="doRename">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mediafile {
  padding: 16px;
}
.path-card {
  margin-bottom: 12px;
}
.crumbs {
  margin-top: 12px;
}
.crumb-link {
  cursor: pointer;
  color: var(--el-color-primary);
}
.crumb-link:hover {
  text-decoration: underline;
}
.list-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.muted {
  color: var(--el-text-color-secondary);
  font-weight: normal;
  font-size: 13px;
}
.name-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.name-cell.clickable {
  cursor: pointer;
  color: var(--el-color-primary);
}
.name-cell.clickable:hover {
  text-decoration: underline;
}
.row-icon {
  font-size: 16px;
}
.dir-icon {
  color: var(--el-color-warning);
}
.file-icon {
  color: var(--el-text-color-secondary);
}
</style>