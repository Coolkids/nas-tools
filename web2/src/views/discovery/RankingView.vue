<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Loading } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import MediaCard from '@/components/MediaCard.vue'
import { getRecommend, proxyDoubanImage, type RecommendItem } from '@/api/discovery'

interface DiscoveryCategory {
  type: string
  title: string
  subtype?: string
  week?: string
}

const DISCOVERY_LIST: Record<string, DiscoveryCategory[]> = {
  RANKING: [
    { type: 'MOV', title: '正在热映', subtype: 'dbom' },
    { type: 'MOV', title: '即将上映', subtype: 'dbnm' },
    { type: 'TRENDING', title: 'TMDB流行趋势', subtype: 'tmdb' },
    { type: 'MOV', title: '豆瓣最新电影', subtype: 'dbnm' },
    { type: 'MOV', title: '豆瓣热门电影', subtype: 'dbhm' },
    { type: 'MOV', title: '豆瓣电影TOP250', subtype: 'dbtop' },
    { type: 'TV', title: '豆瓣热门电视剧', subtype: 'dbht' },
    { type: 'TV', title: '华语口碑剧集榜', subtype: 'dbct' },
    { type: 'TV', title: '全球口碑剧集榜', subtype: 'dbgt' }
  ],
  BANGUMI: [
    { type: 'TV', title: '星期一', subtype: 'bangumi', week: '1' },
    { type: 'TV', title: '星期二', subtype: 'bangumi', week: '2' },
    { type: 'TV', title: '星期三', subtype: 'bangumi', week: '3' },
    { type: 'TV', title: '星期四', subtype: 'bangumi', week: '4' },
    { type: 'TV', title: '星期五', subtype: 'bangumi', week: '5' },
    { type: 'TV', title: '星期六', subtype: 'bangumi', week: '6' },
    { type: 'TV', title: '星期日', subtype: 'bangumi', week: '7' }
  ]
}

const route = useRoute()
const router = useRouter()

const discoveryType = computed(() => (route.name === 'bangumi' ? 'BANGUMI' : 'RANKING'))
const activeTab = computed(() => route.name as string)
const categories = computed(() => DISCOVERY_LIST[discoveryType.value] || [])

const slideMap = ref<Record<string, RecommendItem[]>>({})
const loadingMap = ref<Record<string, boolean>>({})

const TABS: Array<{ label: string; name: string }> = [
  { label: '榜单', name: 'ranking' },
  { label: 'Bangumi 每日放送', name: 'bangumi' }
]

function switchTab(name: string) {
  if (name === activeTab.value) return
  router.push({ name })
}

async function loadCategory(cat: DiscoveryCategory) {
  loadingMap.value[cat.title] = true
  try {
    const res = await getRecommend({
      type: cat.type,
      subtype: cat.subtype,
      page: 1,
      week: cat.week
    })
    if (res.code === 0) {
      slideMap.value[cat.title] = res.Items || []
    }
  } catch {
    slideMap.value[cat.title] = []
  } finally {
    loadingMap.value[cat.title] = false
  }
}

function loadAll() {
  slideMap.value = {}
  loadingMap.value = {}
  for (const cat of categories.value) {
    loadCategory(cat)
  }
}

onMounted(loadAll)

watch(
  () => route.name,
  () => loadAll()
)
</script>

<template>
  <div class="ranking-view">
    <PageHeader title="榜单" description="多榜单聚合浏览">
      <template #actions>
        <el-radio-group :model-value="activeTab" size="default" @change="switchTab($event as string)">
          <el-radio-button v-for="tab in TABS" :key="tab.name" :value="tab.name">
            {{ tab.label }}
          </el-radio-button>
        </el-radio-group>
      </template>
    </PageHeader>

    <div class="sections">
      <section v-for="cat in categories" :key="cat.title" class="section">
        <div class="section-title">{{ cat.title }}</div>

        <div v-if="loadingMap[cat.title]" class="section-loading">
          <el-icon class="is-loading"><Loading /></el-icon>
          <span>加载中...</span>
        </div>
        <el-empty
          v-else-if="!slideMap[cat.title] || slideMap[cat.title].length === 0"
          :image-size="60"
          description="暂无数据"
        />
        <div v-else class="slide-row">
          <MediaCard
            v-for="(item, idx) in slideMap[cat.title]"
            :key="`${cat.title}-${item.id}-${idx}`"
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
          />
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.ranking-view {
  padding: 16px;
}
.sections {
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 12px;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
}
.section-loading {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 24px 0;
}
.slide-row {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 160px;
  gap: 12px;
  overflow-x: auto;
  padding-bottom: 8px;
}
.slide-row::-webkit-scrollbar {
  height: 6px;
}
.slide-row::-webkit-scrollbar-thumb {
  background: var(--el-border-color);
  border-radius: 3px;
}
</style>
