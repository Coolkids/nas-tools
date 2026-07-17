<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Loading } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import MediaCard from '@/components/MediaCard.vue'
import { personMedias, proxyDoubanImage, type RecommendItem } from '@/api/discovery'
import { useModalStore } from '@/stores/modal'

const route = useRoute()
const router = useRouter()
const modal = useModalStore()

const personId = computed(() => (route.query.id as string) || (route.query.tmdbid as string) || '')
const personName = computed(() => (route.query.name as string) || (route.query.title as string) || '人物')

const creditType = ref<'MOV' | 'TV'>(
  ((route.query.type as string) || 'MOV').toUpperCase() === 'TV' ? 'TV' : 'MOV'
)

const items = ref<RecommendItem[]>([])
const page = ref(1)
const loading = ref(false)
const noMore = ref(false)

function switchType(t: 'MOV' | 'TV') {
  if (t === creditType.value) return
  router.replace({
    path: route.path,
    query: { ...route.query, type: t }
  })
}

async function loadPage() {
  if (loading.value || noMore.value || !personId.value) return
  loading.value = true
  try {
    const res = await personMedias(personId.value, creditType.value, page.value)
    if (res.code === 0) {
      const list = res.data || []
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
  loadPage()
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

onMounted(() => {
  reset()
  window.addEventListener('scroll', onScroll, { passive: true })
})

onBeforeUnmount(() => {
  window.removeEventListener('scroll', onScroll)
})

watch(personId, () => reset())
watch(creditType, () => reset())

watch(
  () => route.query.type,
  (t) => {
    const next = ((t as string) || 'MOV').toUpperCase() === 'TV' ? 'TV' : 'MOV'
    if (next !== creditType.value) creditType.value = next
  }
)
</script>

<template>
  <div class="person-view">
    <PageHeader title="人物" />

    <div class="person-header">
      <el-avatar :size="88" class="person-avatar">
        {{ personName.charAt(0) || '?' }}
      </el-avatar>
      <div class="person-meta">
        <h2 class="person-name">{{ personName }}</h2>
        <el-radio-group :model-value="creditType" size="default" @change="switchType($event as 'MOV' | 'TV')">
          <el-radio-button value="MOV">参演电影</el-radio-button>
          <el-radio-button value="TV">参演剧集</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <el-empty
      v-if="!loading && items.length === 0"
      description="暂无作品"
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
.person-view {
  padding: 16px;
}
.person-header {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 16px;
  margin-bottom: 24px;
  background: var(--el-fill-color-light);
  border-radius: 12px;
}
.person-avatar {
  flex-shrink: 0;
  background-color: var(--el-color-primary);
  color: #fff;
  font-size: 32px;
  font-weight: 600;
}
.person-meta {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.person-name {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
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
