<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Delete, Edit, Film, Link, VideoCamera } from '@element-plus/icons-vue'
import {
  getTmdbCache,
  deleteTmdbCache,
  modifyTmdbCache,
  clearTmdbCache,
  type TmdbCacheItem
} from '@/api/rename'
import { useModalStore } from '@/stores/modal'
import PageHeader from '@/components/PageHeader.vue'

const modal = useModalStore()
const list = ref<TmdbCacheItem[]>([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(30)
const keyword = ref('')

const modifyVisible = ref(false)
const modifying = ref(false)
const modifyKey = ref('')
const modifyTitle = ref('')

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await getTmdbCache({
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

function posterUrl(item: TmdbCacheItem) {
  return item[1].poster_path ? `https://image.tmdb.org/t/p/w500${item[1].poster_path}` : ''
}

function tmdbUrl(item: TmdbCacheItem) {
  const id = item[1].id
  return item[1].media_type === '电影'
    ? `https://www.themoviedb.org/movie/${id}`
    : `https://www.themoviedb.org/tv/${id}`
}

function openModify(item: TmdbCacheItem) {
  modifyKey.value = item[0]
  modifyTitle.value = item[1].title
  modifyVisible.value = true
}

async function submitModify() {
  if (!modifyTitle.value) {
    modal.warning('标题不能为空')
    return
  }
  modifying.value = true
  try {
    const res = await modifyTmdbCache(modifyKey.value, modifyTitle.value)
    if (res.code === 0) {
      modifyVisible.value = false
      modal.success('修改成功')
      load()
    } else {
      modal.error(res.msg || '修改失败')
    }
  } finally {
    modifying.value = false
  }
}

async function remove(item: TmdbCacheItem) {
  const ok = await modal.confirm(`${item[2]} 对应的缓存TMDB条目将被删除，是否确认？`)
  if (!ok) return
  const res = await deleteTmdbCache(item[0])
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  }
}

async function clearAll() {
  const ok = await modal.confirm(
    '清空TMDB缓存后，将会增加TMDB API的请求次数，增加搜索响应时间，但是可以一次性解决TMDB改名后本地缓存未更新的问题，是否确认？'
  )
  if (!ok) return
  modal.showLoading('清理中...')
  try {
    const res = await clearTmdbCache()
    if (res.code === 0) {
      modal.success('TMDB缓存清理完成！')
      load()
    } else {
      modal.error(res.msg || '清理失败')
    }
  } finally {
    modal.hideLoading()
  }
}
</script>

<template>
  <div class="tmdb-cache" v-loading="loading">
    <PageHeader title="TMDB缓存" description="本地TMDB识别缓存管理">
      <template #actions>
        <el-button type="danger" :icon="Delete" @click="clearAll">清空TMDB缓存</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <div class="toolbar">
        <el-input
          v-model="keyword"
          placeholder="搜索缓存..."
          clearable
          style="width: 220px"
          @keyup.enter="doSearch"
        />
        <el-button type="primary" @click="doSearch">搜索</el-button>
      </div>

      <el-table :data="list" stripe>
        <el-table-column label="索引" min-width="220">
          <template #default="{ row }">
            <div class="index-cell">
              <el-icon class="type-icon">
                <Film v-if="row[1].media_type === '电影'" />
                <VideoCamera v-else />
              </el-icon>
              <span class="index-name">{{ row[2] }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="220" prop="1.title">
          <template #default="{ row }">{{ row[1].title }}</template>
        </el-table-column>
        <el-table-column label="TMDB" width="120">
          <template #default="{ row }">
            <span class="tmdb-id">{{ row[1].id }}</span>
            <a :href="tmdbUrl(row)" target="_blank" class="tmdb-link">
              <el-icon><Link /></el-icon>
            </a>
          </template>
        </el-table-column>
        <el-table-column label="海报" width="80">
          <template #default="{ row }">
            <el-image
              v-if="posterUrl(row)"
              :src="posterUrl(row)"
              fit="cover"
              class="poster"
              :preview-src-list="[posterUrl(row)]"
              preview-teleported
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-button :icon="Edit" link @click="openModify(row)">修改</el-button>
            <el-button :icon="Delete" link type="danger" @click="remove(row)">删除</el-button>
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

    <el-dialog v-model="modifyVisible" title="修改TMDB缓存" width="480px">
      <el-form label-width="80px">
        <el-form-item label="标题" required>
          <el-input v-model="modifyTitle" placeholder="标题" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modifyVisible = false">取消</el-button>
        <el-button type="primary" :loading="modifying" @click="submitModify">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tmdb-cache {
  padding: 16px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.index-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.type-icon {
  font-size: 18px;
  color: var(--el-color-primary);
}
.index-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tmdb-id {
  color: var(--el-text-color-secondary);
  margin-right: 6px;
}
.tmdb-link {
  color: var(--el-color-primary);
  text-decoration: none;
}
.poster {
  width: 50px;
  height: 75px;
  border-radius: 4px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
