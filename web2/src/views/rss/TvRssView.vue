<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, Delete, Clock, Film } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import { getTvRssList, removeRssMedia, type RssMediaItem } from '@/api/rss'

const router = useRouter()
const modal = useModalStore()

const loading = ref(false)
const items = ref<RssMediaItem[]>([])

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await getTvRssList()
    if (res.code === 0) items.value = Object.values(res.result || {})
    else modal.error(res.msg || '获取电视剧订阅失败')
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取电视剧订阅失败')
  } finally {
    loading.value = false
  }
}

const count = computed(() => items.value.length)

function stateMeta(state?: string) {
  switch (state) {
    case 'D':
      return { label: '队列中', type: 'info' as const }
    case 'S':
      return { label: '正在搜索', type: 'warning' as const }
    case 'R':
      return { label: '正在订阅', type: 'success' as const }
    default:
      return { label: '完成', type: 'primary' as const }
  }
}

function progressOf(item: RssMediaItem) {
  const total = item.total || 0
  if (total <= 0) return 0
  const done = total - (item.lack || 0)
  return Math.round((done * 100) / total)
}

function doneOf(item: RssMediaItem) {
  const total = item.total || 0
  if (total <= 0) return null
  return `${total - (item.lack || 0)}/${total}`
}

async function onRemove(item: RssMediaItem) {
  const ok = await modal.confirm(`确认删除电视剧订阅「${item.name} ${item.season || ''}」？`, '删除订阅')
  if (!ok) return
  try {
    const res = await removeRssMedia({
      name: item.name,
      type: 'TV',
      year: item.year,
      season: item.season,
      rssid: item.id,
      tmdbid: item.tmdbid
    })
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

function goHistory() {
  router.push({ path: '/rss_history', query: { t: 'TV' } })
}
</script>

<template>
  <div class="tv-rss">
    <PageHeader title="电视剧订阅" description="管理已订阅的电视剧">
      <template #actions>
        <el-button :icon="Clock" @click="goHistory">订阅历史</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-empty
      v-if="!loading && count === 0"
      description="当前没有正在订阅的电视剧。"
    />

    <div v-else v-loading="loading" class="card-grid">
      <el-card
        v-for="item in items"
        :key="item.id"
        class="rss-card"
        shadow="hover"
        :body-style="{ padding: '0' }"
      >
        <div class="poster-wrap">
          <el-image :src="item.image" fit="cover" class="poster">
            <template #error>
              <div class="poster-placeholder">
                <el-icon :size="28"><Film /></el-icon>
              </div>
            </template>
          </el-image>
        </div>
        <el-progress
          v-if="item.total && item.total > 0"
          :percentage="progressOf(item)"
          :show-text="false"
          :stroke-width="3"
          class="card-progress"
        />
        <div class="card-body">
          <div class="card-title" :title="item.name">
            <span class="name">{{ item.name }}</span>
            <span v-if="item.year" class="year">({{ item.year }})</span>
            <span v-if="item.season && item.season !== 'S00'" class="season">{{ item.season }}</span>
          </div>
          <div class="card-state">
            <el-tag size="small" :type="stateMeta(item.state).type" effect="light">
              {{ stateMeta(item.state).label }}
            </el-tag>
            <span v-if="doneOf(item)" class="ep-count">{{ doneOf(item) }}</span>
          </div>
          <div v-if="item.over_edition" class="badges">
            <el-tag size="small" type="danger" effect="dark">洗版</el-tag>
          </div>
          <div class="actions">
            <el-button :icon="Delete" size="small" type="danger" plain @click="onRemove(item)">删除</el-button>
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.tv-rss {
  padding: 16px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 16px;
}
.rss-card {
  overflow: hidden;
  transition: transform 0.15s;
}
.rss-card:hover {
  transform: translateY(-2px);
}
.poster-wrap {
  width: 100%;
  aspect-ratio: 2 / 3;
  background-color: var(--el-fill-color-light);
  overflow: hidden;
}
.poster {
  width: 100%;
  height: 100%;
  display: block;
}
.poster-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-placeholder);
}
.card-progress {
  display: block;
}
.card-progress :deep(.el-progress-bar__outer) {
  border-radius: 0;
}
.card-body {
  padding: 10px 12px 12px;
}
.card-title {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.name {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.year,
.season {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}
.card-state {
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ep-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.badges {
  margin-bottom: 8px;
}
.actions {
  display: flex;
  justify-content: center;
}
</style>
