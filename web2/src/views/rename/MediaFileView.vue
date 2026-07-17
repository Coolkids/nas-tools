<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Refresh,
  Folder,
  Document,
  Edit,
  Delete,
  Download,
  Back,
  Files,
  Link,
  Tools,
  Upload,
  Search,
  QuestionFilled
} from '@element-plus/icons-vue'
import { doAction } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import { nameTest, type NameTestData } from '@/api/system'

interface FileItem {
  path: string
  name: string
  ext: string
  size: string
  type?: string
}

interface SubPathResult {
  code: number
  msg?: string
  count?: number
  data?: FileItem[]
}

interface TreeData {
  id: string
  label: string
  isLeaf: boolean
  path: string
}

const modal = useModalStore()
const loading = ref(false)
const currentDir = ref('/')
const pathInput = ref('/')
const files = ref<FileItem[]>([])

const treeRef = ref()
const treeKey = ref(0)

onMounted(() => {
  load(currentDir.value)
})

async function loadTreeChildren(parentPath: string) {
  try {
    const res = await doAction<SubPathResult>('get_sub_path', { dir: parentPath, filter: 'ONLYDIR' })
    if (res.code === 0 && res.data) {
      return res.data.map((d) => ({
        id: d.path,
        label: d.name,
        path: d.path,
        isLeaf: false,
      }))
    }
  } catch {
    // ignore
  }
  return []
}

async function treeLazyLoad(node: any, resolve: (data: TreeData[]) => void) {
  if (node.level === 0) {
    const roots = await loadTreeChildren(currentDir.value)
    resolve(roots)
  } else {
    const children = await loadTreeChildren(node.data.path)
    if (children.length === 0) {
      resolve([])
    } else {
      resolve(children)
    }
  }
}

function handleTreeClick(data: TreeData) {
  load(data.path)
}

async function load(dir?: string) {
  const target = dir || currentDir.value
  loading.value = true
  try {
    const res = await doAction<SubPathResult>('get_sub_path', { dir: target, filter: 'ONLYDIR|MEDIAFILE|SUBFILE' })
    if (res.code === 0) {
      files.value = res.data || []
      currentDir.value = target
      pathInput.value = target
      treeKey.value++
    } else {
      modal.error(res.msg || '获取文件列表失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取文件列表失败')
  } finally {
    loading.value = false
  }
}

const breadcrumbs = computed(() => {
  const parts = currentDir.value.split('/').filter(Boolean)
  const crumbs = [{ name: '根目录', path: '/' }]
  let acc = ''
  for (const p of parts) {
    acc += '/' + p
    crumbs.push({ name: p, path: acc })
  }
  return crumbs
})

function goPath(path: string) {
  load(path)
}

function parentDir() {
  const d = currentDir.value
  const idx = d.lastIndexOf('/')
  const parent = idx <= 0 ? '/' : d.slice(0, idx)
  load(parent)
}

function goInput() {
  load(pathInput.value)
}

function isDir(f: FileItem): boolean {
  return !f.ext || f.type === 'dir'
}

const fileItems = computed(() => files.value.filter((f) => !isDir(f)))

function clickItem(f: FileItem) {
  if (isDir(f)) {
    load(f.path)
  }
}

// ---- 重命名 ----

const renameVisible = ref(false)
const renameTarget = ref('')
const renameNewName = ref('')

function openRename(f: FileItem) {
  renameTarget.value = f.path
  renameNewName.value = f.name
  renameVisible.value = true
}

async function doRename() {
  if (!renameNewName.value) return modal.warning('请填写新文件名')
  renameVisible.value = false
  const res = await doAction<{ code: number; msg?: string }>('rename_file', {
    path: renameTarget.value,
    name: renameNewName.value
  })
  if (res.code === 0) {
    modal.success('重命名成功')
    load()
  } else {
    modal.error(res.msg || '重命名失败')
  }
}

// ---- 删除 ----

async function deleteFile(f: FileItem) {
  const ok = await modal.confirm(`是否确认删除文件 ${f.name} ？注意：没有其它媒体文件的目录也将被删除。`, '删除文件')
  if (!ok) return
  const res = await doAction<{ code: number; msg?: string }>('delete_files', { files: [f.path] })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  } else {
    modal.error(res.msg || '删除失败')
  }
}

// ---- 下载字幕 ----

async function downloadSubtitle(f: FileItem) {
  modal.showLoading('下载字幕中...')
  try {
    const res = await doAction<{ code: number; msg?: string }>('download_subtitle', { path: f.path, name: f.name })
    if (res.code === 0) {
      modal.success(res.msg || '下载字幕成功')
      load()
    } else {
      modal.error(res.msg || '下载字幕失败')
    }
  } finally {
    modal.hideLoading()
  }
}

// ---- 识别 ----

const nameTestLoading = ref<Record<string, boolean>>({})
const nameTestResults = reactive<Record<string, NameTestData | { name: string }>>({})

const chipOrder = ['name', 'year', 'season_episode', 'title', 'tmdbid', 'restype', 'pix', 'video_codec', 'audio_codec', 'team']

async function doNameTest(f: FileItem) {
  const key = f.path
  nameTestLoading.value[key] = true
  try {
    const res = await nameTest(f.name)
    if (res.code === 0 && res.data) {
      nameTestResults[key] = res.data
    } else {
      nameTestResults[key] = { name: '无法识别' }
    }
  } catch {
    nameTestResults[key] = { name: '识别失败' }
  } finally {
    nameTestLoading.value[key] = false
  }
}

// ---- 转移 ----

const transferVisible = ref(false)
const transferPath = ref('')
const transferOutPath = ref('')
const transferSyncmod = ref('copy')
const transferType = ref('MOV')
const transferTmdb = ref('')
const transferSeason = ref('')
const transferMinFilesize = ref('')
const transferEpFormat = ref('')
const transferEpDetails = ref('')
const transferEpOffset = ref('')
const transferLoading = ref(false)

function openTransfer(f: FileItem) {
  transferPath.value = f.path
  transferOutPath.value = ''
  transferSyncmod.value = 'copy'
  transferType.value = 'MOV'
  transferTmdb.value = ''
  transferSeason.value = ''
  transferMinFilesize.value = ''
  transferEpFormat.value = ''
  transferEpDetails.value = ''
  transferEpOffset.value = ''
  transferVisible.value = true
}

function openTransferAll() {
  transferPath.value = currentDir.value
  transferOutPath.value = ''
  transferSyncmod.value = 'copy'
  transferType.value = 'MOV'
  transferTmdb.value = ''
  transferSeason.value = ''
  transferMinFilesize.value = ''
  transferEpFormat.value = ''
  transferEpDetails.value = ''
  transferEpOffset.value = ''
  transferVisible.value = true
}

async function doTransfer() {
  if (!transferPath.value) return modal.warning('输入路径不能为空')
  if (transferMinFilesize.value && isNaN(Number(transferMinFilesize.value))) return modal.warning('最小文件大小必须为数字')
  if (transferEpDetails.value && !/^\d{1,5}([,-]\d{1,5})?$/.test(transferEpDetails.value)) return modal.warning('起始集/终止集格式错误')
  if (transferEpOffset.value && !/^-?\d{1,5}$/.test(transferEpOffset.value)) return modal.warning('集数偏移格式错误')
  if ((transferEpDetails.value || transferEpOffset.value) && !transferEpFormat.value) return modal.warning('集数定位必须填写{ep}格式')
  transferLoading.value = true
  try {
    const params: Record<string, string> = {
      inpath: transferPath.value,
      syncmod: transferSyncmod.value,
      type: transferType.value
    }
    if (transferOutPath.value) params.outpath = transferOutPath.value
    if (transferTmdb.value) params.tmdb = transferTmdb.value
    if (transferSeason.value) params.season = transferSeason.value
    if (transferMinFilesize.value) params.min_filesize = transferMinFilesize.value
    if (transferEpFormat.value) params.episode_format = transferEpFormat.value
    if (transferEpDetails.value) params.episode_details = transferEpDetails.value
    if (transferEpOffset.value) params.episode_offset = transferEpOffset.value
    const res = await doAction<{ retcode: number; retmsg: string }>('rename_udf', params)
    if (res.retcode === 0) {
      modal.success('转移成功')
      transferVisible.value = false
      load()
    } else {
      modal.error(res.retmsg || '转移失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '转移失败')
  } finally {
    transferLoading.value = false
  }
}

// ---- 硬链接查询 ----

interface HardlinkFile {
  file: string
  filename: string
  filepath: string
}

const hardlinkVisible = ref(false)
const hardlinkLoading = ref(false)
const hardlinkDir = ref('')
const hardlinkResults = ref<Record<string, HardlinkFile[]>>({})
const hardlinkSearchDirVisible = ref(false)
const hardlinkPendingFiles = ref<string[]>([])

function openHardlink(f: FileItem) {
  hardlinkPendingFiles.value = [f.path]
  if (currentDir.value === '/') {
    hardlinkDir.value = '/' + f.path.split('/')[1]
  } else {
    hardlinkDir.value = currentDir.value
  }
  hardlinkSearchDirVisible.value = true
}

function openHardlinkAll() {
  const filePaths = files.value.filter((f) => !isDir(f)).map((f) => f.path)
  if (filePaths.length === 0) {
    modal.warning('当前目录下没有文件')
    return
  }
  hardlinkPendingFiles.value = filePaths
  if (currentDir.value === '/') {
    hardlinkDir.value = '/'
  } else {
    hardlinkDir.value = currentDir.value
  }
  hardlinkSearchDirVisible.value = true
}

async function doHardlinkSearch() {
  if (!hardlinkDir.value) {
    modal.warning('请填写查找目录')
    return
  }
  hardlinkSearchDirVisible.value = false
  hardlinkLoading.value = true
  hardlinkVisible.value = true
  try {
    const res = await doAction<{ code: number; data: Record<string, HardlinkFile[]> }>('find_hardlinks', {
      files: hardlinkPendingFiles.value,
      dir: hardlinkDir.value
    })
    if (res.code === 0) {
      if (res.data && Object.keys(res.data).length > 0) {
        hardlinkResults.value = res.data
      } else {
        hardlinkResults.value = {}
        modal.success('查询成功，但未找到硬链接文件')
        hardlinkVisible.value = false
      }
    } else {
      modal.error('查询硬链接文件失败')
      hardlinkVisible.value = false
    }
  } catch {
    modal.error('查询硬链接失败')
    hardlinkVisible.value = false
  } finally {
    hardlinkLoading.value = false
  }
}

function chipType(key: string): string {
  const map: Record<string, string> = { name: 'warning', year: 'warning', season_episode: 'warning', title: 'success', tmdbid: 'success', team: 'info' }
  return map[key] || ''
}

function chipLabel(key: string): string {
  const map: Record<string, string> = { name: '名称', year: '年份', season_episode: '季集', title: '标题', tmdbid: 'TMDB ID', restype: '质量', pix: '分辨率', video_codec: '视频编码', audio_codec: '音频编码', team: '制作组' }
  return map[key] || key
}

// ---- TMDB查询 ----

interface TmdbSearchItem {
  tmdb_id: string
  title: string
  year: string
  image: string
  overview: string
  link: string
}

const tmdbSearchVisible = ref(false)
const tmdbSearchKeyword = ref('')
const tmdbSearchResults = ref<TmdbSearchItem[]>([])
const tmdbSearchSelected = ref('')

function searchTmdb() {
  tmdbSearchKeyword.value = ''
  tmdbSearchResults.value = []
  tmdbSearchSelected.value = ''
  transferVisible.value = false
  tmdbSearchVisible.value = true
}

async function doTmdbSearch() {
  if (!tmdbSearchKeyword.value) return modal.warning('请输入名称')
  try {
    const res = await doAction<{ code: number; result: TmdbSearchItem[] }>('search_media_infos', {
      keyword: tmdbSearchKeyword.value,
      searchtype: 'tmdb'
    })
    if (res.code === 0) {
      tmdbSearchResults.value = res.result || []
    } else {
      tmdbSearchResults.value = []
    }
  } catch {
    tmdbSearchResults.value = []
  }
}

function confirmTmdbSearch() {
  if (tmdbSearchSelected.value) {
    transferTmdb.value = tmdbSearchSelected.value
  }
  tmdbSearchVisible.value = false
  transferVisible.value = true
}

function cancelTmdbSearch() {
  tmdbSearchVisible.value = false
  transferVisible.value = true
}

function onTmdbPosterError(e: Event) {
  (e.target as HTMLImageElement).src = '/no-image.png'
}

const hardlinkSelected = ref<Set<string>>(new Set())

function toggleHardlinkSelect(file: string) {
  if (hardlinkSelected.value.has(file)) {
    hardlinkSelected.value.delete(file)
  } else {
    hardlinkSelected.value.add(file)
  }
  hardlinkSelected.value = new Set(hardlinkSelected.value)
}

function selectAllHardlinks() {
  const all = new Set<string>()
  for (const list of Object.values(hardlinkResults.value)) {
    for (const h of list) {
      all.add(h.file)
    }
  }
  hardlinkSelected.value = all
}

async function deleteSelectedHardlinks() {
  const files = Array.from(hardlinkSelected.value)
  if (files.length === 0) {
    modal.warning('没有硬链接文件被选中')
    return
  }
  const ok = await modal.confirm(
    `即将删除所有选中的硬链接文件，如文件所在目录已没有其它媒体文件则目录也将被删除，是否确认？`,
    '删除硬链接'
  )
  if (!ok) return
  const res = await doAction<{ code: number; msg?: string }>('delete_files', { files })
  if (res.code === 0) {
    modal.success('删除成功')
    hardlinkVisible.value = false
  } else {
    modal.error(res.msg || '删除失败')
  }
}
</script>

<template>
  <div class="mediafile" v-loading="loading">
    <PageHeader title="文件管理" description="浏览媒体目录、识别/转移/重命名/删除/字幕/硬链接查询">
      <template #actions>
        <el-button :icon="Back" @click="parentDir">上级目录</el-button>
        <el-button :icon="Upload" @click="openTransferAll">转移目录</el-button>
        <el-button :icon="Link" @click="openHardlinkAll">所有硬链接</el-button>
        <el-button :icon="Refresh" @click="load()">刷新</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" class="path-card">
      <el-input v-model="pathInput" placeholder="目录路径" @keyup.enter="goInput">
        <template #prefix>
          <el-icon><Folder /></el-icon>
        </template>
        <template #append>
          <el-button :icon="Files" @click="goInput">前往</el-button>
        </template>
      </el-input>
      <el-breadcrumb class="crumbs" separator="/">
        <el-breadcrumb-item
          v-for="(c, idx) in breadcrumbs"
          :key="idx"
          @click="goPath(c.path)"
        >
          <span class="crumb-link">{{ c.name }}</span>
        </el-breadcrumb-item>
      </el-breadcrumb>
    </el-card>

    <div class="split-layout">
      <!-- 左侧：目录树 -->
      <div class="dir-panel">
        <el-card shadow="never" class="dir-card">
          <template #header>
            <div class="list-header">
              <el-icon><Folder /></el-icon>
              <span>目录</span>
              <el-button size="small" link :icon="Back" @click="parentDir" style="margin-left:auto;">上级</el-button>
            </div>
          </template>
          <el-tree
            :key="treeKey"
            ref="treeRef"
            :lazy="true"
            :load="treeLazyLoad"
            node-key="id"
            @node-click="handleTreeClick"
            :highlight-current="true"
            :expand-on-click-node="true"
            empty-text="空目录"
          >
            <template #default="{ data }">
              <span class="tree-node">
                <el-icon><Folder /></el-icon>
                <span>{{ data.label }}</span>
              </span>
            </template>
          </el-tree>
        </el-card>
      </div>

      <!-- 右侧：文件列表 -->
      <div class="file-panel">
        <el-card shadow="never">
          <template #header>
            <div class="list-header" style="justify-content:space-between;">
              <div>
                <el-icon><Document /></el-icon>
                <span>文件</span>
                <span class="muted">{{ fileItems.length }} 个</span>
              </div>
              <div class="file-actions">
                <el-button size="small" :icon="Upload" @click="openTransferAll">转移目录</el-button>
                <el-button size="small" :icon="Link" @click="openHardlinkAll">所有硬链接</el-button>
              </div>
            </div>
          </template>
          <div v-loading="loading">
            <div v-if="fileItems.length === 0" style="padding:40px 0;">
              <el-empty description="没有文件" :image-size="80" />
            </div>
            <div v-for="row in fileItems" :key="row.path" class="file-card">
              <div class="file-top">
                <div class="file-info">
                  <el-icon class="file-icon"><Document /></el-icon>
                  <span class="file-name">{{ row.name }}</span>
                  <span class="file-size">{{ row.size || '-' }}</span>
                </div>
                <div class="file-ops">
                  <el-button size="small" link :icon="Tools" @click="doNameTest(row)">识别</el-button>
                  <el-button size="small" link :icon="Upload" @click="openTransfer(row)">转移</el-button>
                  <el-button size="small" link :icon="Download" @click="downloadSubtitle(row)">字幕</el-button>
                  <el-button size="small" link :icon="Link" @click="openHardlink(row)">硬链接查询</el-button>
                  <el-button size="small" link :icon="Edit" @click="openRename(row)">重命名</el-button>
                  <el-button size="small" link type="danger" :icon="Delete" @click="deleteFile(row)">删除</el-button>
                </div>
              </div>
              <!-- 识别结果 inline chips -->
              <div v-if="nameTestResults[row.path]" class="chips-row" v-loading="nameTestLoading[row.path]">
                <template v-if="'title' in nameTestResults[row.path]">
                  <el-tag v-for="key in chipOrder" :key="key" v-show="(nameTestResults[row.path] as any)[key]" size="small" :type="chipType(key)">
                    {{ chipLabel(key) }}：{{ (nameTestResults[row.path] as any)[key] }}
                  </el-tag>
                </template>
                <el-tag v-else type="danger" size="small">{{ (nameTestResults[row.path] as any).name }}</el-tag>
              </div>
            </div>
          </div>
        </el-card>
      </div>
    </div>

    <!-- 转移弹窗 -->
    <el-dialog v-model="transferVisible" title="自定义识别转移" width="640px" destroy-on-close top="30px">
      <el-form label-width="110px">
        <el-form-item label="输入路径" required>
          <el-input v-model="transferPath" placeholder="文件/目录路径" />
        </el-form-item>
        <el-form-item label="输出路径">
          <el-input v-model="transferOutPath" placeholder="转移后文件存储路径，留空则转移至媒体库" />
        </el-form-item>
        <el-form-item label="转移方式" required>
          <el-select v-model="transferSyncmod" style="width: 100%">
            <el-option label="硬链接" value="link" />
            <el-option label="软链接" value="softlink" />
            <el-option label="复制" value="copy" />
            <el-option label="移动" value="move" />
            <el-option label="Rclone复制" value="rclonecopy" />
            <el-option label="Rclone移动" value="rclone" />
            <el-option label="Minio复制" value="miniocopy" />
            <el-option label="Minio移动" value="minio" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="transferType">
            <el-radio value="MOV" border>电影</el-radio>
            <el-radio value="TV" border>电视剧</el-radio>
            <el-radio value="ANIME" border>动漫</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="TMDB ID">
          <el-input v-model="transferTmdb" placeholder="留空自动识别">
            <template #append>
              <el-button @click="searchTmdb">查询</el-button>
            </template>
          </el-input>
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="10">
            <el-form-item label="季">
              <el-select v-model="transferSeason" style="width: 100%" :disabled="transferType === 'MOV'">
                <el-option label="请选择" value="" />
                <el-option v-for="s in 20" :key="s" :label="'第' + s + '季'" :value="String(s)" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="14">
            <el-form-item label="最小文件大小">
              <el-input v-model="transferMinFilesize" placeholder="留空使用默认值，填0不限制">
                <template #suffix>
                  <el-tooltip content="留空将使用基础设置中的转移最小文件大小设置，如不限制大小需输入0" placement="top">
                    <el-icon><QuestionFilled /></el-icon>
                  </el-tooltip>
                </template>
              </el-input>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="集数定位">
          <div style="flex:1">
            <div class="ep-label-row">
              <span class="ep-label">集数定位格式</span>
              <el-tooltip placement="top" width="380" popper-class="ep-tooltip">
                <template #content>
                  <div style="font-size: 12px; line-height: 1.8;">
                    3个都不填，用默认识别<br>
                    1、<b>{ep}</b>: 标定集数位置，例如：<br>
                    &nbsp;&nbsp;'(BD)十二国記 第45話「東の海神 西の滄海 五章」(1440x1080 x264-10bpp flac).mkv'<br>
                    &nbsp;&nbsp;此处可以填'(BD)十二国記 第{ep}話{a}(1440x1080 x264-10bpp flac).mkv'<br>
                    &nbsp;&nbsp;ep表示集，a表示理解成一个变量。<br>
                    2、<b>起始集[, 终止集]</b>：裁定处理集数范围，如果输入是文件，输出就是单个文件，如果输入是目录，输出可以根据填的值批量识别<br>
                    &nbsp;&nbsp;例如 '1' 表示第一集, '2,4' 只取第2集到第4集，'1-2'表示第1-2集<br>
                    3、<b>集数偏移</b>：例如ep定位出集数是11, 实际是第1集, 此处填-10
                  </div>
                </template>
                <el-icon><QuestionFilled /></el-icon>
              </el-tooltip>
            </div>
            <el-row :gutter="8" style="margin-top: 8px;">
              <el-col :span="8">
                <el-input v-model="transferEpFormat" placeholder="{ep}定位集数" size="small" />
              </el-col>
              <el-col :span="8">
                <el-input v-model="transferEpDetails" placeholder="起始集[,终止集]，如1或1,2" size="small" />
              </el-col>
              <el-col :span="8">
                <el-input v-model="transferEpOffset" placeholder="集数偏移，如-10" size="small" />
              </el-col>
            </el-row>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="transferVisible = false">取消</el-button>
        <el-button type="primary" :loading="transferLoading" @click="doTransfer">转移</el-button>
      </template>
    </el-dialog>

    <!-- TMDB查询弹窗 -->
    <el-dialog v-model="tmdbSearchVisible" title="查询TMDB ID" width="580px" destroy-on-close top="30px">
      <div>
        <div style="display:flex; gap:8px; margin-bottom:16px;">
          <el-input v-model="tmdbSearchKeyword" placeholder="输入名称查询" @keyup.enter="doTmdbSearch" />
          <el-button type="primary" :icon="Search" @click="doTmdbSearch">搜索</el-button>
        </div>
        <div v-if="tmdbSearchResults.length > 0" class="tmdb-list">
          <div
            v-for="(item, idx) in tmdbSearchResults"
            :key="idx"
            class="tmdb-item"
            :class="{ selected: tmdbSearchSelected === item.tmdb_id }"
            @click="tmdbSearchSelected = item.tmdb_id"
          >
            <el-radio v-model="tmdbSearchSelected" :value="item.tmdb_id" style="margin-right:8px;" />
            <img class="tmdb-poster" :src="item.image" alt="" @error="onTmdbPosterError">
            <div class="tmdb-info">
              <a :href="item.link" target="_blank" class="tmdb-title">{{ item.title }} ({{ item.year }})</a>
              <div class="tmdb-overview">{{ item.overview }}</div>
            </div>
          </div>
        </div>
        <el-empty v-else-if="tmdbSearchKeyword" description="未找到相关信息" />
      </div>
      <template #footer>
        <el-button @click="cancelTmdbSearch">取消</el-button>
        <el-button type="primary" :disabled="!tmdbSearchSelected" @click="confirmTmdbSearch">确定</el-button>
      </template>
    </el-dialog>

    <!-- 硬链接查询：选择目录弹窗 -->
    <el-dialog v-model="hardlinkSearchDirVisible" title="硬链接查询" width="480px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="查找目录" required>
          <el-input v-model="hardlinkDir" placeholder="选择路径" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="hardlinkSearchDirVisible = false">取消</el-button>
        <el-button type="primary" @click="doHardlinkSearch">确定</el-button>
      </template>
    </el-dialog>

    <!-- 硬链接查询：结果弹窗 -->
    <el-dialog v-model="hardlinkVisible" title="硬链接文件" width="700px" destroy-on-close v-loading="hardlinkLoading">
      <div v-for="(links, file) in hardlinkResults" :key="file" class="hl-group">
        <div class="hl-file-title">{{ file }}</div>
        <div v-for="(h, idx) in links" :key="idx" class="hl-item">
          <el-checkbox
            :model-value="hardlinkSelected.has(h.file)"
            @change="() => toggleHardlinkSelect(h.file)"
          />
          <div class="hl-info">
            <div class="hl-name">
              <el-icon><Link /></el-icon>
              {{ h.filename }}
            </div>
            <div class="hl-path">{{ h.filepath }}</div>
          </div>
        </div>
      </div>
      <template #footer>
        <a class="me-auto" style="cursor: pointer; color: var(--el-color-primary);" @click="selectAllHardlinks">全选</a>
        <el-button type="danger" @click="deleteSelectedHardlinks" :disabled="hardlinkSelected.size === 0">批量删除</el-button>
      </template>
    </el-dialog>

    <!-- 重命名弹窗 -->
    <el-dialog v-model="renameVisible" title="文件重命名" width="480px" destroy-on-close>
      <el-form label-width="80px">
        <el-form-item label="新文件名" required>
          <el-input v-model="renameNewName" placeholder="新文件名" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" @click="doRename">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mediafile {
  padding: 16px;
}
.path-card {
  margin-bottom: 12px;
}
.crumbs {
  margin-top: 12px;
}
.crumb-link {
  cursor: pointer;
  color: var(--el-color-primary);
}
.crumb-link:hover {
  text-decoration: underline;
}
.list-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.muted {
  color: var(--el-text-color-secondary);
  font-weight: normal;
  font-size: 13px;
}
.name-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.name-cell.clickable {
  cursor: pointer;
  color: var(--el-color-primary);
}
.name-cell.clickable:hover {
  text-decoration: underline;
}
.row-icon {
  font-size: 16px;
}
.dir-icon {
  color: var(--el-color-warning);
}
.file-icon {
  color: var(--el-text-color-secondary);
}
.chips-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.hl-group {
  margin-bottom: 16px;
}
.hl-file-title {
  font-weight: 600;
  margin-bottom: 8px;
  padding: 4px 8px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}
.hl-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 8px;
}
.hl-item:hover {
  background: var(--el-fill-color-light);
}
.hl-info {
  flex: 1;
}
.hl-name {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
}
.hl-path {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.ep-label-row {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 0;
}
.ep-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.tmdb-list {
  max-height: 400px;
  overflow-y: auto;
}
.tmdb-item {
  display: flex;
  align-items: flex-start;
  padding: 10px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
  margin-bottom: 6px;
}
.tmdb-item:hover,
.tmdb-item.selected {
  background: var(--el-fill-color-light);
  border-color: var(--el-color-primary);
}
.tmdb-poster {
  width: 50px;
  height: 70px;
  object-fit: cover;
  border-radius: 4px;
  flex-shrink: 0;
}
.tmdb-info {
  flex: 1;
  min-width: 0;
  margin-left: 10px;
}
.tmdb-title {
  font-weight: 600;
  color: var(--el-color-primary);
  text-decoration: none;
  display: block;
  margin-bottom: 4px;
}
.tmdb-title:hover {
  text-decoration: underline;
}
.tmdb-overview {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1.5;
}
.split-layout {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.dir-panel {
  width: 260px;
  flex-shrink: 0;
}
.dir-card {
}
.dir-list {
  max-height: 600px;
  overflow-y: auto;
}
.dir-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--el-color-primary);
}
.dir-item:hover {
  background: var(--el-fill-color-light);
}
.dir-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-panel {
  flex: 1;
  min-width: 0;
}
.file-card {
  padding: 12px 14px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.file-card:last-child {
  border-bottom: none;
}
.file-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.file-info {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}
.file-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-size {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.file-ops {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  flex-shrink: 0;
}
</style>
