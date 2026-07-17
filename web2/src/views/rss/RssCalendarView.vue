<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh, Film, Film as FilmIcon } from '@element-plus/icons-vue'
import { doAction } from '@/api'
import { getMovieRssList, getTvRssList, type RssMediaItem } from '@/api/rss'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'

interface CalendarEvent {
  id: string | number
  title: string
  start: string
  poster?: string
  vote_average?: string | number
  year?: string
  type: string
  rssid?: string | number
}

const modal = useModalStore()
const loading = ref(false)
const calendarDate = ref(new Date())
const events = ref<CalendarEvent[]>([])

onMounted(load)

async function load() {
  loading.value = true
  try {
    const [movieRes, tvRes] = await Promise.all([getMovieRssList(), getTvRssList()])
    const movieItems: RssMediaItem[] = movieRes.code === 0 ? Object.values(movieRes.result || {}) : []
    const tvItems: RssMediaItem[] = tvRes.code === 0 ? Object.values(tvRes.result || {}) : []

    const evs: CalendarEvent[] = []
    await Promise.all(
      movieItems.map(async (m) => {
        try {
          const res = await doAction<{ code: number; id?: string | number; title?: string; start?: string; poster?: string; vote_average?: string | number; year?: string; type?: string; rssid?: string | number }>(
            'movie_calendar_data',
            { id: m.id, rssid: m.id }
          )
          if (res.code === 0 && res.start) {
            evs.push({
              id: res.id ?? m.id,
              title: res.title ?? m.name,
              start: res.start,
              poster: res.poster,
              vote_average: res.vote_average,
              year: res.year,
              type: res.type ?? '电影',
              rssid: res.rssid ?? m.id
            })
          }
        } catch {
          /* ignore */
        }
      })
    )
    await Promise.all(
      tvItems.map(async (t) => {
        try {
          const res = await doAction<{ code: number; events?: CalendarEvent[] }>('tv_calendar_data', {
            id: t.id,
            season: t.season,
            name: t.name,
            rssid: t.id
          })
          if (res.code === 0 && res.events) {
            for (const e of res.events) evs.push(e)
          }
        } catch {
          /* ignore */
        }
      })
    )
    events.value = evs
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取日历数据失败')
  } finally {
    loading.value = false
  }
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : `${n}`
}

function dateKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function eventsOf(date: Date): CalendarEvent[] {
  const key = dateKey(date)
  return events.value.filter((e) => (e.start || '').slice(0, 10) === key)
}

const monthEvents = computed(() => events.value)

function isMovie(e: CalendarEvent): boolean {
  return e.type === '电影' || e.type === 'MOV'
}

function posterStyle(e: CalendarEvent): Record<string, string> {
  if (!e.poster) return {}
  return { backgroundImage: `url(${e.poster})` }
}
</script>

<template>
  <div class="rss-calendar" v-loading="loading">
    <PageHeader title="订阅日历" description="电影/电视剧上线日程">
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <el-calendar v-model="calendarDate">
        <template #date-cell="{ data }">
          <div class="calendar-cell">
            <div class="day-num" :class="{ today: data.isSelected }">{{ data.day.split('-').slice(2).join('') }}</div>
            <div class="event-list">
              <div
                v-for="e in eventsOf(new Date(data.day))"
                :key="`${e.id}-${e.start}`"
                class="event-item"
                :class="isMovie(e) ? 'movie' : 'tv'"
              >
                <div class="event-poster" :style="posterStyle(e)">
                  <el-icon v-if="!e.poster"><FilmIcon /></el-icon>
                </div>
                <div class="event-info">
                  <div class="event-title" :title="e.title">{{ e.title }}</div>
                  <div class="event-meta">
                    <el-tag size="small" :type="isMovie(e) ? 'success' : 'primary'" effect="plain">
                      {{ e.type }}
                    </el-tag>
                    <span v-if="e.vote_average" class="vote">★ {{ e.vote_average }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </el-calendar>
    </el-card>

    <div class="legend">
      <el-tag type="success" effect="dark">电影</el-tag>
      <el-tag type="primary" effect="dark">电视剧</el-tag>
      <span class="muted">共 {{ monthEvents.length }} 个订阅事件</span>
    </div>
  </div>
</template>

<style scoped>
.rss-calendar {
  padding: 16px;
}
.calendar-cell {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.day-num {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: right;
  padding: 2px 4px;
}
.day-num.today {
  color: var(--el-color-primary);
  font-weight: 600;
}
.event-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow: hidden;
}
.event-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 4px;
  border-left: 3px solid var(--el-color-primary);
  border-radius: 2px;
  background-color: var(--el-fill-color-light);
}
.event-item.movie {
  border-left-color: var(--el-color-success);
}
.event-item.tv {
  border-left-color: var(--el-color-primary);
}
.event-poster {
  width: 28px;
  height: 36px;
  flex-shrink: 0;
  background-size: cover;
  background-position: center;
  background-color: var(--el-fill-color-darker);
  border-radius: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-placeholder);
}
.event-info {
  flex: 1;
  min-width: 0;
}
.event-title {
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.event-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 2px;
}
.vote {
  font-size: 11px;
  color: var(--el-color-warning);
}
.legend {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 0 4px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>