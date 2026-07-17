<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { Search, Loading, Download, Link } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { search, getSearchResult, type SearchResultItem, type TorrentItem } from '@/api/media'
import { useModalStore } from '@/stores/modal'

const route = useRoute()
const modal = useModalStore()

const keyword = ref((route.query.q as string) || '')
const searching = ref(false)
const results = ref<SearchResultItem[]>([])
const total = ref(0)

let pollTimer: ReturnType<typeof setInterval> | null = null
let pollStart = 0
let lastTotal = -1
let stableCount = 0

const resultItems = computed(() => Object.values(results.value))
const hasResults = computed(() => resultItems.value.length > 0)

interface FlatTorrent extends TorrentItem {
  groupRestype: string
  groupRespix: string
  season: string
}

function flattenSeason(item: SearchResultItem): Array<{ season: string; torrents: FlatTorrent[] }> {
  return item.torrent_dict.map(([seKey, seasonDict]) => {
    const torrents: FlatTorrent[] = []
    for (const [, group] of Object.entries(seasonDict)) {
      for (const [, unique] of Object.entries(group.group_torrents)) {
        for (const t of unique.torrent_list) {
          torrents.push({
            ...t,
            groupRestype: group.group_info.restype,
            groupRespix: group.group_info.respix,
            season: seKey
          })
        }
      }
    }
    return { season: seKey, torrents }
  })
}

function freeText(t: TorrentItem): { text: string; type: string } | null {
  if (t.downloadvalue === 0) return { text: 'FREE', type: 'success' }
  if (t.downloadvalue !== 1) return { text: `${Math.round(t.downloadvalue * 100)}%DL`, type: 'info' }
  return null
}

function uploadText(t: TorrentItem): { text: string; type: string } | null {
  if (t.uploadvalue !== 1) return { text: `${Math.round(t.uploadvalue * 100)}%UL`, type: 'warning' }
  return null
}

function openTorrent(t: TorrentItem) {
  if (t.enclosure && /^https?:\/\//.test(t.enclosure)) {
    window.open(t.enclosure, '_blank')
  } else if (t.pageurl) {
    window.open(t.pageurl, '_blank')
  } else {
    modal.info('无可用下载链接')
  }
}

function openPage(url: string) {
  if (url) window.open(url, '_blank')
}

async function doSearch() {
  const q = keyword.value.trim()
  if (!q) {
    modal.warning('请输入搜索关键字')
    return
  }
  stopPolling()
  searching.value = true
  results.value = []
  total.value = 0
  lastTotal = -1
  stableCount = 0
  try {
    const res = await search({ search_word: q })
    if (res.code !== 0 && res.msg) {
      modal.error(res.msg)
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '搜索请求失败')
  }
  pollStart = Date.now()
  await fetchResults()
  startPolling()
}

async function fetchResults() {
  try {
    const res = await getSearchResult()
    if (res.code === 0) {
      total.value = res.total || 0
      results.value = Object.values(res.result || {})
      if (total.value === lastTotal) {
        stableCount += 1
      } else {
        stableCount = 0
        lastTotal = total.value
      }
      if (stableCount >= 2 && total.value > 0) {
        stopPolling()
      }
    }
  } catch {
    // ignore transient errors during polling
  }
  if (Date.now() - pollStart > 30000) {
    stopPolling()
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(fetchResults, 1500)
}

function stopPolling() {
  searching.value = false
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  if (keyword.value) doSearch()
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="search-view">
    <PageHeader title="资源搜索" :description="hasResults ? `共搜索到 ${total} 条记录` : ''">
      <template #actions>
        <el-input
          v-model="keyword"
          placeholder="输入电影/电视剧名称..."
          clearable
          style="width: 320px"
          @keyup.enter="doSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button type="primary" :icon="Search" :loading="searching" @click="doSearch">
          {{ searching ? '搜索中' : '搜索' }}
        </el-button>
        <el-button v-if="searching" @click="stopPolling">停止</el-button>
      </template>
    </PageHeader>

    <div v-if="searching && !hasResults" class="loading-tip">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在搜索资源...</span>
    </div>

    <el-empty
      v-else-if="!hasResults"
      description="输入想看的电影、电视剧名称，点击搜索试试看吧。"
    />

    <div v-else class="result-list">
      <el-card
        v-for="item in resultItems"
        :key="item.key"
        class="result-card"
        shadow="never"
      >
        <div class="result-head">
          <img
            v-if="item.poster"
            :src="item.poster"
            :alt="item.title"
            class="result-poster"
            @error="($event.target as HTMLImageElement).style.display = 'none'"
          />
          <div class="result-info">
            <div class="result-title-row">
              <h3 class="result-title">{{ item.title }}</h3>
              <el-tag v-if="item.exist" type="success" size="small" effect="plain">已存在</el-tag>
            </div>
            <div class="result-meta">
              <el-tag v-if="item.type" size="small" type="info">{{ item.type }}</el-tag>
              <span v-if="item.vote && item.vote !== '0'" class="meta-text">评分 {{ item.vote }}</span>
              <a
                v-if="item.tmdbid && item.tmdbid !== '0'"
                :href="item.type === '电影' ? `https://www.themoviedb.org/movie/${item.tmdbid}` : `https://www.themoviedb.org/tv/${item.tmdbid}`"
                target="_blank"
                class="meta-link"
              >TMDB: {{ item.tmdbid }}</a>
            </div>
            <p v-if="item.overview" class="result-overview">{{ item.overview }}</p>
          </div>
        </div>

        <div
          v-for="sec in flattenSeason(item)"
          :key="`${item.key}-${sec.season}`"
          class="season-block"
        >
          <div v-if="sec.season !== 'MOV'" class="season-title">{{ sec.season }}</div>
          <el-table :data="sec.torrents" size="small" stripe>
            <el-table-column label="站点" width="90">
              <template #default="{ row }">
                <span class="site-cell">{{ row.site }}</span>
              </template>
            </el-table-column>
            <el-table-column label="种子名称" min-width="280">
              <template #default="{ row }">
                <div class="torrent-name">{{ row.torrent_name }}</div>
                <div v-if="row.description" class="torrent-desc">{{ row.description }}</div>
                <div class="torrent-badges">
                  <el-tag v-if="row.groupRestype" size="small" type="danger">{{ row.groupRestype }}</el-tag>
                  <el-tag v-if="row.groupRespix" size="small" type="warning">{{ row.groupRespix }}</el-tag>
                  <el-tag v-if="row.video_encode" size="small">{{ row.video_encode }}</el-tag>
                  <el-tag v-if="row.size" size="small" type="info">{{ row.size }}</el-tag>
                  <el-tag v-if="row.releasegroup" size="small" type="info">{{ row.releasegroup }}</el-tag>
                  <el-tag
                    v-if="uploadText(row)"
                    size="small"
                    :type="uploadText(row)!.type"
                  >{{ uploadText(row)!.text }}</el-tag>
                  <el-tag
                    v-if="freeText(row)"
                    size="small"
                    :type="freeText(row)!.type"
                  >{{ freeText(row)!.text }}</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="大小" width="90" prop="size" />
            <el-table-column label="做种" width="70" align="center">
              <template #default="{ row }">
                <span v-if="row.seeders">{{ row.seeders }}↑</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" align="center">
              <template #default="{ row }">
                <el-button size="small" type="primary" :icon="Download" @click="openTorrent(row)">
                  下载
                </el-button>
                <el-button
                  v-if="row.pageurl"
                  size="small"
                  :icon="Link"
                  @click="openPage(row.pageurl)"
                />
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.search-view {
  padding: 16px;
}
.loading-tip {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
  padding: 40px 0;
  justify-content: center;
}
.result-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.result-card {
  border-radius: 8px;
}
.result-head {
  display: flex;
  gap: 16px;
  margin-bottom: 16px;
}
.result-poster {
  width: 80px;
  height: 120px;
  object-fit: cover;
  border-radius: 6px;
  flex-shrink: 0;
}
.result-info {
  flex: 1;
  min-width: 0;
}
.result-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.result-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}
.result-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.meta-link {
  color: var(--el-color-primary);
  text-decoration: none;
}
.meta-link:hover {
  text-decoration: underline;
}
.result-overview {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.season-block {
  margin-top: 12px;
}
.season-title {
  font-weight: 600;
  font-size: 15px;
  margin: 8px 0;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
}
.site-cell {
  font-weight: 600;
}
.torrent-name {
  font-size: 13px;
  word-break: break-all;
}
.torrent-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.torrent-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
</style>
