<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { Refresh, VideoPause, Tools, Delete, Search, Connection, Promotion } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import { getConfig, type AppConfig } from '@/api/config'
import {
  runScheduler,
  truncateBlacklist,
  truncateRsshistory,
  nameTest,
  netTest,
  NETTEST_TARGETS,
  type NameTestData,
  type NetTestResult
} from '@/api/system'

interface ServiceItem {
  id: string
  name: string
  type: 'scheduler' | 'manual'
  interval: string
  state: boolean
}

const modal = useModalStore()
const loading = ref(false)
const services = ref<ServiceItem[]>([])

const runningCount = computed(() => services.value.filter((s) => s.state).length)
const schedulerCount = computed(() => services.value.filter((s) => s.type === 'scheduler').length)
const manualCount = computed(() => services.value.filter((s) => s.type === 'manual').length)

function asDigit(v: unknown): number | null {
  const s = String(v ?? '')
  return /^\d+$/.test(s) ? Number(s) : null
}

function buildServices(cfg: AppConfig): ServiceItem[] {
  const pt = (cfg.pt || {}) as Record<string, unknown>
  const douban = (cfg.douban || {}) as Record<string, unknown>
  const list: ServiceItem[] = []

  const rss = asDigit(pt.pt_check_interval)
  list.push({
    id: 'rssdownload',
    name: 'RSS订阅',
    type: 'scheduler',
    interval: rss !== null ? `${Math.round(rss / 60)} 分钟` : '未启用',
    state: rss !== null
  })

  let search = asDigit(pt.search_rss_interval)
  if (search !== null && search < 6) search = 6
  list.push({
    id: 'subscribe_search_all',
    name: '订阅搜索',
    type: 'scheduler',
    interval: search !== null ? `${search} 小时` : '未启用',
    state: search !== null
  })

  const monitor = !!pt.pt_monitor
  list.push({
    id: 'pttransfer',
    name: '下载文件转移',
    type: 'scheduler',
    interval: monitor ? '5 分钟' : '未启用',
    state: monitor
  })

  list.push({
    id: 'autoremovetorrents',
    name: '自动删种',
    type: 'scheduler',
    interval: '需配置删种任务',
    state: false
  })

  const signin = pt.ptsignin_cron
  let signinInterval = '未启用'
  if (signin) {
    signinInterval = String(signin).includes(':') ? String(signin) : `${signin} 小时`
  }
  list.push({
    id: 'ptsignin',
    name: '站点签到',
    type: 'scheduler',
    interval: signinInterval,
    state: !!signin
  })

  list.push({
    id: 'sync',
    name: '目录同步',
    type: 'scheduler',
    interval: '实时监控',
    state: true
  })

  const doubanInterval = douban.interval
  list.push({
    id: 'douban',
    name: '豆瓣想看',
    type: 'scheduler',
    interval: doubanInterval ? `${doubanInterval} 小时` : '未启用',
    state: !!doubanInterval
  })

  list.push({ id: 'blacklist', name: '清理转移缓存', type: 'manual', interval: '手动', state: false })
  list.push({ id: 'rsshistory', name: '清理RSS缓存', type: 'manual', interval: '手动', state: false })

  return list
}

async function load() {
  loading.value = true
  try {
    const res = await getConfig()
    if (res.code === 0) {
      services.value = buildServices(res.config || {})
    } else {
      services.value = buildServices({})
    }
  } catch {
    services.value = buildServices({})
    modal.error('加载服务配置失败')
  } finally {
    loading.value = false
  }
}

async function runService(s: ServiceItem) {
  if (s.type === 'manual') {
    if (s.id === 'blacklist') {
      const ok = await modal.confirm(
        '清理文件整理缓存后，已转移过的文件允许重新转移（包括识别错误的文件），是否确认？'
      )
      if (!ok) return
      try {
        await truncateBlacklist()
        modal.success('文件缓存清理完成！')
      } catch {
        modal.error('清理失败')
      }
      return
    }
    if (s.id === 'rsshistory') {
      const ok = await modal.confirm(
        '清理RSS缓存后，已订阅下载但未入库的资源可能会被重新下载，是否确认？'
      )
      if (!ok) return
      try {
        await truncateRsshistory()
        modal.success('RSS缓存清理完成！')
      } catch {
        modal.error('清理失败')
      }
      return
    }
    return
  }
  const ok = await modal.confirm(`是否立即运行 ${s.name}？`)
  if (!ok) return
  try {
    await runScheduler(s.id)
    modal.success(`${s.name} 服务启动成功，正在后台运行`)
  } catch {
    modal.error('服务启动失败')
  }
}

const nameTestVisible = ref(false)
const nameTestInput = ref('')
const nameTestLoading = ref(false)
const nameTestResult = ref<NameTestData | { name: string } | null>(null)

function openNameTest() {
  nameTestInput.value = ''
  nameTestResult.value = null
  nameTestVisible.value = true
}

async function doNameTest() {
  const name = nameTestInput.value.trim()
  if (!name) {
    modal.warning('请输入资源名称')
    return
  }
  nameTestLoading.value = true
  nameTestResult.value = null
  try {
    const res = await nameTest(name)
    if (res.code === 0 && res.data) {
      nameTestResult.value = res.data
    } else {
      nameTestResult.value = { name: '无法识别' }
    }
  } catch {
    modal.error('识别失败')
  } finally {
    nameTestLoading.value = false
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    modal.success('已复制到剪贴板')
  } catch {
    modal.error('复制失败')
  }
}

function openUrl(url: string) {
  if (url) window.open(url, '_blank')
}

function searchTmdb(query: string) {
  if (query) window.open(`https://www.themoviedb.org/search?query=${encodeURIComponent(query)}`, '_blank')
}

function toArray(v: unknown): string[] {
  if (Array.isArray(v)) return v.filter(Boolean)
  if (typeof v === 'string' && v) return [v]
  return []
}

function getReplacedWords(data: NameTestData): string[] {
  return toArray((data as any).replaced_words)
}

function hasReplacedWords(data: NameTestData): boolean {
  return getReplacedWords(data).length > 0
}

function showOrgString(data: NameTestData): boolean {
  if (!data.org_string) return false
  const hasReplaced = hasReplacedWords(data)
  const hasTmdb = !!data.tmdbid
  if (hasReplaced) return true
  if (!hasTmdb) return true
  return false
}

const netTestVisible = ref(false)
const netTestLoading = ref(false)
const netTestResults = ref<{ target: string; res?: boolean; time?: string; testing: boolean }[]>([])

function openNetTest() {
  netTestResults.value = NETTEST_TARGETS.map((t) => ({ target: t, testing: true }))
  netTestLoading.value = true
  netTestVisible.value = true
  Promise.all(
    netTestResults.value.map((r) =>
      netTest(r.target)
        .then((ret: NetTestResult) => {
          r.res = ret.res
          r.time = ret.time
        })
        .catch(() => {
          r.res = false
          r.time = '失败'
        })
        .finally(() => {
          r.testing = false
        })
    )
  ).finally(() => {
    netTestLoading.value = false
  })
}

onMounted(load)
</script>

<template>
  <div class="service" v-loading="loading">
    <PageHeader title="服务" description="定时任务与系统操作">
      <template #actions>
        <el-button :icon="Refresh" @click="load" :loading="loading">刷新</el-button>
      </template>
    </PageHeader>

    <el-row :gutter="16" class="stat-row">
      <el-col :xs="12" :sm="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-primary)"><Tools /></el-icon>
            <div class="stat-body">
              <div class="stat-label">服务总数</div>
              <div class="stat-value">{{ services.length }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-success)"><Promotion /></el-icon>
            <div class="stat-body">
              <div class="stat-label">运行中</div>
              <div class="stat-value">{{ runningCount }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-warning)"><VideoPause /></el-icon>
            <div class="stat-body">
              <div class="stat-label">定时任务</div>
              <div class="stat-value">{{ schedulerCount }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <el-icon class="stat-icon" color="var(--el-color-danger)"><Delete /></el-icon>
            <div class="stat-body">
              <div class="stat-label">手动操作</div>
              <div class="stat-value">{{ manualCount }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="block">
      <template #header>
        <span class="block-title">服务列表</span>
      </template>
      <el-table :data="services" empty-text="没有开启任何后台服务">
        <el-table-column prop="name" label="名称" min-width="160" />
        <el-table-column label="类型" width="110" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.type === 'scheduler' ? 'primary' : 'info'">
              {{ row.type === 'scheduler' ? '定时任务' : '手动操作' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.state ? 'success' : 'danger'"
              effect="plain"
            >
              {{ row.state ? 'ON' : 'OFF' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="interval" label="运行周期" width="160" />
        <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-button
              size="small"
              :type="row.type === 'manual' ? 'danger' : 'primary'"
              :icon="row.type === 'manual' ? Delete : Promotion"
              @click="runService(row)"
            >
              {{ row.type === 'manual' ? '清理' : '运行' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        <div class="tool-header">
          <span class="block-title">测试工具</span>
        </div>
      </template>

      <div class="tool-body" style="margin-bottom: 16px">
        <el-button :icon="Tools" @click="openNameTest">名称识别测试</el-button>
        <el-button :icon="Connection" @click="openNetTest">网络连通性测试</el-button>
      </div>
    </el-card>

    <el-dialog v-model="nameTestVisible" title="名称识别测试" width="640px" destroy-on-close>
      <div class="name-test-body">
        <el-input
          v-model="nameTestInput"
          placeholder="种子名/文件名等"
          clearable
          @keyup.enter="doNameTest"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
      </div>
      <div v-if="nameTestResult" class="name-test-result">
        <template v-if="'title' in nameTestResult">
          <div class="result-row">
            <span class="result-group">
              <el-tag
                type="warning"
                :class="{ 'is-danger': (nameTestResult as NameTestData).name === '无法识别' }"
                class="link-tag"
                @click="searchTmdb((nameTestResult as NameTestData).name)"
              >识别名称：{{ (nameTestResult as NameTestData).name }}</el-tag>
              <el-tag
                v-if="showOrgString(nameTestResult as NameTestData)"
                type="warning"
                class="wrap-tag"
              >识别用名：{{ (nameTestResult as NameTestData).org_string }}</el-tag>
            </span>
          </div>
          <div v-if="hasReplacedWords(nameTestResult as NameTestData)" class="result-row">
            <span class="result-group">
              <span class="chip-label" style="font-size: 13px; color: var(--el-text-color-secondary);">应用替换词：</span>
              <el-tag
                v-for="(w, wi) in toArray(getReplacedWords(nameTestResult as NameTestData))"
                :key="wi"
                type="info"
                size="small"
              >{{ w }}</el-tag>
            </span>
          </div>
          <div class="result-row">
            <span class="result-group">
              <el-tag
                type="success"
                class="link-tag"
                title="点击复制"
                @click="copyText((nameTestResult as NameTestData).title)"
              >
                <span class="link-label">标题</span>：{{ (nameTestResult as NameTestData).title }}
              </el-tag>
              <el-tag
                v-if="(nameTestResult as NameTestData).tmdbid"
                type="success"
                class="link-tag"
                @click="openUrl((nameTestResult as NameTestData).tmdblink)"
              ><span class="link-label">TMDB ID</span>：{{ (nameTestResult as NameTestData).tmdbid }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).year" type="warning">年份：{{ (nameTestResult as NameTestData).year }}</el-tag>
              <el-tag
                v-if="(nameTestResult as NameTestData).season_episode"
                type="warning"
                class="link-tag"
                @click="openUrl((nameTestResult as NameTestData).tmdb_S_E_link)"
              ><span class="link-label">季集</span>：{{ (nameTestResult as NameTestData).season_episode }}</el-tag>
            </span>
          </div>
          <div class="result-row">
            <span class="result-group">
              <el-tag v-if="(nameTestResult as NameTestData).restype">质量：{{ (nameTestResult as NameTestData).restype }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).effect">特性：{{ (nameTestResult as NameTestData).effect }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).category" type="primary">类别：{{ (nameTestResult as NameTestData).category }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).pix">分辨率：{{ (nameTestResult as NameTestData).pix }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).video_codec">视频编码：{{ (nameTestResult as NameTestData).video_codec }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).audio_codec">音频编码：{{ (nameTestResult as NameTestData).audio_codec }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).team" type="info">制作组/字幕组：{{ (nameTestResult as NameTestData).team }}</el-tag>
              <el-tag v-if="(nameTestResult as NameTestData).part" type="warning">分集：{{ (nameTestResult as NameTestData).part }}</el-tag>
            </span>
          </div>
        </template>
        <template v-else>
          <el-tag type="danger">{{ nameTestResult.name }}</el-tag>
        </template>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="nameTestVisible = false">取消</el-button>
          <el-button type="primary" :loading="nameTestLoading" @click="doNameTest">
            {{ nameTestLoading ? '识别中...' : '识别' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="netTestVisible" title="网络连通性测试" width="600px" destroy-on-close>
      <el-table
        v-loading="netTestLoading"
        :data="netTestResults"
        size="small"
      >
        <el-table-column prop="target" label="测试对象" min-width="240" />
        <el-table-column label="连通性" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.testing" size="small" type="info">测试中</el-tag>
            <el-tag v-else size="small" :type="row.res ? 'success' : 'danger'">
              {{ row.res ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="120" align="center">
          <template #default="{ row }">
            <span :style="{ color: row.res ? 'var(--el-color-success)' : 'var(--el-color-danger)' }">
              {{ row.time || '-' }}
            </span>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="netTestVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.service {
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
.block {
  margin-bottom: 16px;
}
.block-title {
  font-weight: 600;
}
.tool-header {
  display: flex;
  align-items: center;
}
.tool-body {
  display: flex;
  gap: 8px;
  align-items: center;
}
.name-result {
  margin-top: 12px;
}
.name-test-body {
  margin-bottom: 12px;
}
.name-test-result {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.result-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.result-group {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.dialog-footer {
  display: flex;
  justify-content: space-between;
  width: 100%;
}
.link-tag {
  cursor: pointer;
}
.link-tag:hover {
  opacity: 0.8;
}
.link-label {
  text-decoration: underline;
  text-underline-offset: 2px;
}
.is-danger.el-tag {
  --el-tag-bg-color: var(--el-color-danger-light-9);
  --el-tag-border-color: var(--el-color-danger-light-5);
  --el-tag-text-color: var(--el-color-danger);
}
.name-test-result .el-tag {
  max-width: 100%;
  white-space: normal;
  word-break: break-all;
  line-height: 1.4;
  height: auto;
  min-height: 24px;
}
</style>
