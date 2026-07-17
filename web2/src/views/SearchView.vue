<script setup lang="ts">
import { ref, computed, reactive, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { Search, Loading, Download, Link } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { search, getSearchResult, type SearchResultItem, type TorrentItem } from '@/api/media'
import { useModalStore } from '@/stores/modal'
import { doAction } from '@/api/request'

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

// ---- 高级搜索 ----

const showAdvanced = ref(false)
const advancedForm = reactive({
  type: '',
  name: '',
  year: '',
  season: '',
  restype: '',
  pix: '',
  sp_state: '* *',
  rule: ''
})

const restypeDict: Record<string, string> = {
  BLURAY: 'BluRay',
  REMUX: 'REMUX',
  DOLBY: 'Dolby',
  WEB: 'WEB-DL',
  HDTV: 'HDTV',
  UHD: 'UHD',
  HDR: 'HDR',
  '3D': '3D'
}

const pixDict: Record<string, string> = {
  '8k': '8K',
  '4k': '4K',
  '1080p': '1080p',
  '720p': '720p'
}

const spStates = [
  { value: '* *', label: '全部' },
  { value: '1.0 1.0', label: '普通' },
  { value: '1.0 0.0', label: '免费' },
  { value: '2.0 1.0', label: '2X' },
  { value: '2.0 0.0', label: '2X免费' },
  { value: '1.0 0.5', label: '50%' },
  { value: '2.0 0.5', label: '2X 50%' },
  { value: '1.0 0.7', label: '70%' },
  { value: '1.0 0.3', label: '30%' }
]

const seasonOptions = computed(() => {
  const options = [{ value: '', label: '全部' }]
  for (let i = 1; i <= 20; i++) {
    options.push({ value: `S${i.toString().padStart(2, '0')}`, label: `第${i}季` })
  }
  return options
})

interface RuleOption {
  id: number
  name: string
}

const filterRules = ref<RuleOption[]>([])

async function loadFilterRules() {
  try {
    const res: any = await doAction('get_filterrules', {})
    if (res.code === 0) {
      filterRules.value = (res.ruleGroups || []).map((g: any) => ({
        id: g.id,
        name: g.name
      }))
    }
  } catch {
    // ignore
  }
}

function openAdvancedDialog() {
  advancedForm.type = ''
  advancedForm.name = keyword.value || ''
  advancedForm.year = ''
  advancedForm.season = ''
  advancedForm.restype = ''
  advancedForm.pix = ''
  advancedForm.sp_state = '* *'
  advancedForm.rule = ''
  loadFilterRules()
  showAdvanced.value = true
}

async function doAdvancedSearch() {
  const name = advancedForm.name.trim()
  if (!name) {
    modal.warning('请输入电影/电视剧名称')
    return
  }
  let kw = name
  if (advancedForm.type) {
    kw = advancedForm.type + ' ' + name
  }
  if (advancedForm.year) {
    kw = kw + ' ' + advancedForm.year
  }
  if (advancedForm.season) {
    kw = kw + ' ' + advancedForm.season
  }
  const filters: Record<string, string> = {}
  if (advancedForm.restype) filters.restype = advancedForm.restype
  if (advancedForm.pix) filters.pix = advancedForm.pix
  if (advancedForm.sp_state && advancedForm.sp_state !== '* *') filters.sp_state = advancedForm.sp_state
  if (advancedForm.rule) filters.rule = advancedForm.rule
  showAdvanced.value = false
  keyword.value = kw
  stopPolling()
  searching.value = true
  results.value = []
  total.value = 0
  lastTotal = -1
  stableCount = 0
  try {
    const res = await search({ search_word: kw, filters, unident: true })
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

// ---- 基础搜索 ----

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
        <el-button @click="openAdvancedDialog">高级搜索</el-button>
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
            <el-table-column label="操作" width="160" align="center">
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

    <el-dialog v-model="showAdvanced" title="高级搜索" width="750px" destroy-on-close>
      <el-form label-width="60px">
        <el-row :gutter="16">
          <el-col :span="6">
            <el-form-item label="类型">
              <el-select v-model="advancedForm.type" style="width: 100%">
                <el-option label="全部" value="" />
                <el-option label="电影" value="电影" />
                <el-option label="电视剧" value="电视剧" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="10">
            <el-form-item label="名称">
              <el-input v-model="advancedForm.name" placeholder="电影/电视剧名称" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="年份">
              <el-input v-model="advancedForm.year" placeholder="20xx" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="季">
              <el-select v-model="advancedForm.season" style="width: 100%">
                <el-option
                  v-for="opt in seasonOptions"
                  :key="opt.value"
                  :label="opt.label"
                  :value="opt.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="质量">
              <el-select v-model="advancedForm.restype" style="width: 100%">
                <el-option label="全部" value="" />
                <el-option
                  v-for="(label, key) in restypeDict"
                  :key="key"
                  :label="label"
                  :value="key"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="分辨率">
              <el-select v-model="advancedForm.pix" style="width: 100%">
                <el-option label="全部" value="" />
                <el-option
                  v-for="(label, key) in pixDict"
                  :key="key"
                  :label="label"
                  :value="key"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="促销">
              <el-select v-model="advancedForm.sp_state" style="width: 100%">
                <el-option
                  v-for="s in spStates"
                  :key="s.value"
                  :label="s.label"
                  :value="s.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="24">
            <el-form-item label="规则">
              <el-select v-model="advancedForm.rule" style="width: 100%">
                <el-option label="全部" value="" />
                <el-option
                  v-for="r in filterRules"
                  :key="r.id"
                  :label="r.name"
                  :value="r.id"
                />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="showAdvanced = false">取消</el-button>
        <el-button type="primary" @click="doAdvancedSearch">开始搜索</el-button>
      </template>
    </el-dialog>
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
