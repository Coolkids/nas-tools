<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Connection } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useConfigForm } from '@/composables/useConfigForm'
import { useModalStore } from '@/stores/modal'
import { testConnection } from '@/api/config'

type ServerKeyType = 'emby' | 'jellyfin' | 'plex'

interface ServerField {
  key: string
  label: string
  placeholder?: string
  inputType?: 'text' | 'password'
  required?: boolean
}

interface ServerType {
  type: ServerKeyType
  name: string
  testCommand: string
  fields: ServerField[]
}

const SERVERS: ServerType[] = [
  {
    type: 'emby',
    name: 'Emby',
    testCommand: 'app.mediaserver.client.emby|Emby',
    fields: [
      { key: 'host', label: '服务器地址', placeholder: 'http://127.0.0.1:8096', required: true },
      { key: 'api_key', label: 'Api Key', required: true }
    ]
  },
  {
    type: 'jellyfin',
    name: 'Jellyfin',
    testCommand: 'app.mediaserver.client.jellyfin|Jellyfin',
    fields: [
      { key: 'host', label: '服务器地址', placeholder: 'http://127.0.0.1:8096', required: true },
      { key: 'api_key', label: 'Api Key', required: true }
    ]
  },
  {
    type: 'plex',
    name: 'Plex',
    testCommand: 'app.mediaserver.client.plex|Plex',
    fields: [
      { key: 'host', label: '服务器地址', placeholder: 'http://127.0.0.1:32400', required: true },
      { key: 'token', label: 'X-Plex-Token' },
      { key: 'servername', label: '服务器名称' },
      { key: 'username', label: '用户名' },
      { key: 'password', label: '密码', inputType: 'password' }
    ]
  }
]

const { config, loading, load, save } = useConfigForm()
const modal = useModalStore()

const dialogVisible = ref(false)
const currentServer = ref<ServerType | null>(null)
const form = reactive<Record<string, string>>({})
const testing = ref(false)

const activeType = computed(() => {
  const media = config.value.media as Record<string, unknown> | undefined
  return media?.media_server as string | undefined
})

function serverConfig(type: string): Record<string, unknown> | undefined {
  return config.value[type] as Record<string, unknown> | undefined
}

function openDialog(server: ServerType) {
  currentServer.value = server
  const cfg = serverConfig(server.type) || {}
  server.fields.forEach((f) => {
    form[f.key] = (cfg[f.key] as string) || ''
  })
  dialogVisible.value = true
}

function buildItems(): Record<string, unknown> {
  const items: Record<string, unknown> = {
    'media.media_server': currentServer.value!.type
  }
  currentServer.value!.fields.forEach((f) => {
    items[`${currentServer.value!.type}.${f.key}`] = form[f.key] ?? ''
  })
  return items
}

async function handleSave() {
  const ok = await save(buildItems())
  if (ok) dialogVisible.value = false
}

async function handleTest() {
  testing.value = true
  try {
    const applied = await save(buildItems(), true)
    if (!applied) return
    const res = await testConnection(currentServer.value!.testCommand)
    if (res.code === 0) modal.success('测试成功')
    else modal.error(res.msg || '测试失败')
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="media-server-view">
    <PageHeader title="媒体服务器" description="选择并配置媒体服务器，用于媒体库展示与状态同步" />
    <div class="server-grid">
      <el-card
        v-for="s in SERVERS"
        :key="s.type"
        shadow="hover"
        class="server-card"
        @click="openDialog(s)"
      >
        <div class="server-body">
          <div class="server-name">{{ s.name }}</div>
          <div class="server-status">
            <el-tag v-if="activeType === s.type" type="success" size="small" effect="dark">
              正在使用
            </el-tag>
            <span v-else class="server-hint">点击配置</span>
          </div>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" :title="currentServer?.name" width="560px">
      <el-form v-if="currentServer" label-width="110px">
        <el-form-item
          v-for="f in currentServer.fields"
          :key="f.key"
          :label="f.label"
          :required="f.required"
        >
          <el-input
            v-model="form[f.key]"
            :type="f.inputType || 'text'"
            :placeholder="f.placeholder"
            :show-password="f.inputType === 'password'"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :icon="Connection" :loading="testing" @click="handleTest">测试</el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSave">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.server-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 16px;
}
.server-card {
  cursor: pointer;
  transition: transform 0.15s ease;
}
.server-card:hover {
  transform: translateY(-2px);
}
.server-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
}
.server-name {
  font-size: 16px;
  font-weight: 600;
}
.server-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
