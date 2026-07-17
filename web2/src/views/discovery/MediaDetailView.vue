<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Search, Star, Check, Loading } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import MediaCard from '@/components/MediaCard.vue'
import PersonCard from '@/components/PersonCard.vue'
import {
  mediaDetail,
  mediaRecommendations,
  mediaPerson,
  proxyDoubanImage,
  type MediaDetail,
  type RecommendItem,
  type PersonItem
} from '@/api/discovery'
import { useModalStore } from '@/stores/modal'

const route = useRoute()
const modal = useModalStore()

const media = ref<MediaDetail | null>(null)
const recommendations = ref<RecommendItem[]>([])
const persons = ref<PersonItem[]>([])
const loading = ref(false)
const loadingRec = ref(false)
const loadingPerson = ref(false)
const errMsg = ref('')

async function loadAll() {
  const type = (route.query.type as string) || 'movie'
  const id = (route.query.id as string) || ''
  if (!id) {
    errMsg.value = '未指定媒体ID'
    return
  }
  loading.value = true
  errMsg.value = ''
  media.value = null
  recommendations.value = []
  persons.value = []
  try {
    const res = await mediaDetail(type, id)
    if (res.code !== 0 || !res.data) {
      errMsg.value = res.msg || '未查询到TMDB媒体信息'
      return
    }
    media.value = res.data
    const tmdbid = res.data.tmdbid
    loadingRec.value = true
    loadingPerson.value = true
    mediaRecommendations(type, tmdbid)
      .then((r) => {
        if (r.code === 0) recommendations.value = r.data || []
      })
      .finally(() => (loadingRec.value = false))
    mediaPerson(type, tmdbid)
      .then((r) => {
        if (r.code === 0) persons.value = r.data || []
      })
      .finally(() => (loadingPerson.value = false))
  } catch (e) {
    errMsg.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function onSearchResource() {
  if (!media.value) return
  modal.info(`搜索资源：${media.value.title}（订阅/下载逻辑后续实现）`)
}

function onToggleFav() {
  if (!media.value) return
  const next = media.value.fav === '1' ? '0' : '1'
  media.value.fav = next
  modal.success(next === '1' ? '已加入订阅' : '已取消订阅')
}

onMounted(loadAll)
watch(() => [route.query.type, route.query.id], loadAll)
</script>

<template>
  <div class="media-detail" v-loading="loading">
    <PageHeader title="媒体详情" />

    <el-alert v-if="errMsg" type="error" :title="errMsg" :closable="false" show-icon />

    <template v-else-if="media">
      <div class="backdrop">
        <img
          v-if="media.background"
          :src="media.background"
          :alt="media.title"
          class="backdrop-img"
          @error="($event.target as HTMLImageElement).style.display = 'none'"
        />
        <div class="backdrop-mask" />
        <div class="backdrop-content">
          <div class="poster-wrap">
            <img
              v-if="media.image"
              :src="media.image"
              :alt="media.title"
              class="poster"
              @error="($event.target as HTMLImageElement).style.display = 'none'"
            />
            <div v-else class="poster placeholder">
              <el-icon :size="40"><Film /></el-icon>
            </div>
          </div>
          <div class="info">
            <div v-if="media.fav === '2'" class="fav-badge">
              <el-icon><Check /></el-icon> 已下载
            </div>
            <h1 class="title">
              {{ media.title }}
              <span v-if="media.year" class="year">({{ media.year }})</span>
            </h1>
            <div class="meta">
              <el-tag v-if="media.tmdbid" size="small" type="success">TMDB: {{ media.tmdbid }}</el-tag>
              <el-tag v-if="media.vote && media.vote !== '0'" size="small" type="warning">
                评分 {{ media.vote }}
              </el-tag>
              <span v-if="media.runtime">{{ media.runtime }}</span>
              <span v-if="media.genres">| {{ media.genres }}</span>
            </div>
            <div class="actions">
              <el-button type="primary" :icon="Search" @click="onSearchResource">搜索资源</el-button>
              <el-button
                v-if="media.fav === '1'"
                type="danger"
                :icon="Star"
                @click="onToggleFav"
              >删除订阅</el-button>
              <el-button v-else :icon="Star" @click="onToggleFav">添加订阅</el-button>
            </div>
          </div>
        </div>
      </div>

      <div class="body-row">
        <div class="overview-col">
          <h3 class="block-title">简介</h3>
          <p class="overview">{{ media.overview || '暂无简介' }}</p>

          <div v-if="media.crews && media.crews.length" class="crews">
            <div v-for="(crew, idx) in media.crews" :key="idx" class="crew-item">
              <strong>{{ Object.keys(crew)[0] }}</strong>
              <span class="text-muted">{{ Object.values(crew)[0] }}</span>
            </div>
          </div>
        </div>

        <div v-if="media.fact && media.fact.length" class="fact-col">
          <h3 class="block-title">发布信息</h3>
          <div class="fact-card">
            <div v-for="(fact, idx) in media.fact" :key="idx" class="fact-row">
              <div class="fact-label">{{ Object.keys(fact)[0] }}</div>
              <div class="fact-value">{{ Object.values(fact)[0] }}</div>
            </div>
          </div>
        </div>
      </div>

      <section class="block">
        <h3 class="block-title">演员阵容</h3>
        <div v-if="loadingPerson" class="loading-tip">
          <el-icon class="is-loading"><Loading /></el-icon> 加载中...
        </div>
        <el-empty
          v-else-if="persons.length === 0"
          :image-size="60"
          description="暂无演员信息"
        />
        <div v-else class="person-grid">
          <PersonCard
            v-for="person in persons"
            :key="person.id"
            :person-id="person.id"
            :image="person.image"
            :name="person.name"
            :role="person.role"
          />
        </div>
      </section>

      <section class="block">
        <h3 class="block-title">推荐影片</h3>
        <div v-if="loadingRec" class="loading-tip">
          <el-icon class="is-loading"><Loading /></el-icon> 加载中...
        </div>
        <el-empty
          v-else-if="recommendations.length === 0"
          :image-size="60"
          description="暂无推荐"
        />
        <div v-else class="media-grid">
          <MediaCard
            v-for="(item, idx) in recommendations"
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
      </section>
    </template>

    <div v-else class="loading-tip">
      <el-icon class="is-loading"><Loading /></el-icon> 加载中...
    </div>
  </div>
</template>

<style scoped>
.media-detail {
  padding: 16px;
}
.backdrop {
  position: relative;
  width: 100%;
  min-height: 360px;
  border-radius: 12px;
  overflow: hidden;
  background-color: var(--el-fill-color-light);
}
.backdrop-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.backdrop-mask {
  position: absolute;
  inset: 0;
  background: linear-gradient(to top, rgba(0, 0, 0, 0.85), rgba(0, 0, 0, 0.2));
}
.backdrop-content {
  position: relative;
  display: flex;
  gap: 20px;
  padding: 24px;
  align-items: flex-end;
  min-height: 360px;
}
.poster-wrap {
  flex-shrink: 0;
  width: 160px;
  aspect-ratio: 2 / 3;
}
.poster {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}
.poster.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-placeholder);
  background: rgba(255, 255, 255, 0.1);
}
.info {
  flex: 1;
  color: #fff;
  min-width: 0;
}
.fav-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #2fb344;
  color: #fff;
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 12px;
  margin-bottom: 8px;
}
.title {
  margin: 0 0 8px;
  font-size: 26px;
  font-weight: 700;
}
.year {
  font-size: 18px;
  font-weight: 400;
  margin-left: 6px;
  opacity: 0.85;
}
.meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
  margin-bottom: 16px;
  opacity: 0.95;
}
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.body-row {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 20px;
  margin-top: 24px;
}
@media (max-width: 768px) {
  .body-row {
    grid-template-columns: 1fr;
  }
  .backdrop-content {
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
}
.block-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 12px;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
}
.overview {
  color: var(--el-text-color-regular);
  line-height: 1.7;
  margin: 0;
  white-space: pre-line;
}
.crews {
  margin-top: 20px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}
.crew-item {
  font-size: 13px;
}
.crew-item strong {
  display: block;
  margin-bottom: 2px;
}
.text-muted {
  color: var(--el-text-color-secondary);
}
.fact-card {
  background: var(--el-fill-color-light);
  border-radius: 8px;
  padding: 8px 12px;
}
.fact-row {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;
}
.fact-row:last-child {
  border-bottom: none;
}
.fact-label {
  font-weight: 600;
  min-width: 25%;
}
.fact-value {
  color: var(--el-text-color-secondary);
  text-align: right;
  word-break: break-all;
}
.block {
  margin-top: 28px;
}
.loading-tip {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 24px 0;
}
.person-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
}
.media-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
}
</style>
