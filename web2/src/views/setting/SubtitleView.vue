<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import { useConfigForm } from '@/composables/useConfigForm'
import { useModalStore } from '@/stores/modal'

type SubType = 'opensubtitles' | 'chinesesubfinder'

interface ServerOption {
  type: SubType
  name: string
}

const SERVERS: ServerOption[] = [
  { type: 'opensubtitles', name: 'OpenSubtitles' },
  { type: 'chinesesubfinder', name: 'ChineseSubFinder' }
]

const { config, loading, load, save } = useConfigForm()
const modal = useModalStore()

const dialogVisible = ref(false)
const currentType = ref<SubType | null>(null)
const form = reactive({
  enable: false,
  host: '',
  api_key: '',
  local_path: '',
  remote_path: ''
})

const activeServer = computed(() => {
  const sub = config.value.subtitle as Record<string, unknown> | undefined
  return sub?.server as string | undefined
})

function openDialog(type: SubType) {
  currentType.value = type
  const sub = config.value.subtitle as Record<string, Record<string, unknown>> | undefined
  if (type === 'opensubtitles') {
    form.enable = Boolean(sub?.opensubtitles?.enable)
  } else {
    const csf = sub?.chinesesubfinder || {}
    form.host = (csf.host as string) || ''
    form.api_key = (csf.api_key as string) || ''
    form.local_path = (csf.local_path as string) || ''
    form.remote_path = (csf.remote_path as string) || ''
  }
  dialogVisible.value = true
}

async function handleSave() {
  if (!currentType.value) return
  const items: Record<string, unknown> = { 'subtitle.server': currentType.value }
  if (currentType.value === 'opensubtitles') {
    items['subtitle.opensubtitles.enable'] = form.enable
  } else {
    items['subtitle.chinesesubfinder.host'] = form.host
    items['subtitle.chinesesubfinder.api_key'] = form.api_key
    items['subtitle.chinesesubfinder.local_path'] = form.local_path
    items['subtitle.chinesesubfinder.remote_path'] = form.remote_path
  }
  const ok = await save(items)
  if (ok) dialogVisible.value = false
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="subtitle-view">
    <PageHeader title="字幕" description="选择并配置字幕下载服务" />
    <div class="server-grid">
      <el-card
        v-for="s in SERVERS"
        :key="s.type"
        shadow="hover"
        class="server-card"
        @click="openDialog(s.type)"
      >
        <div class="server-body">
          <div class="server-name">{{ s.name }}</div>
          <div class="server-status">
            <el-tag v-if="activeServer === s.type" type="success" size="small" effect="dark">
              正在使用
            </el-tag>
            <span v-else class="server-hint">点击配置</span>
          </div>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" :title="currentType === 'opensubtitles' ? 'OpenSubtitles' : 'ChineseSubFinder'" width="560px">
      <el-form v-if="currentType === 'opensubtitles'" label-width="140px">
        <el-form-item label="开启字幕下载">
          <el-switch v-model="form.enable" />
          <span class="form-tip">开启 opensubtitles.org 字幕下载</span>
        </el-form-item>
      </el-form>
      <el-form v-else label-width="110px">
        <el-form-item label="服务器地址" required>
          <el-input v-model="form.host" placeholder="http://127.0.0.1:19035" />
        </el-form-item>
        <el-form-item label="Api Key" required>
          <el-input v-model="form.api_key" />
        </el-form-item>
        <el-form-item label="本地路径">
          <el-input v-model="form.local_path" placeholder="本地映射路径" />
        </el-form-item>
        <el-form-item label="远程路径">
          <el-input v-model="form.remote_path" placeholder="远程映射路径" />
        </el-form-item>
      </el-form>
      <template #footer>
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
.form-tip {
  margin-left: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
