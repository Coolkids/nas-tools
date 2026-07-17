<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Refresh, VideoPlay, VideoPause, Delete, Film } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import { getDownloading, ptStart, ptStop, ptRemove, type DownloadTask } from '@/api/download'

const modal = useModalStore()

const loading = ref(false)
const list = ref<DownloadTask[]>([])
let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function load() {
  try {
    const res = await getDownloading()
    if (res.code === 0) list.value = res.result || []
  } catch (e) {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }
}

const count = computed(() => list.value.length)

async function manualRefresh() {
  loading.value = true
  await load()
  loading.value = false
}

function isStopped(task: DownloadTask) {
  return task.state !== 'Downloading'
}

async function onStart(task: DownloadTask) {
  try {
    await ptStart(task.id)
    await load()
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '开始失败')
  }
}

async function onStop(task: DownloadTask) {
  try {
    await ptStop(task.id)
    await load()
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '暂停失败')
  }
}

async function onRemove(task: DownloadTask) {
  const ok = await modal.confirm('确认删除该下载任务？删除后将同时删除文件。', '删除下载')
  if (!ok) return
  try {
    await ptRemove(task.id)
    modal.success('删除成功')
    await load()
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '删除失败')
  }
}

function progressType(progress: number) {
  if (progress >= 100) return 'success' as const
  return '' as const
}
</script>

<template>
  <div class="downloading">
    <PageHeader title="正在下载" description="查看下载器中正在进行的任务">
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" @click="manualRefresh">刷新</el-button>
      </template>
    </PageHeader>

    <el-empty
      v-if="!loading && count === 0"
      description="当前下载器中没有正在下载的任务。"
    />

    <el-card v-else shadow="never" v-loading="loading">
      <el-table :data="list" style="width: 100%" empty-text="没有下载任务">
        <el-table-column label="任务" min-width="320">
          <template #default="{ row }">
            <div class="task-cell">
              <el-image
                v-if="row.image"
                :src="row.image"
                fit="cover"
                class="task-poster"
              >
                <template #error>
                  <div class="poster-placeholder">
                    <el-icon :size="18"><Film /></el-icon>
                  </div>
                </template>
              </el-image>
              <div class="task-info">
                <div class="task-title" :title="row.title || row.name">{{ row.title || row.name }}</div>
                <div class="task-speed">{{ row.speed }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="240">
          <template #default="{ row }">
            <el-progress
              :percentage="Number(row.progress) || 0"
              :status="progressType(Number(row.progress) || 0)"
              :stroke-width="14"
              :text-inside="true"
            />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="isStopped(row)" size="small" type="info" effect="plain">已暂停</el-tag>
            <el-tag v-else size="small" type="success" effect="light">下载中</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="isStopped(row)"
              :icon="VideoPlay"
              size="small"
              type="primary"
              plain
              @click="onStart(row)"
            >开始</el-button>
            <el-button
              v-else
              :icon="VideoPause"
              size="small"
              type="warning"
              plain
              @click="onStop(row)"
            >暂停</el-button>
            <el-button :icon="Delete" size="small" type="danger" plain @click="onRemove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.downloading {
  padding: 16px;
}
.task-cell {
  display: flex;
  align-items: center;
  gap: 12px;
}
.task-poster {
  width: 44px;
  height: 60px;
  border-radius: 4px;
  flex-shrink: 0;
  overflow: hidden;
  background-color: var(--el-fill-color-light);
}
.poster-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-placeholder);
}
.task-info {
  min-width: 0;
  flex: 1;
}
.task-title {
  font-weight: 600;
  font-size: 13px;
  color: var(--el-text-color-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-speed {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
</style>
