<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useConfigForm } from '@/composables/useConfigForm'
import { useModalStore } from '@/stores/modal'
import { doAction } from '@/api'
import { getSystemConfig } from '@/api/config'

const { config, loading, load, save } = useConfigForm()
const modal = useModalStore()

const RMT_MODES = [
  { value: 'copy', label: '复制' },
  { value: 'link', label: '硬链接' },
  { value: 'softlink', label: '软链接' },
  { value: 'move', label: '移动' },
  { value: 'rclonecopy', label: 'Rclone复制' },
  { value: 'rclone', label: 'Rclone移动' },
  { value: 'miniocopy', label: 'Minio复制' },
  { value: 'minio', label: 'Minio移动' }
]

const activeTab = ref('system')

const form = reactive<Record<string, unknown>>({})

const SYSTEM_KEYS = [
  'app.logtype', 'app.logpath', 'app.logserver', 'app.loglevel', 'app.wallpaper',
  'app.web_port', 'app.login_user', 'app.login_password', 'app.ssl_cert', 'app.ssl_key',
  'app.proxies', 'app.domain', 'app.user_agent'
]
const MEDIA_KEYS = [
  'app.rmt_tmdbkey', 'app.tmdb_domain', 'app.rmt_match_mode', 'media.category',
  'pt.rmt_mode', 'media.min_filesize', 'media.ignored_paths', 'media.ignored_files',
  'pt.download_order', 'media.movie_name_format', 'media.tv_name_format',
  'media.filesize_cover', 'media.refresh_mediaserver', 'media.nfo_poster'
]
const SERVICE_KEYS = [
  'pt.ptsignin_cron', 'pt.pt_check_interval', 'pt.search_rss_interval',
  'media.mediasync_interval', 'pt.pt_monitor', 'pt.pt_monitor_only',
  'pt.search_auto', 'pt.search_no_result_rss'
]
const SECURITY_KEYS = [
  'security.media_server_webhook_allow_ip.ipv4',
  'security.media_server_webhook_allow_ip.ipv6',
  'security.telegram_webhook_allow_ip.ipv4',
  'security.telegram_webhook_allow_ip.ipv6',
  'security.synology_webhook_allow_ip.ipv4',
  'security.synology_webhook_allow_ip.ipv6',
  'security.api_key'
]
const LAB_KEYS = [
  'laboratory.search_keyword', 'laboratory.search_tmdbweb', 'laboratory.tmdb_cache_expire',
  'laboratory.use_douban_titles', 'laboratory.search_en_title', 'laboratory.tmdb_proxy'
]

const SCRAPER_NFO = [
  { group: '电影', items: [
    { key: 'scraper_nfo.movie.basic', label: '基础信息' },
    { key: 'scraper_nfo.movie.credits', label: '演职人员' },
    { key: 'scraper_nfo.movie.credits_chinese', label: '演职人员中文' }
  ] },
  { group: '电视剧', items: [
    { key: 'scraper_nfo.tv.basic', label: '基础信息' },
    { key: 'scraper_nfo.tv.credits', label: '演职人员' },
    { key: 'scraper_nfo.tv.credits_chinese', label: '演职人员中文' },
    { key: 'scraper_nfo.tv.season_basic', label: '季-基础信息' },
    { key: 'scraper_nfo.tv.episode_basic', label: '集-基础信息' },
    { key: 'scraper_nfo.tv.episode_credits', label: '集-演职人员' }
  ] }
]
const SCRAPER_PIC = [
  { group: '电影图片', items: [
    { key: 'scraper_pic.movie.poster', label: 'poster' },
    { key: 'scraper_pic.movie.backdrop', label: 'fanart' },
    { key: 'scraper_pic.movie.background', label: 'background' },
    { key: 'scraper_pic.movie.logo', label: 'logo' },
    { key: 'scraper_pic.movie.disc', label: 'disc' },
    { key: 'scraper_pic.movie.banner', label: 'banner' },
    { key: 'scraper_pic.movie.thumb', label: 'thumb' }
  ] },
  { group: '电视剧图片', items: [
    { key: 'scraper_pic.tv.poster', label: 'poster' },
    { key: 'scraper_pic.tv.backdrop', label: 'fanart' },
    { key: 'scraper_pic.tv.background', label: 'show' },
    { key: 'scraper_pic.tv.logo', label: 'logo' },
    { key: 'scraper_pic.tv.clearart', label: 'clearart' },
    { key: 'scraper_pic.tv.banner', label: 'banner' },
    { key: 'scraper_pic.tv.thumb', label: 'thumb' }
  ] },
  { group: '电视剧-季图片', items: [
    { key: 'scraper_pic.tv.season_poster', label: 'poster' },
    { key: 'scraper_pic.tv.season_banner', label: 'banner' },
    { key: 'scraper_pic.tv.season_thumb', label: 'thumb' }
  ] },
  { group: '电视剧-集图片', items: [
    { key: 'scraper_pic.tv.episode_thumb', label: 'thumb' }
  ] }
]
const SCRAPER_KEYS = [...SCRAPER_NFO, ...SCRAPER_PIC].flatMap((s) => s.items.map((i) => i.key))

const scraperVisible = ref(false)
const scraperTab = ref('nfo')
const scriptVisible = ref(false)
const scriptTab = ref('css')
const scriptForm = reactive({ css: '', javascript: '' })
const scriptLoading = ref(false)
const scriptSaving = ref(false)
const releaseGroupsVisible = ref(false)
const releaseGroups = ref('')

function getCfg(path: string): unknown {
  const parts = path.split('.')
  let cur: unknown = config.value
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[p]
  }
  return cur
}

function syncForm() {
  const str = (path: string, def = ''): string => {
    const v = getCfg(path)
    return v === undefined || v === null ? def : String(v)
  }
  const sw = (path: string): boolean => !!getCfg(path)

  SYSTEM_KEYS.forEach((k) => {
    if (k === 'app.proxies') {
      const proxies = getCfg('app.proxies') as { http?: string } | undefined
      let p = proxies?.http || ''
      if (p.startsWith('http://')) p = p.replace('http://', '')
      form[k] = p
    } else {
      form[k] = str(k, k === 'app.logtype' ? 'console' : k === 'app.wallpaper' ? 'themoviedb' : k === 'app.tmdb_domain' ? '' : '')
    }
  })
  form['app.logtype'] = str('app.logtype', 'console')
  form['app.wallpaper'] = str('app.wallpaper', 'themoviedb')
  form['app.loglevel'] = str('app.loglevel', 'info')
  form['app.tmdb_domain'] = str('app.tmdb_domain', 'api.themoviedb.org')

  MEDIA_KEYS.forEach((k) => {
    if (typeof form[k] === 'undefined') form[k] = str(k)
  })
  form['pt.rmt_mode'] = str('pt.rmt_mode', 'copy')
  form['app.rmt_match_mode'] = str('app.rmt_match_mode', 'normal')
  form['media.filesize_cover'] = sw('media.filesize_cover')
  form['media.refresh_mediaserver'] = sw('media.refresh_mediaserver')
  form['media.nfo_poster'] = sw('media.nfo_poster')

  SERVICE_KEYS.forEach((k) => (form[k] = str(k)))
  form['pt.pt_monitor'] = sw('pt.pt_monitor')
  form['pt.pt_monitor_only'] = sw('pt.pt_monitor_only')
  form['pt.search_auto'] = sw('pt.search_auto')
  form['pt.search_no_result_rss'] = sw('pt.search_no_result_rss')

  SECURITY_KEYS.forEach((k) => (form[k] = str(k)))
  form['pt.download_order'] = str('pt.download_order', '')

  LAB_KEYS.forEach((k) => {
    if (k.startsWith('laboratory.')) form[k] = sw(k)
  })

  SCRAPER_KEYS.forEach((k) => (form[k] = sw(k)))
  releaseGroups.value = str('laboratory.release_groups')
}

async function loadData() {
  await load()
  syncForm()
}

async function saveSection(keys: string[]) {
  const items: Record<string, unknown> = {}
  keys.forEach((k) => {
    items[k] = form[k]
  })
  await save(items)
}

function openScraper() {
  syncForm()
  scraperVisible.value = true
}

async function saveScraper() {
  const ok = await save(SCRAPER_KEYS.reduce((acc, k) => {
    acc[k] = form[k]
    return acc
  }, {} as Record<string, unknown>))
  if (ok) scraperVisible.value = false
}

async function openScript() {
  scriptLoading.value = true
  scriptVisible.value = true
  try {
    const res = await getSystemConfig('CustomScript')
    if (res.code === 0 && res.value) {
      scriptForm.css = (res.value.css as string) || ''
      scriptForm.javascript = (res.value.javascript as string) || ''
    }
  } catch {
    modal.error('加载自定义脚本失败')
  } finally {
    scriptLoading.value = false
  }
}

async function saveScript() {
  scriptSaving.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('save_user_script', {
      css: scriptForm.css,
      javascript: scriptForm.javascript
    })
    if (res.code === 0) {
      modal.success('保存成功')
      scriptVisible.value = false
    } else {
      modal.error(res.msg || '保存失败')
    }
  } finally {
    scriptSaving.value = false
  }
}

function openReleaseGroups() {
  releaseGroups.value = String(form['laboratory.release_groups'] || getCfg('laboratory.release_groups') || '')
  releaseGroupsVisible.value = true
}

async function saveReleaseGroups() {
  let val = releaseGroups.value.replace(/;$/g, '')
  form['laboratory.release_groups'] = val
  const ok = await save({ 'laboratory.release_groups': val })
  if (ok) releaseGroupsVisible.value = false
}

onMounted(loadData)
</script>

<template>
  <div class="basic-view" v-loading="loading">
    <PageHeader title="基础设置" description="系统、媒体、服务、安全与实验室配置" />

    <el-tabs v-model="activeTab" class="basic-tabs">
      <el-tab-pane label="系统" name="system">
        <el-card shadow="never">
          <el-form label-width="160px">
            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="日志输出类型" required>
                  <el-select v-model="form['app.logtype']">
                    <el-option value="console" label="控制台" />
                    <el-option value="file" label="文件" />
                    <el-option value="server" label="日志中心" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="日志文件路径">
                  <el-input v-model="form['app.logpath']" placeholder="/config/logs" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="日志中心地址">
                  <el-input v-model="form['app.logserver']" placeholder="127.0.0.1:514" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="日志级别">
                  <el-select v-model="form['app.loglevel']">
                    <el-option value="info" label="INFO" />
                    <el-option value="debug" label="DEBUG" />
                    <el-option value="error" label="ERROR" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="WEB壁纸来源" required>
                  <el-select v-model="form['app.wallpaper']">
                    <el-option value="themoviedb" label="电影海报" />
                    <el-option value="bing" label="Bing每日壁纸" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="WEB服务端口" required>
                  <el-input v-model="form['app.web_port']" placeholder="3000" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="WEB管理用户" required>
                  <el-input v-model="form['app.login_user']" placeholder="admin" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="WEB管理密码" required>
                  <el-input v-model="form['app.login_password']" type="password" show-password placeholder="password" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="HTTPS证书文件路径">
                  <el-input v-model="form['app.ssl_cert']" placeholder="pem格式证书" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="HTTPS密钥文件路径">
                  <el-input v-model="form['app.ssl_key']" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="代理服务器">
                  <el-input v-model="form['app.proxies']" placeholder="127.0.0.1:7890" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="外网访问地址">
                  <el-input v-model="form['app.domain']" placeholder="http://IP:PORT" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="User-Agent">
                  <el-input v-model="form['app.user_agent']" placeholder="Mozilla/5.0 ..." />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <div class="card-footer">
            <el-button @click="openScript">自定义 CSS/JavaScript</el-button>
            <el-button type="primary" :loading="loading" @click="saveSection(SYSTEM_KEYS)">保存</el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="媒体" name="media">
        <el-card shadow="never">
          <el-form label-width="170px">
            <el-row :gutter="16">
              <el-col :span="8">
                <el-form-item label="TMDB API Key" required>
                  <el-input v-model="form['app.rmt_tmdbkey']" placeholder="支持多个key用;分隔" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="TMDB域名" required>
                  <el-select v-model="form['app.tmdb_domain']">
                    <el-option value="api.themoviedb.org" label="api.themoviedb.org" />
                    <el-option value="api.tmdb.org" label="api.tmdb.org" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="TMDB匹配模式" required>
                  <el-select v-model="form['app.rmt_match_mode']">
                    <el-option value="normal" label="正常模式" />
                    <el-option value="strict" label="严格模式" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="8">
                <el-form-item label="二级分类策略">
                  <el-input v-model="form['media.category']" placeholder="default-category" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="默认文件转移方式" required>
                  <el-select v-model="form['pt.rmt_mode']">
                    <el-option v-for="m in RMT_MODES" :key="m.value" :value="m.value" :label="m.label" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="转移最小文件大小(MB)" required>
                  <el-input v-model="form['media.min_filesize']" placeholder="200" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="8">
                <el-form-item label="文件路径转移忽略词">
                  <el-input v-model="form['media.ignored_paths']" placeholder="正则表达式，;分隔" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="文件名转移忽略词">
                  <el-input v-model="form['media.ignored_files']" placeholder="正则表达式，;分隔" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="下载优先规则" required>
                  <el-select v-model="form['pt.download_order']">
                    <el-option value="" label="默认" />
                    <el-option value="site" label="站点优先" />
                    <el-option value="seeder" label="做种数优先" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="电影重命名格式" required>
                  <el-input v-model="form['media.movie_name_format']" placeholder="{title} ({year})/{title}-{part} ({year}) - {videoFormat}" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="电视剧重命名格式" required>
                  <el-input v-model="form['media.tv_name_format']" placeholder="{title} ({year})/Season {season}/{title}-{part} - {season_episode}" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="8">
                <el-form-item label="高质量文件覆盖">
                  <el-switch v-model="form['media.filesize_cover']" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="实时刷新媒体库">
                  <el-switch v-model="form['media.refresh_mediaserver']" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="刮削元数据及图片">
                  <el-switch v-model="form['media.nfo_poster']" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <div class="card-footer">
            <el-button @click="openScraper">刮削设置</el-button>
            <el-button @click="openReleaseGroups">自定义制作组/字幕组</el-button>
            <el-button type="primary" :loading="loading" @click="saveSection(MEDIA_KEYS)">保存</el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="服务" name="service">
        <el-card shadow="never">
          <el-form label-width="170px">
            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="站点签到时间">
                  <el-input v-model="form['pt.ptsignin_cron']" placeholder="留空关闭自动签到" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="订阅RSS周期(秒)">
                  <el-input v-model="form['pt.pt_check_interval']" placeholder="留空关闭RSS订阅" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="订阅搜索周期(小时)">
                  <el-input v-model="form['pt.search_rss_interval']" placeholder="留空关闭订阅定时搜索" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="媒体库同步周期(小时)">
                  <el-input v-model="form['media.mediasync_interval']" placeholder="留空关闭媒体库同步" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="下载软件监控">
                  <el-switch v-model="form['pt.pt_monitor']" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="只管理NAStool下载">
                  <el-switch v-model="form['pt.pt_monitor_only']" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="远程搜索自动择优下载">
                  <el-switch v-model="form['pt.search_auto']" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="远程下载不完整自动订阅">
                  <el-switch v-model="form['pt.search_no_result_rss']" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <div class="card-footer">
            <el-button type="primary" :loading="loading" @click="saveSection(SERVICE_KEYS)">保存</el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="安全" name="security">
        <el-card shadow="never">
          <el-form label-width="200px">
            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="媒体服务器Webhook源IPv4">
                  <el-input v-model="form['security.media_server_webhook_allow_ip.ipv4']" placeholder="允许的IPv4 CIDR" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="媒体服务器Webhook源IPv6">
                  <el-input v-model="form['security.media_server_webhook_allow_ip.ipv6']" placeholder="允许的IPv6 CIDR" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="Telegram源IPv4">
                  <el-input v-model="form['security.telegram_webhook_allow_ip.ipv4']" placeholder="允许的IPv4 CIDR" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="Telegram源IPv6">
                  <el-input v-model="form['security.telegram_webhook_allow_ip.ipv6']" placeholder="允许的IPv6 CIDR" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="Synology Chat源IPv4">
                  <el-input v-model="form['security.synology_webhook_allow_ip.ipv4']" placeholder="允许的IPv4 CIDR" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="Synology Chat源IPv6">
                  <el-input v-model="form['security.synology_webhook_allow_ip.ipv6']" placeholder="允许的IPv6 CIDR" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="API密钥">
                  <el-input v-model="form['security.api_key']" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <div class="card-footer">
            <el-button type="primary" :loading="loading" @click="saveSection(SECURITY_KEYS)">保存</el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="实验室" name="laboratory">
        <el-card shadow="never">
          <el-form label-width="220px">
            <el-row :gutter="16">
              <el-col :span="8">
                <el-form-item label="辅助识别">
                  <el-switch v-model="form['laboratory.search_keyword']" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="增强识别">
                  <el-switch v-model="form['laboratory.search_tmdbweb']" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="TMDB缓存过期策略">
                  <el-switch v-model="form['laboratory.tmdb_cache_expire']" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="8">
                <el-form-item label="使用豆瓣名称联想">
                  <el-switch v-model="form['laboratory.use_douban_titles']" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="搜索优先使用英文名">
                  <el-switch v-model="form['laboratory.search_en_title']" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="使用TMDB代理服务">
                  <el-switch v-model="form['laboratory.tmdb_proxy']" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <div class="card-footer">
            <el-button type="primary" :loading="loading" @click="saveSection(LAB_KEYS)">保存</el-button>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="scraperVisible" title="刮削设置" width="720px" :close-on-click-modal="false">
      <el-tabs v-model="scraperTab">
        <el-tab-pane label="元数据" name="nfo">
          <div v-for="sec in SCRAPER_NFO" :key="sec.group" class="scraper-section">
            <div class="scraper-title">{{ sec.group }}</div>
            <el-checkbox v-for="i in sec.items" :key="i.key" v-model="form[i.key]">{{ i.label }}</el-checkbox>
          </div>
        </el-tab-pane>
        <el-tab-pane label="图片" name="pic">
          <div v-for="sec in SCRAPER_PIC" :key="sec.group" class="scraper-section">
            <div class="scraper-title">{{ sec.group }}</div>
            <el-checkbox v-for="i in sec.items" :key="i.key" v-model="form[i.key]">{{ i.label }}</el-checkbox>
          </div>
        </el-tab-pane>
      </el-tabs>
      <template #footer>
        <el-button @click="scraperVisible = false">取消</el-button>
        <el-button type="primary" @click="saveScraper">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="scriptVisible" title="自定义 CSS/JavaScript" width="800px" :close-on-click-modal="false">
      <el-tabs v-model="scriptTab" v-loading="scriptLoading">
        <el-tab-pane label="CSS" name="css">
          <el-input v-model="scriptForm.css" type="textarea" :rows="18" placeholder="/* 自定义CSS */" />
        </el-tab-pane>
        <el-tab-pane label="JavaScript" name="js">
          <el-input v-model="scriptForm.javascript" type="textarea" :rows="18" placeholder="// 自定义JavaScript" />
        </el-tab-pane>
      </el-tabs>
      <template #footer>
        <el-button @click="scriptVisible = false">取消</el-button>
        <el-button type="primary" :loading="scriptSaving" @click="saveScript">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="releaseGroupsVisible" title="自定义制作组/字幕组" width="640px" :close-on-click-modal="false">
      <el-input v-model="releaseGroups" type="textarea" :rows="6" placeholder="多个制作组/字幕组用;分隔，支持正则表达式" />
      <template #footer>
        <el-button @click="releaseGroupsVisible = false">取消</el-button>
        <el-button type="primary" @click="saveReleaseGroups">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.basic-view {
  padding: 16px;
}
.basic-tabs :deep(.el-tabs__content) {
  padding-top: 8px;
}
.card-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 12px;
  margin-top: 8px;
}
.scraper-section {
  margin-bottom: 16px;
}
.scraper-title {
  font-weight: 600;
  margin-bottom: 8px;
  border-left: 3px solid var(--el-color-primary);
  padding-left: 8px;
}
.scraper-section .el-checkbox {
  margin-right: 16px;
  min-width: 120px;
}
</style>
