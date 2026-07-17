<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Plus, Refresh, Edit, Delete } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'
import {
  getRssParserList,
  getRssParserDetail,
  updateRssParser,
  deleteRssParser,
  type RssParser,
  type RssParserPayload
} from '@/api/rss'

const modal = useModalStore()

const loading = ref(false)
const list = ref<RssParser[]>([])

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await getRssParserList()
    if (res.code === 0) list.value = res.detail || []
    else modal.error(res.msg || '获取解析器列表失败')
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取解析器列表失败')
  } finally {
    loading.value = false
  }
}

const count = computed(() => list.value.length)

const dialogVisible = ref(false)
const dialogTitle = ref('新增解析器')
const submitting = ref(false)
const form = reactive({
  id: '' as string | number,
  name: '',
  type: 'XML',
  format: '',
  params: ''
})

const typeOptions = [
  { label: 'XML', value: 'XML' },
  { label: 'JSON', value: 'JSON' }
]

function resetForm() {
  form.id = ''
  form.name = ''
  form.type = 'XML'
  form.format = ''
  form.params = ''
}

function openAdd() {
  resetForm()
  dialogTitle.value = '新增解析器'
  dialogVisible.value = true
}

async function openEdit(row: RssParser) {
  try {
    const res = await getRssParserDetail(row.id)
    if (res.code === 0 && res.detail) {
      form.id = res.detail.id
      form.name = res.detail.name
      form.type = res.detail.type || 'XML'
      form.format = res.detail.format || ''
      form.params = res.detail.params || ''
      dialogTitle.value = '编辑解析器'
      dialogVisible.value = true
    } else {
      modal.error('获取解析器详情失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取解析器详情失败')
  }
}

async function submit() {
  if (!form.name) {
    modal.error('请输入名称')
    return
  }
  submitting.value = true
  try {
    const payload: RssParserPayload = {
      name: form.name,
      type: form.type,
      format: form.format,
      params: form.params
    }
    if (form.id) payload.id = form.id
    const res = await updateRssParser(payload)
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

async function onDelete(row: RssParser) {
  const ok = await modal.confirm(`确认删除解析器「${row.name}」？`, '删除解析器')
  if (!ok) return
  try {
    const res = await deleteRssParser(row.id)
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
</script>

<template>
  <div class="rss-parser">
    <PageHeader title="RSS解析器" description="管理自定义RSS订阅的解析规则">
      <template #actions>
        <el-button :icon="Plus" type="primary" @click="openAdd">新增解析器</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" v-loading="loading">
      <div class="toolbar">共 {{ count }} 条记录</div>
      <el-table :data="list" style="width: 100%" empty-text="没有数据">
        <el-table-column label="名称" prop="name" min-width="160" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.type || '' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="附加参数" prop="params" min-width="200" show-overflow-tooltip />
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button :icon="Edit" size="small" text @click="openEdit(row)">编辑</el-button>
            <el-button :icon="Delete" size="small" text type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="720px" destroy-on-close>
      <el-form :model="form" label-width="90px" label-position="right">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="自定义名称" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="类型">
              <el-select v-model="form.type" style="width: 100%">
                <el-option v-for="t in typeOptions" :key="t.value" :label="t.label" :value="t.value" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="解析格式">
          <el-input
            v-model="form.format"
            type="textarea"
            :autosize="{ minRows: 8, maxRows: 18 }"
            placeholder="JSON 解析配置：list / title / enclosure / description / size / type / year"
          />
        </el-form-item>
        <el-form-item label="附加参数">
          <el-input v-model="form.params" placeholder="附加到RSS链接中的参数" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">
          {{ form.id ? '保存' : '新增' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.rss-parser {
  padding: 16px;
}
.toolbar {
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
