<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Delete, Refresh, Back, Star, Film } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import { getRssHistory, deleteRssHistory, reRssHistory, type RssHistoryItem, type RssType } from '@/api/rss'

const route = useRoute()
const router = useRouter()
const modal = useModalStore()

const loading = ref(false)
const list = ref<RssHistoryItem[]>([])
const activeType = ref<RssType | ''>('')

const tabs = [
  { label: '电影', value: 'MOV' as const },
  { label: '电视剧', value: 'TV' as const }
]

onMounted(() => {
  const t = route.query.t as string
  if (t === 'MOV' || t === 'TV') activeType.value = t
  load()
})

async function load() {
  loading.value = true
  try {
    const res = await getRssHistory(activeType.value || undefined)
    if (res.code === 0) list.value = res.result || []
    else modal.error(res.msg || '获取订阅历史失败')
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取订阅历史失败')
  } finally {
    loading.value = false
  }
}

const count = computed(() => list.value.length)

function tmdbUrl(item: RssHistoryItem) {
  if (!item.TMDBID) return ''
  const kind = item.TYPE === 'MOV' ? 'movie' : 'tv'
  return `https://www.themoviedb.org/${kind}/${item.TMDBID}`
}

function remainEpisodes(item: RssHistoryItem) {
  if (item.TOTAL && item.TOTAL > 0) {
    return item.TOTAL - (item.START || 0)
  }
  return null
}

async function onDelete(item: RssHistoryItem) {
  const ok = await modal.confirm(`确认删除「${item.NAME}」的订阅历史记录？`, '删除订阅历史')
  if (!ok) return
  try {
    const res = await deleteRssHistory(item.ID)
    if (res.code === 0) {
      modal.success('删除成功')
      load()
    } else {
      modal.error(res.msg || '删除失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '删除失败')
  }
}

async function onReRss(item: RssHistoryItem) {
  const t = (item.TYPE || activeType.value) as RssType
  if (!t) {
    modal.error('无法确定订阅类型')
    return
  }
  try {
    const res = await reRssHistory(item.ID, t)
    if (res.code === 0) modal.success('重新订阅成功！')
    else modal.error(`重新订阅失败：${res.msg || '未知错误'}`)
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '重新订阅失败')
  }
}

function goBack() {
  router.push(activeType.value === 'TV' ? '/tv_rss' : '/movie_rss')
}
</script>

<template>
  <div class="rss-history">
    <PageHeader title="订阅历史" description="查看已完成或已取消的订阅记录">
      <template #actions>
        <el-radio-group v-model="activeType" size="default" @change="load">
          <el-radio-button v-for="t in tabs" :key="t.value" :value="t.value">{{ t.label }}</el-radio-button>
        </el-radio-group>
        <el-button :icon="Back" @click="goBack">返回</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" v-loading="loading">
      <div class="toolbar">共 {{ count }} 条记录</div>
      <el-table :data="list" style="width: 100%" empty-text="没有完成的订阅">
        <el-table-column label="" width="80">
          <template #default="{ row }">
            <el-image
              :src="row.IMAGE"
              fit="cover"
              class="poster"
            >
              <template #error>
                <div class="poster-placeholder">
                  <el-icon :size="20"><Film /></el-icon>
                </div>
              </template>
            </el-image>
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="220">
          <template #default="{ row }">
            <div class="title-row">
              <span class="title-text">{{ row.NAME }}</span>
              <span v-if="row.YEAR" class="title-year">({{ row.YEAR }})</span>
              <span v-if="row.SEASON" class="title-season">{{ row.SEASON }}</span>
            </div>
            <a
              v-if="row.TMDBID"
              :href="tmdbUrl(row)"
              target="_blank"
              class="tmdb-badge"
            >TMDB: {{ row.TMDBID }}</a>
            <div v-if="remainEpisodes(row)" class="ep-info">
              <small>共 {{ remainEpisodes(row) }} 集</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="简介" min-width="260" class-name="desc-col">
          <template #default="{ row }">
            <span class="desc-text">{{ row.DESC }}</span>
          </template>
        </el-table-column>
        <el-table-column label="完成时间" width="170" prop="FINISH_TIME" />
        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button :icon="Star" size="small" @click="onReRss(row)">重新订阅</el-button>
            <el-button :icon="Delete" size="small" type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.rss-history {
  padding: 16px;
}
.toolbar {
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.poster {
  width: 50px;
  height: 75px;
  border-radius: 4px;
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
.title-row {
  margin-bottom: 4px;
}
.title-text {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.title-year,
.title-season {
  margin-left: 4px;
  color: var(--el-text-color-secondary);
}
.tmdb-badge {
  display: inline-block;
  font-size: 12px;
  color: var(--el-color-success);
  border: 1px solid var(--el-color-success);
  border-radius: 10px;
  padding: 1px 8px;
  text-decoration: none;
  margin-right: 4px;
}
.tmdb-badge:hover {
  background-color: var(--el-color-success);
  color: #fff;
}
.ep-info {
  color: var(--el-text-color-secondary);
}
.desc-text {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
</style>
