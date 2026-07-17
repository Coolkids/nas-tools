<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Delete, View } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useConfigForm } from '@/composables/useConfigForm'
import { useModalStore } from '@/stores/modal'
import { doAction } from '@/api'

interface DoubanHistoryItem {
  ID: number
  NAME: string
  YEAR: string
  TYPE: string
  RATING: string
  IMAGE: string
  STATE: string
  ADD_TIME: string
}

const { config, loading, load, save } = useConfigForm()
const modal = useModalStore()

const form = reactive({
  users: '',
  days: '',
  interval: '',
  types: '',
  cookie: '',
  auto_search: false,
  auto_rss: false
})

const historyVisible = ref(false)
const historyLoading = ref(false)
const history = ref<DoubanHistoryItem[]>([])

function initForm() {
  const d = config.value.douban as Record<string, unknown> | undefined
  if (!d) return
  const users = d.users
  form.users = Array.isArray(users) ? (users as string[]).join(',') : ((users as string) || '')
  form.days = (d.days as string) || ''
  form.interval = (d.interval as string) || ''
  form.types = (d.types as string) || ''
  form.cookie = (d.cookie as string) || ''
  form.auto_search = Boolean(d.auto_search)
  form.auto_rss = Boolean(d.auto_rss)
}

async function reload() {
  await load()
  initForm()
}

async function handleSave() {
  const ok = await save({
    'douban.users': form.users,
    'douban.days': form.days,
    'douban.interval': form.interval,
    'douban.types': form.types,
    'douban.cookie': form.cookie,
    'douban.auto_search': form.auto_search,
    'douban.auto_rss': form.auto_rss
  })
  if (ok) await load()
}

async function openHistory() {
  historyVisible.value = true
  historyLoading.value = true
  try {
    const res = await doAction<{ code: number; result?: DoubanHistoryItem[] }>('get_douban_history', {})
    if (res.code === 0 && Array.isArray(res.result)) {
      history.value = res.result
    }
  } catch {
    modal.error('获取历史记录失败')
  } finally {
    historyLoading.value = false
  }
}

async function deleteHistory(id: number) {
  const ok = await modal.confirm('确定删除该历史记录？')
  if (!ok) return
  try {
    const res = await doAction<{ code: number }>('delete_douban_history', { id })
    if (res.code === 0) {
      history.value = history.value.filter((h) => h.ID !== id)
      modal.success('删除成功')
    } else {
      modal.error('删除失败')
    }
  } catch {
    modal.error('删除失败')
  }
}

function stateTag(state: string): { type: 'success' | 'primary' | 'warning'; text: string } {
  if (state === 'DOWNLOADED') return { type: 'success', text: '已下载' }
  if (state === 'RSS') return { type: 'primary', text: '已订阅' }
  if (state === 'NEW') return { type: 'primary', text: '新增' }
  return { type: 'warning', text: '处理中' }
}

onMounted(reload)
</script>

<template>
  <div v-loading="loading" class="douban-view">
    <PageHeader title="豆瓣" description="配置豆瓣同步，自动跟踪标记的想看/在看/看过" />
    <el-card shadow="never">
      <el-form label-width="120px">
        <div class="form-row">
          <el-form-item label="豆瓣用户ID" required>
            <el-input v-model="form.users" placeholder="用户1,用户2,用户3" />
          </el-form-item>
          <el-form-item label="同步周期(天)" required>
            <el-input v-model="form.days" placeholder="30" />
          </el-form-item>
          <el-form-item label="同步间隔(小时)" required>
            <el-input v-model="form.interval" placeholder="留空关闭豆瓣同步" />
          </el-form-item>
          <el-form-item label="同步数据类型" required>
            <el-input v-model="form.types" placeholder="do,wish,collect" />
          </el-form-item>
        </div>
        <el-form-item label="豆瓣Cookie" required>
          <el-input v-model="form.cookie" />
        </el-form-item>
        <div class="form-row">
          <el-form-item label="自动搜索下载">
            <el-switch v-model="form.auto_search" />
          </el-form-item>
          <el-form-item label="自动添加订阅">
            <el-switch v-model="form.auto_rss" />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <div class="card-footer">
          <el-button :icon="View" @click="openHistory">历史记录</el-button>
          <el-button type="primary" @click="handleSave">保存</el-button>
        </div>
      </template>
    </el-card>

    <el-dialog v-model="historyVisible" title="豆瓣历史记录" width="760px">
      <el-table v-loading="historyLoading" :data="history" empty-text="没有数据">
        <el-table-column label="" width="64">
          <template #default="{ row }">
            <img class="history-img" :src="row.IMAGE" alt="" />
          </template>
        </el-table-column>
        <el-table-column label="标题">
          <template #default="{ row }">
            <div>{{ row.NAME }} ({{ row.YEAR }})</div>
            <div v-if="row.RATING" class="text-muted">评分：{{ row.RATING }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="TYPE" label="类型" width="90" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="stateTag(row.STATE).type" size="small">
              {{ stateTag(row.STATE).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="ADD_TIME" label="添加时间" width="170" />
        <el-table-column label="" width="70" align="right">
          <template #default="{ row }">
            <el-button :icon="Delete" type="danger" link @click="deleteHistory(row.ID)" />
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button type="primary" @click="historyVisible = false">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.form-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0 16px;
}
.card-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.history-img {
  width: 48px;
  height: 64px;
  object-fit: cover;
  border-radius: 4px;
}
.text-muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
