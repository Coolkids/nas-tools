<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Loading } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import MediaCard from '@/components/MediaCard.vue'
import { getRecommend, proxyDoubanImage, type RecommendItem } from '@/api/discovery'
import { useModalStore } from '@/stores/modal'

interface TypeConfig {
  type: string
  subtype?: string
  title: string
  subtitle?: string
  week?: string
  tmdbid?: string
  personid?: string
  keyword?: string
  source?: string
}

const ROUTE_TYPE_MAP: Record<string, TypeConfig> = {
  douban_movie: { type: 'DOUBANTAG', subtype: 'MOV', title: '豆瓣电影' },
  douban_tv: { type: 'DOUBANTAG', subtype: 'TV', title: '豆瓣剧集' },
  tmdb_movie: { type: 'DISCOVER', subtype: 'MOV', title: 'TMDB电影' },
  tmdb_tv: { type: 'DISCOVER', subtype: 'TV', title: 'TMDB剧集' },
  downloaded: { type: 'DOWNLOADED', title: '最近下载' }
}

const TABS: Array<{ label: string; name: string }> = [
  { label: '推荐', name: 'recommend' },
  { label: '豆瓣电影', name: 'douban_movie' },
  { label: '豆瓣剧集', name: 'douban_tv' },
  { label: 'TMDB电影', name: 'tmdb_movie' },
  { label: 'TMDB剧集', name: 'tmdb_tv' }
]

const route = useRoute()
const router = useRouter()
const modal = useModalStore()

const items = ref<RecommendItem[]>([])
const page = ref(1)
const loading = ref(false)
const noMore = ref(false)
const initializing = ref(false)

const typeConfig = computed<TypeConfig>(() => {
  const name = route.name as string
  if (name === 'recommend') {
    const q = route.query
    const type = (q.type as string) || 'TRENDING'
    return {
      type,
      subtype: (q.subtype as string) || undefined,
      title: (q.title as string) || '推荐',
      subtitle: (q.subtitle as string) || undefined,
      week: (q.week as string) || undefined,
      tmdbid: (q.tmdbid as string) || undefined,
      personid: (q.personid as string) || undefined,
      keyword: (q.keyword as string) || undefined,
      source: (q.source as string) || undefined
    }
  }
  return ROUTE_TYPE_MAP[name] || { type: 'TRENDING', title: '推荐' }
})

const activeTab = computed(() => route.name as string)

function switchTab(name: string) {
  if (name === activeTab.value) return
  router.push({ name })
}

async function loadPage() {
  if (loading.value || noMore.value) return
  loading.value = true
  try {
    const cfg = typeConfig.value
    const res = await getRecommend({
      type: cfg.type,
      subtype: cfg.subtype,
      page: page.value,
      week: cfg.week,
      tmdbid: cfg.tmdbid,
      personid: cfg.personid,
      keyword: cfg.keyword,
      source: cfg.source
    })
    if (res.code === 0) {
      const list = res.Items || []
      if (list.length === 0) {
        noMore.value = true
      } else {
        items.value.push(...list)
        page.value += 1
        if (list.length < 20) noMore.value = true
      }
    } else {
      modal.error(res.msg || '加载失败')
      noMore.value = true
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '加载失败')
    noMore.value = true
  } finally {
    loading.value = false
  }
}

function reset() {
  items.value = []
  page.value = 1
  noMore.value = false
  initializing.value = true
  loadPage().finally(() => {
    initializing.value = false
  })
}

function onScroll() {
  if (loading.value || noMore.value) return
  const scrollTop = document.documentElement.scrollTop || document.body.scrollTop
  const scrollHeight = document.documentElement.scrollHeight
  const clientHeight = document.documentElement.clientHeight
  if (scrollHeight - clientHeight - scrollTop < 200) {
    loadPage()
  }
}

function onFavChange(idx: number, fav: string) {
  if (items.value[idx]) items.value[idx].fav = fav
}

onMounted(() => {
  reset()
  window.addEventListener('scroll', onScroll, { passive: true })
})

onBeforeUnmount(() => {
  window.removeEventListener('scroll', onScroll)
})

watch(
  () => [route.name, route.query.type, route.query.subtype, route.query.week, route.query.tmdbid, route.query.personid, route.query.keyword],
  () => reset()
)
</script>

<template>
  <div class="recommend-view">
    <PageHeader :title="typeConfig.title" :description="typeConfig.subtitle">
      <template #actions>
        <el-radio-group :model-value="activeTab" size="default" @change="switchTab($event as string)">
          <el-radio-button v-for="tab in TABS" :key="tab.name" :value="tab.name">
            {{ tab.label }}
          </el-radio-button>
        </el-radio-group>
      </template>
    </PageHeader>

    <el-empty
      v-if="!initializing && !loading && items.length === 0"
      description="没有数据"
    />

    <div v-else class="media-grid">
      <MediaCard
        v-for="(item, idx) in items"
        :key="`${item.id}-${idx}`"
        :tmdb-id="item.id"
        :title="item.title"
        :image="proxyDoubanImage(item.image)"
        :fav="item.fav"
        :vote="item.vote"
        :year="item.year"
        :overview="item.overview"
        :date="item.date"
        :media-type="item.type"
        :res-type="item.media_type"
        :show-sub="'1'"
        :site="item.site"
        :weekday="item.weekday"
        @fav-change="onFavChange(idx, $event)"
      />
    </div>

    <div v-if="loading" class="load-tip">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>加载中...</span>
    </div>
    <div v-else-if="noMore && items.length > 0" class="load-tip">
      <span>没有更多了</span>
    </div>
  </div>
</template>

<style scoped>
.recommend-view {
  padding: 16px;
}
.media-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
}
.load-tip {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 24px 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
