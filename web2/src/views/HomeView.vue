<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { Refresh, Film, Monitor, Headset, User, VideoPlay } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import {
  getLibraryMediacount,
  getLibrarySpacesize,
  getTransferStatistics,
  getLibraryPlayhistory,
  type MediaCountResult,
  type LibrarySpaceResult,
  type TransferStatisticsResult,
  type PlayHistoryItem
} from '@/api/system'

const modal = useModalStore()
const loading = ref(false)

const mediaCount = ref<MediaCountResult>({ code: -1 })
const space = ref<LibrarySpaceResult>({ code: -1 })
const stat = ref<TransferStatisticsResult>({
  code: -1,
  MovieChartLabels: [],
  MovieNums: [],
  TvChartLabels: [],
  TvNums: [],
  AnimeNums: []
})
const history = ref<PlayHistoryItem[]>([])

const serverOk = computed(() => mediaCount.value.code === 0)

const totalStat = computed(() => {
  const sum = (arr: number[]) => arr.reduce((a, b) => a + (b || 0), 0)
  return {
    movie: sum(stat.value.MovieNums),
    tv: sum(stat.value.TvNums),
    anime: sum(stat.value.AnimeNums)
  }
})

const statMax = computed(() =>
  Math.max(1, totalStat.value.movie, totalStat.value.tv, totalStat.value.anime)
)

const statRows = computed(() => {
  const movieMap = new Map<string, number>()
  stat.value.MovieChartLabels.forEach((d, i) => movieMap.set(d, stat.value.MovieNums[i] || 0))
  const tvMap = new Map<string, number>()
  const animeMap = new Map<string, number>()
  stat.value.TvChartLabels.forEach((d, i) => {
    tvMap.set(d, stat.value.TvNums[i] || 0)
    animeMap.set(d, stat.value.AnimeNums[i] || 0)
  })
  const dates = Array.from(new Set([...stat.value.MovieChartLabels, ...stat.value.TvChartLabels]))
  return dates.map((d) => ({
    date: d,
    movie: movieMap.get(d) || 0,
    tv: tvMap.get(d) || 0,
    anime: animeMap.get(d) || 0
  }))
})

const usedPercentNum = computed(() => {
  const v = space.value.UsedPercent
  if (v === undefined || v === null || v === '') return 0
  const n = typeof v === 'number' ? v : Number.parseFloat(v)
  return Number.isFinite(n) ? n : 0
})

function pct(v: number): number {
  return Math.round((v / statMax.value) * 100)
}

async function load() {
  loading.value = true
  const [mc, sp, st, ph] = await Promise.allSettled([
    getLibraryMediacount(),
    getLibrarySpacesize(),
    getTransferStatistics(),
    getLibraryPlayhistory()
  ])
  if (mc.status === 'fulfilled') mediaCount.value = mc.value
  if (sp.status === 'fulfilled') space.value = sp.value
  if (st.status === 'fulfilled') stat.value = st.value
  if (ph.status === 'fulfilled' && ph.value.code === 0) history.value = ph.value.result || []
  if (mc.status === 'rejected') modal.error('媒体数量加载失败')
  loading.value = false
}

onMounted(load)
</script>

<template>
  <div class="home" v-loading="loading">
    <PageHeader title="我的媒体库" description="系统概览与媒体库统计">
      <template #actions>
        <el-button :icon="Refresh" @click="load" :loading="loading">刷新</el-button>
      </template>
    </PageHeader>

    <el-alert
      v-if="!loading && !serverOk"
      type="error"
      title="媒体服务器连接失败"
      description="当前无法连接媒体服务器获取数据，请确认 Emby/Jellyfin/Plex 配置是否正确。"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    />

    <el-row :gutter="16" class="stat-row">
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-primary)"><Film /></el-icon>
            <div class="stat-body">
              <div class="stat-label">电影</div>
              <div class="stat-value">{{ mediaCount.Movie || 0 }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-success)"><Monitor /></el-icon>
            <div class="stat-body">
              <div class="stat-label">电视剧/动漫</div>
              <div class="stat-value">{{ mediaCount.Series || 0 }}</div>
              <div class="stat-sub">集数 {{ mediaCount.Episodes || 0 }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-warning)"><Headset /></el-icon>
            <div class="stat-body">
              <div class="stat-label">音乐</div>
              <div class="stat-value">{{ mediaCount.Music || 0 }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-danger)"><User /></el-icon>
            <div class="stat-body">
              <div class="stat-label">活跃用户</div>
              <div class="stat-value">{{ mediaCount.User ?? 0 }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="block">
      <div class="space-head">
        <span>存储空间共 <strong>{{ space.TotalSpace || '-' }}</strong></span>
        <span class="space-detail">
          已使用 <strong>{{ space.UsedSapce || '-' }}</strong>
          ，空闲 <strong>{{ space.FreeSpace || '-' }}</strong>
        </span>
      </div>
      <el-progress
        :percentage="usedPercentNum"
        :stroke-width="14"
        :format="() => `已使用 ${usedPercentNum}%`"
      />
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        <span class="block-title">转移统计（近 30 天）</span>
      </template>
      <el-row :gutter="16" class="transfer-summary">
        <el-col :xs="24" :sm="8">
          <div class="transfer-item">
            <div class="transfer-name">
              <el-icon color="var(--el-color-primary)"><Film /></el-icon>
              <span>电影</span>
              <span class="transfer-count">{{ totalStat.movie }}</span>
            </div>
            <el-progress :percentage="pct(totalStat.movie)" :show-text="false" />
          </div>
        </el-col>
        <el-col :xs="24" :sm="8">
          <div class="transfer-item">
            <div class="transfer-name">
              <el-icon color="var(--el-color-success)"><Monitor /></el-icon>
              <span>电视剧</span>
              <span class="transfer-count">{{ totalStat.tv }}</span>
            </div>
            <el-progress :percentage="pct(totalStat.tv)" :show-text="false" color="var(--el-color-success)" />
          </div>
        </el-col>
        <el-col :xs="24" :sm="8">
          <div class="transfer-item">
            <div class="transfer-name">
              <el-icon color="var(--el-color-warning)"><VideoPlay /></el-icon>
              <span>动漫</span>
              <span class="transfer-count">{{ totalStat.anime }}</span>
            </div>
            <el-progress :percentage="pct(totalStat.anime)" :show-text="false" color="var(--el-color-warning)" />
          </div>
        </el-col>
      </el-row>
      <el-table :data="statRows" size="small" max-height="320" empty-text="暂无转移数据" style="margin-top: 12px">
        <el-table-column prop="date" label="日期" min-width="120" />
        <el-table-column prop="movie" label="电影" width="90" align="center" />
        <el-table-column prop="tv" label="电视剧" width="90" align="center" />
        <el-table-column prop="anime" label="动漫" width="90" align="center" />
      </el-table>
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        <span class="block-title">播放历史</span>
      </template>
      <el-table :data="history" size="small" empty-text="暂无播放记录">
        <el-table-column label="事件" min-width="320">
          <template #default="{ row }">
            <div class="history-row">
              <el-icon class="history-icon">
                <User v-if="row.type === 'LG'" />
                <VideoPlay v-else />
              </el-icon>
              <span class="history-event">{{ row.event }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="date" label="时间" width="180" />
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.home {
  padding: 16px;
}
.stat-row {
  margin-bottom: 16px;
}
.stat-row .el-col {
  margin-bottom: 12px;
}
.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
}
.stat-icon {
  font-size: 34px;
}
.stat-body {
  flex: 1;
  min-width: 0;
}
.stat-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.stat-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.2;
}
.stat-sub {
  font-size: 12px;
  color: var(--el-color-success);
}
.block {
  margin-bottom: 16px;
}
.block-title {
  font-weight: 600;
}
.space-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
  font-size: 14px;
}
.space-detail {
  color: var(--el-text-color-secondary);
}
.transfer-summary .el-col {
  margin-bottom: 8px;
}
.transfer-item {
  padding: 0 4px;
}
.transfer-name {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  font-size: 13px;
}
.transfer-count {
  margin-left: auto;
  font-weight: 600;
}
.history-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.history-icon {
  color: var(--el-color-primary);
  flex-shrink: 0;
}
.history-event {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
