<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useConfigForm } from '@/composables/useConfigForm'
import { useModalStore } from '@/stores/modal'
import { updateDirectory } from '@/api/config'

type LibKey = 'movie' | 'tv' | 'anime' | 'unknown'

interface LibSection {
  key: LibKey
  title: string
  configKey: string
}

const SECTIONS: LibSection[] = [
  { key: 'movie', title: '电影', configKey: 'media.movie_path' },
  { key: 'tv', title: '电视剧', configKey: 'media.tv_path' },
  { key: 'anime', title: '动漫', configKey: 'media.anime_path' },
  { key: 'unknown', title: '未识别', configKey: 'media.unknown_path' }
]

const { config, loading, load } = useConfigForm()
const modal = useModalStore()

const dialogVisible = ref(false)
const currentSection = ref<LibSection | null>(null)
const newPath = ref('')
const submitting = ref(false)

function getPaths(section: LibSection): string[] {
  const media = config.value.media as Record<string, unknown> | undefined
  const arr = media?.[`${section.key}_path`]
  return Array.isArray(arr) ? (arr as string[]) : []
}

function openDialog(section: LibSection) {
  currentSection.value = section
  newPath.value = ''
  dialogVisible.value = true
}

async function handleAdd() {
  if (!newPath.value || !currentSection.value) return
  submitting.value = true
  try {
    const res = await updateDirectory('add', currentSection.value.configKey, newPath.value)
    if (res.code === 0) {
      modal.success('添加成功')
      dialogVisible.value = false
      await load()
    } else {
      modal.error('添加失败')
    }
  } catch {
    modal.error('添加失败')
  } finally {
    submitting.value = false
  }
}

async function handleDelete(section: LibSection, path: string) {
  const ok = await modal.confirm(`确定删除目录 "${path}" ？`)
  if (!ok) return
  try {
    const res = await updateDirectory('sub', section.configKey, path)
    if (res.code === 0) {
      modal.success('删除成功')
      await load()
    } else {
      modal.error('删除失败')
    }
  } catch {
    modal.error('删除失败')
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="library-view">
    <PageHeader title="媒体库" description="配置电影、电视剧、动漫、未识别媒体的库目录" />
    <div class="lib-list">
      <el-card v-for="s in SECTIONS" :key="s.key" shadow="never">
        <template #header>
          <div class="card-header">
            <strong>{{ s.title }}</strong>
            <el-button type="primary" :icon="Plus" size="small" @click="openDialog(s)">
              添加目录
            </el-button>
          </div>
        </template>
        <el-table :data="getPaths(s)" :show-header="false" empty-text="未配置">
          <el-table-column label="目录">
            <template #default="{ row }">
              <span>{{ row }}</span>
            </template>
          </el-table-column>
          <el-table-column width="80" align="right">
            <template #default="{ row }">
              <el-button
                type="danger"
                :icon="Delete"
                link
                @click="handleDelete(s, row)"
              />
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" :title="`新增目录 - ${currentSection?.title}`" width="500px">
      <el-form label-width="80px">
        <el-form-item label="路径">
          <el-input v-model="newPath" placeholder="请输入目录路径" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleAdd">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.lib-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
