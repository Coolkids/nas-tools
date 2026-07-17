<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Film, Refresh, Search, VideoCamera, Delete } from '@element-plus/icons-vue'
import {
  getTransferHistory,
  deleteHistory,
  reIdentification,
  type TransferHistoryItem
} from '@/api/rename'
import { useModalStore } from '@/stores/modal'
import PageHeader from '@/components/PageHeader.vue'

const modal = useModalStore()
const list = ref<TransferHistoryItem[]>([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(30)
const keyword = ref('')
const selected = ref<TransferHistoryItem[]>([])

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await getTransferHistory({
      page: currentPage.value,
      keyword: keyword.value,
      pagenum: pageSize.value
    })
    if (res.code === 0) {
      list.value = res.result || []
      total.value = res.total || 0
    } else {
      modal.error(res.msg || '加载失败')
    }
  } finally {
    loading.value = false
  }
}

function doSearch() {
  currentPage.value = 1
  load()
}

function pageChange(p: number) {
  currentPage.value = p
  load()
}

function tmdbUrl(row: TransferHistoryItem) {
  if (!row.TMDBID) return ''
  return row.TYPE === '电影'
    ? `https://www.themoviedb.org/movie/${row.TMDBID}`
    : `https://www.themoviedb.org/tv/${row.TMDBID}`
}

async function reIdentify(rows: TransferHistoryItem[]) {
  if (rows.length === 0) {
    modal.warning('请先选择记录')
    return
  }
  const ids = rows.map((r) => r.ID)
  modal.showLoading('重新识别中...')
  try {
    const res = await reIdentification('history', ids)
    if (res.retcode === 0) {
      modal.success(res.retmsg || '转移成功')
    } else {
      modal.warning(res.retmsg || '识别失败')
    }
    load()
  } finally {
    modal.hideLoading()
  }
}

async function remove(flag: 'del_source' | 'del_dest' | 'del_all', rows: TransferHistoryItem[]) {
  if (rows.length === 0) {
    modal.warning('请先选择记录')
    return
  }
  const label = flag === 'del_all' ? '源文件及媒体库' : flag === 'del_source' ? '源' : '媒体库'
  const name = rows.map((r) => `${r.TITLE} (${r.YEAR}) ${r.SEASON_EPISODE || ''}`).join('、')
  const ok = await modal.confirm(`${name} 对应${label}文件将被同步删除，是否确认？`)
  if (!ok) return
  const logids = rows.map((r) => r.ID)
  const res = await deleteHistory(flag, logids)
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  } else {
    modal.error(res.msg || '删除失败')
  }
}
</script>

<template>
  <div class="history" v-loading="loading">
    <PageHeader title="转移历史" description="文件整理转移记录">
      <template #actions>
        <el-input
          v-model="keyword"
          placeholder="搜索标题..."
          clearable
          style="width: 200px"
          @keyup.enter="doSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Refresh" @click="reIdentify(selected)">重新识别</el-button>
        <el-dropdown @command="(c: 'del_source' | 'del_dest' | 'del_all') => remove(c, selected)">
          <el-button type="danger" :icon="Delete">批量删除</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="del_source">删除源文件</el-dropdown-item>
              <el-dropdown-item command="del_dest">删除媒体库文件</el-dropdown-item>
              <el-dropdown-item command="del_all">删除源及媒体库文件</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <el-table :data="list" stripe @selection-change="(v: TransferHistoryItem[]) => (selected = v)">
        <el-table-column type="selection" width="42" />
        <el-table-column label="媒体信息" min-width="260">
          <template #default="{ row }">
            <div class="media-cell">
              <el-icon class="media-icon">
                <Film v-if="row.TYPE === '电影'" />
                <VideoCamera v-else />
              </el-icon>
              <div class="media-info">
                <div class="media-title">
                  <a v-if="row.TMDBID" :href="tmdbUrl(row)" target="_blank">
                    {{ row.TITLE }} ({{ row.YEAR }})
                  </a>
                  <span v-else>{{ row.TITLE }} ({{ row.YEAR }})</span>
                  <span v-if="row.SEASON_EPISODE" class="season">{{ row.SEASON_EPISODE }}</span>
                </div>
                <div v-if="row.CATEGORY" class="media-cat">类别：{{ row.CATEGORY }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="文件信息" min-width="280">
          <template #default="{ row }">
            <div class="file-line">{{ row.SOURCE_FILENAME || '' }}</div>
            <div v-if="row.DEST_PATH || row.DEST_FILENAME" class="file-line">
              <span class="arrow">=&gt; </span>
              <span :class="{ green: !!row.DEST_PATH }">{{ row.DEST_FILENAME || '' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">
            <div class="time-cell">
              <small>{{ row.DATE }}</small>
              <small class="muted">来自：{{ row.SOURCE || '' }}</small>
              <small class="muted">转移方式：{{ row.SYNC_MODE || row.MODE || '' }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-dropdown
              trigger="click"
              @command="(c: string) => c === 're' ? reIdentify([row]) : remove(c as 'del_source' | 'del_dest' | 'del_all', [row])"
            >
              <el-button link>更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="re">重新识别</el-dropdown-item>
                  <el-dropdown-item command="del_source" divided>删除源文件</el-dropdown-item>
                  <el-dropdown-item command="del_dest">删除媒体库文件</el-dropdown-item>
                  <el-dropdown-item command="del_all">删除源及媒体库文件</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          background
          layout="total, prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="currentPage"
          @current-change="pageChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.history {
  padding: 16px;
}
.media-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.media-icon {
  font-size: 18px;
  color: var(--el-color-primary);
}
.media-title a {
  color: var(--el-color-primary);
  text-decoration: none;
}
.media-title a:hover {
  text-decoration: underline;
}
.season {
  color: var(--el-color-warning);
  margin-left: 6px;
}
.media-cat,
.muted {
  color: var(--el-text-color-secondary);
}
.file-line {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.arrow {
  color: var(--el-text-color-secondary);
}
.green {
  color: var(--el-color-success);
}
.time-cell {
  display: flex;
  flex-direction: column;
  font-size: 12px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
