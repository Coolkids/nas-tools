<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Refresh, Edit, Delete, Lightning, View, Menu as MenuIcon, Download, Check, Close } from '@element-plus/icons-vue'
import { doAction } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import {
  getUserRssTasks,
  getUserRssTaskDetail,
  updateUserRssTask,
  deleteUserRssTask,
  runUserRss,
  listRssArticles,
  rssArticlesDownload,
  rssArticlesCheck,
  rssArticleTest,
  listRssHistory,
  getRssParserList,
  type UserRssTask,
  type UserRssArticle,
  type UserRssHistory,
  type UpdateUserRssTaskParams,
  type RssParser
} from '@/api/rss'

const router = useRouter()
const modal = useModalStore()

const loading = ref(false)
const list = ref<UserRssTask[]>([])
const parsers = ref<RssParser[]>([])
const ruleGroups = ref<Array<{ id: number | string; name: string }>>([])
const downloadSettings = ref<Array<{ id: number | string; name: string }>>([])

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await getUserRssTasks()
    if (res.code === 0) {
      const detail = res.detail
      list.value = Array.isArray(detail) ? detail : detail ? [detail] : []
    } else {
      modal.error('获取订阅任务失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取订阅任务失败')
  } finally {
    loading.value = false
  }
}

const count = computed(() => list.value.length)

const expandedIds = ref<Set<number>>(new Set())
function toggleDetail(id: number) {
  if (expandedIds.value.has(id)) expandedIds.value.delete(id)
  else expandedIds.value.add(id)
}

async function onRun(row: UserRssTask) {
  try {
    const res = await runUserRss(row.id)
    if (res.code === 0) modal.success('任务运行完成')
    else modal.error(res.msg || '运行失败')
    load()
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '运行失败')
  }
}

async function onDelete(row: UserRssTask) {
  const ok = await modal.confirm(`确认删除订阅任务「${row.name}」？`, '删除任务')
  if (!ok) return
  try {
    const res = await deleteUserRssTask(row.id)
    if (res.code === 0) {
      modal.success('删除成功')
      load()
    } else {
      modal.error(res.msg || '删除失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '删除失败')
  }
}

function addressOnly(addr?: string) {
  if (!addr) return ''
  return addr.split('?')[0]
}

const dialogVisible = ref(false)
const dialogTitle = ref('新增订阅')
const submitting = ref(false)

const form = reactive({
  id: '' as string | number,
  name: '',
  state: 'N',
  interval: '' as string | number,
  address: '',
  parser: '' as string | number,
  uses: 'D' as 'D' | 'R',
  include: '',
  exclude: '',
  rule: '' as string | number,
  save_path: '',
  download_setting: '' as string | number,
  recognization: 'Y',
  restype: '',
  pix: '',
  team: '',
  over_edition: '0',
  rss_sites: [] as Array<string | number>,
  search_sites: [] as Array<string | number>
})

const RESTYPE_OPTIONS = ['BLURAY', 'REMUX', 'DOLBY', 'WEB', 'HDTV', 'UHD', 'HDR', '3D']
const PIX_OPTIONS = ['8k', '4k', '1080p', '720p']

function resetForm() {
  form.id = ''
  form.name = ''
  form.state = 'N'
  form.interval = ''
  form.address = ''
  form.parser = parsers.value[0]?.id ?? ''
  form.uses = 'D'
  form.include = ''
  form.exclude = ''
  form.rule = ''
  form.save_path = ''
  form.download_setting = ''
  form.recognization = 'Y'
  form.restype = ''
  form.pix = ''
  form.team = ''
  form.over_edition = '0'
  form.rss_sites = []
  form.search_sites = []
}

function openAdd() {
  resetForm()
  dialogTitle.value = '新增订阅'
  dialogVisible.value = true
}

async function openEdit(row: UserRssTask) {
  try {
    const res = await getUserRssTaskDetail(row.id)
    if (res.code !== 0) {
      modal.error('获取任务详情失败')
      return
    }
    const d = res.detail as UserRssTask
    resetForm()
    form.id = d.id
    form.name = d.name
    form.state = d.state || 'N'
    form.interval = d.interval ?? ''
    form.address = d.address || ''
    form.parser = d.parser ?? ''
    form.uses = (d.uses as 'D' | 'R') || 'D'
    form.include = d.include || ''
    form.exclude = d.exclude || ''
    form.rule = d.filter ?? ''
    form.save_path = d.save_path || ''
    form.download_setting = d.download_setting ?? ''
    form.recognization = d.recognization || 'Y'
    form.over_edition = String(d.over_edition ?? '0')
    const fargs = d.filter_args || {}
    form.restype = fargs.restype || ''
    form.pix = fargs.pix || ''
    form.team = fargs.team || ''
    const sites = d.sites || { rss_sites: [], search_sites: [] }
    form.rss_sites = sites.rss_sites || []
    form.search_sites = sites.search_sites || []
    dialogTitle.value = '编辑订阅'
    dialogVisible.value = true
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取任务详情失败')
  }
}

async function submit() {
  if (!form.name) {
    modal.warning('请输入名称')
    return
  }
  if (!form.interval || isNaN(Number(form.interval))) {
    modal.warning('请输入有效的刷新间隔')
    return
  }
  if (!form.address) {
    modal.warning('请输入RSS地址')
    return
  }
  if (!form.parser) {
    modal.warning('请选择解析器')
    return
  }
  const params: UpdateUserRssTaskParams = {
    id: form.id || undefined,
    name: form.name,
    address: form.address,
    parser: form.parser,
    interval: form.interval,
    uses: form.uses,
    state: form.state,
    include: form.include,
    exclude: form.exclude,
    rule: form.rule,
    save_path: form.save_path,
    download_setting: form.download_setting
  }
  if (form.uses === 'D') {
    params.recognization = form.recognization
  } else {
    params.over_edition = form.over_edition
    params.sites = { rss_sites: form.rss_sites, search_sites: form.search_sites }
    params.restype = form.restype
    params.pix = form.pix
    params.team = form.team
  }
  submitting.value = true
  try {
    const res = await updateUserRssTask(params)
    if (res.code === 0) {
      modal.success(form.id ? '保存成功' : '新增成功')
      dialogVisible.value = false
      load()
    } else {
      modal.error(res.msg || '保存失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    submitting.value = false
  }
}

const articlesDialogVisible = ref(false)
const articlesLoading = ref(false)
const articlesList = ref<UserRssArticle[]>([])
const articlesUses = ref('D')
const articlesTaskId = ref<string | number>('')
const articlesTitle = ref('订阅预览')
const selectedArticles = ref<UserRssArticle[]>([])

function articlesSelection(rows: UserRssArticle[]) {
  selectedArticles.value = rows
}

async function openArticles(row: UserRssTask) {
  articlesTitle.value = `订阅预览 - ${row.name}`
  articlesTaskId.value = row.id
  articlesDialogVisible.value = true
  articlesLoading.value = true
  articlesList.value = []
  selectedArticles.value = []
  try {
    const res = await listRssArticles(row.id)
    if (res.code === 0) {
      articlesList.value = res.data || []
      articlesUses.value = res.uses || 'D'
    } else {
      modal.warning(res.msg || '未获取到报文')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取报文失败')
  } finally {
    articlesLoading.value = false
  }
}

async function batchDownload() {
  if (selectedArticles.value.length === 0) {
    modal.warning('请先选择文章')
    return
  }
  const articles = selectedArticles.value.map((a) => ({ title: a.title, enclosure: a.enclosure || '' }))
  try {
    const res = await rssArticlesDownload(articlesTaskId.value, articles)
    if (res.code === 0) modal.success('添加下载成功')
    else modal.error('添加下载失败')
    openArticles({ id: articlesTaskId.value, name: '' } as UserRssTask)
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '下载失败')
  }
}

async function batchCheck(flag: 'set_finished' | 'set_unfinish') {
  if (selectedArticles.value.length === 0) {
    modal.warning('请先选择文章')
    return
  }
  const articles = selectedArticles.value.map((a) => ({ title: a.title, enclosure: a.enclosure || '' }))
  try {
    const res = await rssArticlesCheck(flag, articles)
    if (res.code === 0) {
      modal.success('处理成功')
      openArticles({ id: articlesTaskId.value, name: '' } as UserRssTask)
    } else {
      modal.error('处理失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '处理失败')
  }
}

async function singleDownload(article: UserRssArticle) {
  try {
    const res = await rssArticlesDownload(articlesTaskId.value, [{ title: article.title, enclosure: article.enclosure || '' }])
    if (res.code === 0) modal.success('添加下载成功')
    else modal.error('添加下载失败')
    openArticles({ id: articlesTaskId.value, name: '' } as UserRssTask)
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '下载失败')
  }
}

async function testArticle(article: UserRssArticle) {
  try {
    const res = await rssArticleTest(articlesTaskId.value, article.title)
    if (res.code === 0 && res.data) {
      const data = res.data
      const name = (data.name as string) || (data.title as string) || '无法识别'
      const parts: string[] = [name]
      if (data.match_flag) parts.push('匹配')
      else parts.push('不匹配')
      if (data.exist_flag) parts.push('本地已存在')
      else parts.push('本地不存在')
      modal.info(parts.join(' / '))
    } else {
      modal.warning('无法识别')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '测试失败')
  }
}

const historyDialogVisible = ref(false)
const historyLoading = ref(false)
const historyList = ref<UserRssHistory[]>([])
const historyTitle = ref('订阅下载历史')

async function openHistory(row: UserRssTask) {
  historyTitle.value = `订阅下载历史 - ${row.name}`
  historyDialogVisible.value = true
  historyLoading.value = true
  historyList.value = []
  try {
    const res = await listRssHistory(row.id)
    if (res.code === 0) historyList.value = res.data || []
    else modal.warning(res.msg || '无下载记录')
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取历史失败')
  } finally {
    historyLoading.value = false
  }
}

function goParser() {
  router.push('/rss_parser')
}

async function loadOptions() {
  try {
    const [parserRes, ruleRes, dsRes] = await Promise.all([
      getRssParserList(),
      doAction<{ code: number; ruleGroups?: Array<{ id: number; name: string }> }>('get_filterrules', {}),
      doAction<{ code: number; data?: Array<{ id: number | string; name: string }> }>('get_download_setting', {})
    ])
    if (parserRes.code === 0) parsers.value = parserRes.detail || []
    if (ruleRes.code === 0) ruleGroups.value = ruleRes.ruleGroups || []
    if (dsRes.code === 0) downloadSettings.value = dsRes.data || []
  } catch {
    /* ignore */
  }
}

onMounted(loadOptions)
</script>

<template>
  <div class="user-rss" v-loading="loading">
    <PageHeader title="自定义订阅" description="管理自定义RSS订阅任务">
      <template #actions>
        <el-button :icon="Plus" type="primary" @click="openAdd">新建订阅任务</el-button>
        <el-button :icon="MenuIcon" @click="goParser">RSS解析器</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-empty v-if="!loading && count === 0" description="当前没有自定义订阅任何内容。" />

    <div v-else class="task-list">
      <el-card v-for="task in list" :key="task.id" shadow="hover" class="task-card">
        <template #header>
          <div class="card-header">
            <div class="header-left" @click="toggleDetail(task.id)">
              <span class="state-dot" :class="task.state === 'Y' ? 'on' : 'off'" />
              <h3 class="task-name">{{ task.name }}</h3>
              <el-button :icon="Lightning" link type="primary" @click.stop="onRun(task)" title="立即运行" />
            </div>
            <div class="header-meta">
              <el-tag v-if="task.uses === 'D' && task.recognization === 'Y'" size="small" type="primary">TMDB</el-tag>
              <el-tag size="small" :type="task.uses === 'D' ? 'primary' : 'success'">{{ task.uses_text || (task.uses === 'D' ? '下载' : '订阅') }}</el-tag>
            </div>
            <div class="header-actions">
              <el-tooltip content="展开/折叠">
                <el-button :icon="MenuIcon" link @click="toggleDetail(task.id)" />
              </el-tooltip>
              <el-tooltip content="预览报文">
                <el-button :icon="View" link @click="openArticles(task)" />
              </el-tooltip>
              <el-tooltip content="编辑">
                <el-button :icon="Edit" link @click="openEdit(task)" />
              </el-tooltip>
              <el-tooltip content="删除">
                <el-button :icon="Delete" link type="danger" @click="onDelete(task)" />
              </el-tooltip>
            </div>
          </div>
        </template>
        <el-collapse-transition>
          <div v-show="expandedIds.has(task.id)" class="detail-grid">
            <div class="detail-item detail-item-wide">
              <div class="detail-title">地址</div>
              <div class="detail-content small break">{{ addressOnly(task.address) }}</div>
            </div>
            <div class="detail-item">
              <div class="detail-title">解析器</div>
              <div class="detail-content">{{ task.parser_name || task.parser }}</div>
            </div>
            <div class="detail-item">
              <div class="detail-title">刷新间隔</div>
              <div class="detail-content">{{ task.interval }} 分钟</div>
            </div>
            <div class="detail-item">
              <div class="detail-title">动作</div>
              <div class="detail-content">
                <el-tag size="small" :type="task.uses === 'D' ? 'primary' : 'success'">{{ task.uses_text || task.uses }}</el-tag>
              </div>
            </div>
            <template v-if="task.uses === 'D'">
              <div class="detail-item">
                <div class="detail-title">包含</div>
                <div class="detail-content small">{{ task.include || '—' }}</div>
              </div>
              <div class="detail-item">
                <div class="detail-title">排除</div>
                <div class="detail-content small">{{ task.exclude || '—' }}</div>
              </div>
              <div class="detail-item">
                <div class="detail-title">过滤规则</div>
                <div class="detail-content">{{ task.filter_name || '—' }}</div>
              </div>
              <div class="detail-item">
                <div class="detail-title">保存路径</div>
                <div class="detail-content small">{{ task.save_path || '自动' }}</div>
              </div>
            </template>
            <div class="detail-item">
              <div class="detail-title">状态</div>
              <div class="detail-content">
                <el-tag size="small" :type="task.state === 'Y' ? 'success' : 'danger'">
                  {{ task.state === 'Y' ? '正在运行' : '已停用' }}
                </el-tag>
              </div>
            </div>
            <div class="detail-item">
              <div class="detail-title">已处理数量</div>
              <div class="detail-content">
                <el-button v-if="task.uses === 'D'" link type="primary" @click="openHistory(task)">{{ task.counter || 0 }}</el-button>
                <span v-else>{{ task.counter || 0 }}</span>
              </div>
            </div>
            <div class="detail-item">
              <div class="detail-title">最后更新时间</div>
              <div class="detail-content small">{{ task.update_time || '—' }}</div>
            </div>
            <div v-if="task.uses === 'D'" class="detail-item">
              <div class="detail-title">下载设置</div>
              <div class="detail-content">
                <el-tag v-if="task.download_setting" size="small" type="warning">
                  {{ downloadSettings.find((d) => String(d.id) === String(task.download_setting))?.name || '默认' }}
                </el-tag>
                <el-tag v-else size="small" type="danger">默认</el-tag>
              </div>
            </div>
          </div>
        </el-collapse-transition>
      </el-card>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogTitle"
      width="820px"
      :close-on-click-modal="false"
      destroy-on-close
      top="5vh"
    >
      <el-form :model="form" label-width="120px" label-position="right">
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="别名" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="状态" required>
              <el-select v-model="form.state" style="width: 100%">
                <el-option label="正常" value="Y" />
                <el-option label="停用" value="N" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="刷新间隔(分钟)" required>
              <el-input v-model="form.interval" placeholder="30" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="16">
            <el-form-item label="地址" required>
              <el-input v-model="form.address" placeholder="RSS地址" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="解析器" required>
              <el-select v-model="form.parser" style="width: 100%">
                <el-option v-for="p in parsers" :key="p.id" :label="p.name" :value="p.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="动作" required>
          <el-radio-group v-model="form.uses">
            <el-radio-button value="D">下载</el-radio-button>
            <el-radio-button value="R">订阅</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <div v-if="form.uses === 'D'">
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="包含">
                <el-input v-model="form.include" placeholder="关键字/正则" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="排除">
                <el-input v-model="form.exclude" placeholder="关键字/正则" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="过滤规则">
                <el-select v-model="form.rule" style="width: 100%" clearable>
                  <el-option v-for="r in ruleGroups" :key="r.id" :label="r.name" :value="r.id" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="保存路径">
                <el-input v-model="form.save_path" placeholder="留空自动选择" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="下载设置">
                <el-select v-model="form.download_setting" style="width: 100%" clearable>
                  <el-option label="默认" value="" />
                  <el-option v-for="d in downloadSettings" :key="d.id" :label="d.name" :value="d.id" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="识别">
                <el-select v-model="form.recognization" style="width: 100%">
                  <el-option label="是" value="Y" />
                  <el-option label="否" value="N" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
        </div>

        <div v-else>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="包含">
                <el-input v-model="form.include" placeholder="关键字/正则" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="排除">
                <el-input v-model="form.exclude" placeholder="关键字/正则" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-divider content-position="left">订阅设置</el-divider>
          <el-row :gutter="16">
            <el-col :span="6">
              <el-form-item label="质量">
                <el-select v-model="form.restype" style="width: 100%" clearable>
                  <el-option v-for="r in RESTYPE_OPTIONS" :key="r" :label="r" :value="r" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="分辨率">
                <el-select v-model="form.pix" style="width: 100%" clearable>
                  <el-option v-for="p in PIX_OPTIONS" :key="p" :label="p" :value="p" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="制作组">
                <el-input v-model="form.team" placeholder="支持正则" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="过滤规则">
                <el-select v-model="form.rule" style="width: 100%" clearable>
                  <el-option label="站点规则" value="" />
                  <el-option v-for="r in ruleGroups" :key="r.id" :label="r.name" :value="r.id" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="6">
              <el-form-item label="下载设置">
                <el-select v-model="form.download_setting" style="width: 100%" clearable>
                  <el-option label="站点设置" value="" />
                  <el-option v-for="d in downloadSettings" :key="d.id" :label="d.name" :value="d.id" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="保存路径">
                <el-input v-model="form.save_path" placeholder="留空使用默认" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="洗版">
                <el-select v-model="form.over_edition" style="width: 100%">
                  <el-option label="否" value="0" />
                  <el-option label="是" value="1" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="articlesDialogVisible" :title="articlesTitle" width="900px" destroy-on-close>
      <el-table
        :data="articlesList"
        v-loading="articlesLoading"
        stripe
        max-height="440"
        @selection-change="articlesSelection"
      >
        <el-table-column type="selection" width="42" />
        <el-table-column label="标题" min-width="280">
          <template #default="{ row }">
            <a v-if="row.link" :href="row.link" target="_blank" class="article-title">{{ row.title }}</a>
            <span v-else class="article-title">{{ row.title }}</span>
            <el-tag v-if="row.size" size="small" type="success" style="margin-left: 6px">{{ row.size }}</el-tag>
            <el-button link size="small" @click="testArticle(row)" style="margin-left: 6px">识别</el-button>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.finish_flag ? 'primary' : 'warning'">
              {{ row.finish_flag ? '已处理' : '未处理' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="发布时间" prop="date" width="160" />
        <el-table-column label="操作" width="90" v-if="articlesUses === 'D'">
          <template #default="{ row }">
            <el-button :icon="Download" link @click="singleDownload(row)" />
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <div class="articles-footer">
          <el-button v-if="articlesUses === 'D'" type="primary" :icon="Download" @click="batchDownload">批量下载</el-button>
          <el-button type="success" :icon="Check" @click="batchCheck('set_finished')">已处理</el-button>
          <el-button type="warning" :icon="Close" @click="batchCheck('set_unfinish')">未处理</el-button>
          <el-button @click="articlesDialogVisible = false">关闭</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="historyDialogVisible" :title="historyTitle" width="800px" destroy-on-close>
      <el-table :data="historyList" v-loading="historyLoading" stripe max-height="440">
        <el-table-column label="标题" prop="title" min-width="280" show-overflow-tooltip />
        <el-table-column label="下载器" prop="downloader" width="160">
          <template #default="{ row }">
            <el-tag v-if="row.downloader" size="small">{{ row.downloader }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="下载时间" prop="date" width="180" />
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.user-rss {
  padding: 16px;
}
.task-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.task-card :deep(.el-card__header) {
  padding: 12px 16px;
}
.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.state-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}
.state-dot.on {
  background: var(--el-color-success);
}
.state-dot.off {
  background: var(--el-color-danger);
}
.task-name {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.header-meta {
  margin-left: auto;
  display: flex;
  gap: 6px;
}
.header-actions {
  display: flex;
  gap: 4px;
}
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px 16px;
}
.detail-item {
  min-width: 0;
}
.detail-item-wide {
  grid-column: 1 / -1;
}
.detail-title {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}
.detail-content {
  font-size: 13px;
  word-break: break-all;
}
.detail-content.small {
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.detail-content.break {
  word-break: break-all;
}
.article-title {
  color: var(--el-color-primary);
  text-decoration: none;
  word-break: break-all;
}
.article-title:hover {
  text-decoration: underline;
}
.articles-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
