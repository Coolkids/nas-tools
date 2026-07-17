<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Connection, FolderOpened, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useConfigForm } from '@/composables/useConfigForm'
import { useModalStore } from '@/stores/modal'
import { doAction } from '@/api'
import { testConnection, getSystemConfig, setSystemConfig } from '@/api/config'

interface FieldDef {
  id: string
  required?: boolean
  title: string
  tooltip?: string
  type: 'text' | 'password' | 'switch'
  placeholder?: string
}
interface DownloaderDef {
  type: string
  name: string
  testCommand: string
  config: Record<string, FieldDef>
}

const DOWNLOADERS: DownloaderDef[] = [
  {
    type: 'qbittorrent', name: 'Qbittorrent', testCommand: 'app.downloader.client.qbittorrent|Qbittorrent',
    config: {
      qbhost: { id: 'qbittorrent.qbhost', required: true, title: 'IP地址', type: 'text', placeholder: '127.0.0.1' },
      qbport: { id: 'qbittorrent.qbport', required: true, title: '端口', type: 'text', placeholder: '8080' },
      qbusername: { id: 'qbittorrent.qbusername', required: true, title: '用户名', type: 'text', placeholder: 'admin' },
      qbpassword: { id: 'qbittorrent.qbpassword', required: false, title: '密码', type: 'password', placeholder: 'adminadmin' },
      force_upload: { id: 'qbittorrent.force_upload', required: false, title: '自动强制作种', type: 'switch' },
      auto_management: { id: 'qbittorrent.auto_management', required: false, title: '自动管理模式', type: 'switch' }
    }
  },
  {
    type: 'transmission', name: 'Transmission', testCommand: 'app.downloader.client.transmission|Transmission',
    config: {
      trhost: { id: 'transmission.trhost', required: true, title: 'IP地址', type: 'text', placeholder: '127.0.0.1' },
      trport: { id: 'transmission.trport', required: true, title: '端口', type: 'text', placeholder: '9091' },
      trusername: { id: 'transmission.trusername', required: true, title: '用户名', type: 'text', placeholder: 'admin' },
      trpassword: { id: 'transmission.trpassword', required: false, title: '密码', type: 'password' }
    }
  },
  {
    type: 'client115', name: '115网盘', testCommand: 'app.downloader.client.client115|Client115',
    config: {
      cookie: { id: 'client115.cookie', required: true, title: 'Cookie', type: 'text', placeholder: 'USERSESSIONID=xxx;...' }
    }
  },
  {
    type: 'aria2', name: 'Aria2', testCommand: 'app.downloader.client.aria2|Aria2',
    config: {
      host: { id: 'aria2.host', required: true, title: 'IP地址', type: 'text', placeholder: '127.0.0.1' },
      port: { id: 'aria2.port', required: true, title: '端口', type: 'text', placeholder: '6800' },
      secret: { id: 'aria2.secret', required: true, title: '令牌', type: 'text' }
    }
  },
  {
    type: 'pikpak', name: 'PikPak', testCommand: 'app.downloader.client.pikpak|PikPak',
    config: {
      username: { id: 'pikpak.username', required: true, title: '用户名', type: 'text' },
      password: { id: 'pikpak.password', required: true, title: '密码', type: 'password' },
      proxy: { id: 'pikpak.proxy', required: false, title: '代理', type: 'text', placeholder: '127.0.0.1:7890' }
    }
  }
]

const DIR_TYPES = [
  { value: '', label: '全部' },
  { value: '电影', label: '电影' },
  { value: '电视剧', label: '电视剧' },
  { value: '动漫', label: '动漫' }
]

const { config, loading, load, save } = useConfigForm()
const modal = useModalStore()

const dialogVisible = ref(false)
const current = ref<DownloaderDef | null>(null)
const formValues = reactive<Record<string, unknown>>({})
const saving = ref(false)
const testing = ref(false)

const dirVisible = ref(false)
const dirList = ref<Array<{ type: string; category: string; save_path: string; container_path: string; label: string }>>([])

const speedVisible = ref(false)
const speedLoading = ref(false)
const speedSaving = ref(false)
const speedForm = reactive({
  qb_upload: '',
  qb_download: '',
  tr_upload: '',
  tr_download: '',
  ipv4: '',
  ipv6: '',
  bandwidth: '',
  residual_ratio: '',
  allocation: ''
})

onMounted(load)

function getCfg(path: string): unknown {
  const parts = path.split('.')
  let cur: unknown = config.value
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[p]
  }
  return cur
}

function activeType(): string {
  const pt = config.value.pt as Record<string, unknown> | undefined
  return (pt?.pt_client as string) || ''
}

function syncConfig() {
  dirList.value = (getCfg('downloaddir') as typeof dirList.value) || []
}

async function loadData() {
  await load()
  syncConfig()
}

function openDownloader(d: DownloaderDef) {
  current.value = d
  for (const [key, f] of Object.entries(d.config)) {
    const v = getCfg(f.id)
    if (f.type === 'switch') formValues[key] = !!v
    else formValues[key] = v ?? ''
  }
  dialogVisible.value = true
}

function buildItems(): Record<string, unknown> {
  if (!current.value) return {}
  const items: Record<string, unknown> = { 'pt.pt_client': current.value.type }
  for (const [key, f] of Object.entries(current.value.config)) {
    items[f.id] = formValues[key]
  }
  return items
}

async function handleSave() {
  saving.value = true
  try {
    const ok = await save(buildItems())
    if (ok) dialogVisible.value = false
  } finally {
    saving.value = false
  }
}

async function handleTest() {
  testing.value = true
  try {
    const applied = await save(buildItems(), true)
    if (!applied) return
    const res = await testConnection(current.value!.testCommand)
    if (res.code === 0) modal.success('测试成功')
    else modal.error(res.msg || '测试失败')
  } finally {
    testing.value = false
  }
}

function openDir() {
  dirVisible.value = true
}

function addDir() {
  dirList.value.push({ type: '', category: '', save_path: '', container_path: '', label: '' })
}

function removeDir(idx: number) {
  dirList.value.splice(idx, 1)
}

async function saveDir() {
  const ok = await save({ downloaddir: dirList.value })
  if (ok) dirVisible.value = false
}

async function openSpeed() {
  speedVisible.value = true
  speedLoading.value = true
  try {
    const res = await getSystemConfig('SpeedLimit')
    if (res.code === 0 && res.value) {
      Object.keys(speedForm).forEach((k) => {
        speedForm[k as keyof typeof speedForm] = (res.value[k] as string) || ''
      })
    }
  } finally {
    speedLoading.value = false
  }
}

async function saveSpeed() {
  speedSaving.value = true
  try {
    const res = await setSystemConfig('SpeedLimit', { ...speedForm })
    if (res.code === 0) {
      modal.success('保存成功')
      speedVisible.value = false
    } else {
      modal.error(res.msg || '保存失败')
    }
  } finally {
    speedSaving.value = false
  }
}
</script>

<template>
  <div class="downloader-view" v-loading="loading">
    <PageHeader title="下载器" description="配置主下载器及连接参数">
      <template #actions>
        <el-button :icon="FolderOpened" @click="openDir">下载目录</el-button>
        <el-button :icon="Refresh" @click="openSpeed">播放限速</el-button>
      </template>
    </PageHeader>

    <div class="downloader-grid">
      <el-card
        v-for="d in DOWNLOADERS"
        :key="d.type"
        shadow="hover"
        class="downloader-card"
        @click="openDownloader(d)"
      >
        <div class="dl-body">
          <div class="dl-name">{{ d.name }}</div>
          <div class="dl-status">
            <el-tag v-if="activeType() === d.type" type="success" size="small" effect="dark">默认使用</el-tag>
            <span v-else class="dl-hint">点击配置</span>
          </div>
        </div>
      </el-card>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="current?.name || '下载器配置'"
      width="640px"
      :close-on-click-modal="false"
    >
      <el-form label-width="140px">
        <el-row :gutter="12">
          <el-col
            v-for="[key, f] in Object.entries(current?.config || {})"
            :key="f.id"
            :span="f.type === 'switch' ? 24 : 12"
          >
            <el-form-item :label="f.title" :required="f.required">
              <el-switch v-if="f.type === 'switch'" v-model="formValues[key]" />
              <el-input
                v-else
                v-model="formValues[key]"
                :type="f.type === 'password' ? 'password' : 'text'"
                :show-password="f.type === 'password'"
                :placeholder="f.placeholder"
              />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button :icon="Connection" :loading="testing" @click="handleTest">测试</el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="dirVisible" title="下载目录配置" width="900px" :close-on-click-modal="false">
      <div class="dir-list">
        <div v-for="(d, idx) in dirList" :key="idx" class="dir-row">
          <el-select v-model="d.type" placeholder="类型" style="width: 110px">
            <el-option v-for="t in DIR_TYPES" :key="t.value" :value="t.value" :label="t.label" />
          </el-select>
          <el-input v-model="d.category" placeholder="二级分类" style="width: 120px" />
          <el-input v-model="d.save_path" placeholder="下载保存目录" />
          <el-input v-model="d.container_path" placeholder="访问目录" />
          <el-input v-model="d.label" placeholder="分类标签" />
          <el-button :icon="Refresh" link type="danger" @click="removeDir(idx)" />
        </div>
        <el-button :icon="Refresh" @click="addDir">增加目录</el-button>
      </div>
      <template #footer>
        <el-button @click="dirVisible = false">取消</el-button>
        <el-button type="primary" @click="saveDir">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="speedVisible" title="播放限速设置" width="760px" :close-on-click-modal="false">
      <el-form v-loading="speedLoading" label-width="160px">
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="Qbittorrent上传">
              <el-input v-model="speedForm.qb_upload" placeholder="Kb/s" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="Qbittorrent下载">
              <el-input v-model="speedForm.qb_download" placeholder="Kb/s" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="Transmission上传">
              <el-input v-model="speedForm.tr_upload" placeholder="Kb/s" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="Transmission下载">
              <el-input v-model="speedForm.tr_download" placeholder="Kb/s" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="不限速源IPv4">
              <el-input v-model="speedForm.ipv4" placeholder="IPv4 CIDR" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="不限速源IPv6">
              <el-input v-model="speedForm.ipv6" placeholder="IPv6 CIDR" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-divider content-position="left">自动限速设置</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="上行带宽">
              <el-input v-model="speedForm.bandwidth" placeholder="Mbps" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="剩余比例">
              <el-input v-model="speedForm.residual_ratio" placeholder="0.5" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="分配比例">
              <el-input v-model="speedForm.allocation" placeholder="1:1" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="speedVisible = false">取消</el-button>
        <el-button type="primary" :loading="speedSaving" @click="saveSpeed">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.downloader-view {
  padding: 16px;
}
.downloader-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
}
.downloader-card {
  cursor: pointer;
  transition: transform 0.15s ease;
}
.downloader-card:hover {
  transform: translateY(-2px);
}
.dl-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
}
.dl-name {
  font-size: 16px;
  font-weight: 600;
}
.dl-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.dir-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dir-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.dir-row .el-input {
  flex: 1;
}
</style>
