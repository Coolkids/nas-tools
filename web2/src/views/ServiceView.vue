<script setup lang="ts">
import { onMounted, ref, computed, reactive } from 'vue'
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

const nameTestState = reactive({
  input: '',
  loading: false,
  result: null as NameTestData | { name: string } | null
})

async function doNameTest() {
  const name = nameTestState.input.trim()
  if (!name) {
    modal.warning('请输入资源名称')
    return
  }
  nameTestState.loading = true
  nameTestState.result = null
  try {
    const res = await nameTest(name)
    if (res.code === 0 && res.data) {
      nameTestState.result = res.data
    } else {
      nameTestState.result = { name: '无法识别' }
    }
  } catch {
    modal.error('识别失败')
  } finally {
    nameTestState.loading = false
  }
}

const isFullNameData = (
  d: NameTestData | { name: string } | null
): d is NameTestData => !!d && 'title' in d

const netTestState = reactive({
  input: '',
  loading: false,
  results: [] as { target: string; res?: boolean; time?: string; testing: boolean }[]
})

function startNetTest() {
  let targets: string[]
  const input = netTestState.input.trim()
  if (input) {
    targets = input.split(/[\s,，;；\n]+/).filter(Boolean)
  } else {
    targets = [...NETTEST_TARGETS]
  }
  netTestState.results = targets.map((t) => ({ target: t, testing: true }))
  netTestState.loading = true
  Promise.all(
    netTestState.results.map((r) =>
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
    netTestState.loading = false
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

      <el-collapse>
        <el-collapse-item title="名称识别测试" name="name">
          <div class="tool-body">
            <el-input
              v-model="nameTestState.input"
              placeholder="种子名/文件名等"
              clearable
              @keyup.enter="doNameTest"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
            <el-button
              type="primary"
              :icon="Search"
              :loading="nameTestState.loading"
              @click="doNameTest"
            >
              识别
            </el-button>
          </div>
          <div v-if="nameTestState.result" class="name-result">
            <template v-if="isFullNameData(nameTestState.result)">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="类型">{{ nameTestState.result.type }}</el-descriptions-item>
                <el-descriptions-item label="名称">{{ nameTestState.result.name }}</el-descriptions-item>
                <el-descriptions-item label="标题">{{ nameTestState.result.title }}</el-descriptions-item>
                <el-descriptions-item label="年份">{{ nameTestState.result.year }}</el-descriptions-item>
                <el-descriptions-item label="季集">{{ nameTestState.result.season_episode }}</el-descriptions-item>
                <el-descriptions-item label="分类">{{ nameTestState.result.category }}</el-descriptions-item>
                <el-descriptions-item label="TMDB ID">{{ nameTestState.result.tmdbid }}</el-descriptions-item>
                <el-descriptions-item label="资源类型">{{ nameTestState.result.restype }}</el-descriptions-item>
                <el-descriptions-item label="分辨率">{{ nameTestState.result.pix }}</el-descriptions-item>
                <el-descriptions-item label="制作组">{{ nameTestState.result.team }}</el-descriptions-item>
                <el-descriptions-item label="视频编码">{{ nameTestState.result.video_codec }}</el-descriptions-item>
                <el-descriptions-item label="音频编码">{{ nameTestState.result.audio_codec }}</el-descriptions-item>
                <el-descriptions-item label="TMDB 链接" :span="2">
                  <el-link
                    v-if="nameTestState.result.tmdblink"
                    type="primary"
                    :href="nameTestState.result.tmdblink"
                    target="_blank"
                  >
                    {{ nameTestState.result.tmdblink }}
                  </el-link>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item label="原始串" :span="2">{{ nameTestState.result.org_string }}</el-descriptions-item>
              </el-descriptions>
            </template>
            <template v-else>
              <el-tag type="warning">{{ nameTestState.result.name }}</el-tag>
            </template>
          </div>
        </el-collapse-item>

        <el-collapse-item title="网络连通性测试" name="net">
          <div class="tool-body">
            <el-input
              v-model="netTestState.input"
              placeholder="留空则测试默认目标，多个目标用空格或逗号分隔"
              clearable
            >
              <template #prefix>
                <el-icon><Connection /></el-icon>
              </template>
            </el-input>
            <el-button
              type="primary"
              :icon="Connection"
              :loading="netTestState.loading"
              @click="startNetTest"
            >
              测试
            </el-button>
          </div>
          <el-table
            v-if="netTestState.results.length"
            :data="netTestState.results"
            size="small"
            style="margin-top: 12px"
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
        </el-collapse-item>
      </el-collapse>
    </el-card>
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
</style>
