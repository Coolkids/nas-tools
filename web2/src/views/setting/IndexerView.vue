<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Connection } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useConfigForm } from '@/composables/useConfigForm'
import { useModalStore } from '@/stores/modal'
import { doAction } from '@/api'
import { testConnection } from '@/api/config'

type IndexerType = 'builtin' | 'jackett' | 'prowlarr'

interface IndexerItem {
  id: string
  name: string
}

interface ExternalServer {
  type: 'jackett' | 'prowlarr'
  name: string
  testCommand: string
  fields: { key: string; label: string; placeholder?: string; inputType?: 'text' | 'password'; required?: boolean }[]
}

const EXTERNAL_SERVERS: ExternalServer[] = [
  {
    type: 'jackett',
    name: 'Jackett',
    testCommand: 'app.indexer.client.jackett|Jackett',
    fields: [
      { key: 'host', label: 'Jackett地址', placeholder: 'http://127.0.0.1:9117', required: true },
      { key: 'api_key', label: 'Api Key', required: true },
      { key: 'password', label: '密码', inputType: 'password' }
    ]
  },
  {
    type: 'prowlarr',
    name: 'Prowlarr',
    testCommand: 'app.indexer.client.prowlarr|Prowlarr',
    fields: [
      { key: 'host', label: 'Prowlarr地址', placeholder: 'http://127.0.0.1:9696', required: true },
      { key: 'api_key', label: 'Api Key', required: true }
    ]
  }
]

const { config, loading, load, save } = useConfigForm()
const modal = useModalStore()

const dialogVisible = ref(false)
const currentType = ref<IndexerType | null>(null)
const currentExternal = ref<ExternalServer | null>(null)

const indexers = ref<IndexerItem[]>([])
const indexersLoading = ref(false)
const selectedSites = ref<string[]>([])

const externalForm = reactive<Record<string, string>>({})
const testing = ref(false)

const isAllSelected = computed(() =>
  indexers.value.length > 0 && selectedSites.value.length === indexers.value.length
)

function toggleAll(val: boolean) {
  selectedSites.value = val ? indexers.value.map((i) => i.id) : []
}

const activeIndexer = computed(() => {
  const pt = config.value.pt as Record<string, unknown> | undefined
  return (pt?.search_indexer as string) || ''
})

function savedSites(): string[] {
  const pt = config.value.pt as Record<string, unknown> | undefined
  const arr = pt?.indexer_sites
  return Array.isArray(arr) ? (arr as string[]) : []
}

function externalConfig(type: string): Record<string, unknown> | undefined {
  return config.value[type] as Record<string, unknown> | undefined
}

async function openBuiltin() {
  currentType.value = 'builtin'
  currentExternal.value = null
  selectedSites.value = [...savedSites()]
  if (indexers.value.length === 0) {
    indexersLoading.value = true
    try {
      const res = await doAction<{ code: number; indexers?: IndexerItem[] }>('get_indexers', {})
      if (res.code === 0 && Array.isArray(res.indexers)) {
        indexers.value = res.indexers
      }
    } catch {
      modal.error('获取索引器列表失败')
    } finally {
      indexersLoading.value = false
    }
  }
  dialogVisible.value = true
}

function openExternal(server: ExternalServer) {
  currentType.value = server.type
  currentExternal.value = server
  const cfg = externalConfig(server.type) || {}
  server.fields.forEach((f) => {
    externalForm[f.key] = (cfg[f.key] as string) || ''
  })
  dialogVisible.value = true
}

async function handleSave() {
  if (currentType.value === 'builtin') {
    const ok = await save({
      'pt.search_indexer': 'builtin',
      'pt.indexer_sites': selectedSites.value
    })
    if (ok) {
      dialogVisible.value = false
      await load()
    }
    return
  }
  if (!currentExternal.value) return
  const items: Record<string, unknown> = {
    'pt.search_indexer': currentExternal.value.type
  }
  currentExternal.value.fields.forEach((f) => {
    items[`${currentExternal.value!.type}.${f.key}`] = externalForm[f.key] ?? ''
  })
  const ok = await save(items)
  if (ok) {
    dialogVisible.value = false
    await load()
  }
}

async function handleTest() {
  if (!currentExternal.value) return
  testing.value = true
  try {
    const items: Record<string, unknown> = {
      'pt.search_indexer': currentExternal.value.type
    }
    currentExternal.value.fields.forEach((f) => {
      items[`${currentExternal.value!.type}.${f.key}`] = externalForm[f.key] ?? ''
    })
    const applied = await save(items, true)
    if (!applied) return
    const res = await testConnection(currentExternal.value.testCommand)
    if (res.code === 0) modal.success('测试成功')
    else modal.error(res.msg || '测试失败')
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="indexer-view">
    <PageHeader title="索引器" description="选择并配置资源索引器" />
    <div class="server-grid">
      <el-card shadow="hover" class="server-card" @click="openBuiltin">
        <div class="server-body">
          <div class="server-name">内建索引器</div>
          <div class="server-status">
            <el-tag v-if="activeIndexer === 'builtin'" type="success" size="small" effect="dark">
              正在使用
            </el-tag>
            <span v-else class="server-hint">点击配置</span>
          </div>
        </div>
      </el-card>
      <el-card
        v-for="s in EXTERNAL_SERVERS"
        :key="s.type"
        shadow="hover"
        class="server-card"
        @click="openExternal(s)"
      >
        <div class="server-body">
          <div class="server-name">{{ s.name }}</div>
          <div class="server-status">
            <el-tag v-if="activeIndexer === s.type" type="success" size="small" effect="dark">
              正在使用
            </el-tag>
            <span v-else class="server-hint">点击配置</span>
          </div>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" :title="currentType === 'builtin' ? '内建索引器' : (currentExternal?.name || '')" width="640px">
      <div v-if="currentType === 'builtin'" v-loading="indexersLoading">
        <div class="builtin-header">
          <label class="field-label">索引站点</label>
          <el-checkbox :model-value="isAllSelected" @change="(val: boolean) => toggleAll(val)">全选</el-checkbox>
        </div>
        <el-checkbox-group v-model="selectedSites" class="site-group">
          <el-checkbox v-for="i in indexers" :key="i.id" :label="i.id" class="site-item">
            {{ i.name }}
          </el-checkbox>
        </el-checkbox-group>
        <el-empty v-if="!indexersLoading && indexers.length === 0" description="暂无可用索引站点" />
      </div>
      <el-form v-else-if="currentExternal" label-width="110px">
        <el-form-item
          v-for="f in currentExternal.fields"
          :key="f.key"
          :label="f.label"
          :required="f.required"
        >
          <el-input
            v-model="externalForm[f.key]"
            :type="f.inputType || 'text'"
            :placeholder="f.placeholder"
            :show-password="f.inputType === 'password'"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <template v-if="currentExternal">
          <el-button :icon="Connection" :loading="testing" @click="handleTest">测试</el-button>
        </template>
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
.builtin-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.field-label {
  font-weight: 600;
}
.site-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  max-height: 360px;
  overflow-y: auto;
}
.site-item {
  margin-right: 0;
}
</style>
