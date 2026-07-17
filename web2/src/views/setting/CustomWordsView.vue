<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Plus,
  Edit,
  Delete,
  Refresh,
  VideoPlay,
  VideoPause,
  Upload as UploadIcon,
  Download as DownloadIcon,
  DocumentCopy,
  QuestionFilled
} from '@element-plus/icons-vue'
import { doAction } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import { useModalStore } from '@/stores/modal'

interface CustomWord {
  id: number
  replaced: string
  replace: string
  front: string
  back: string
  offset: string
  regex: number
  season: number
  type: number
  enabled: number
  help: string
  [key: string]: unknown
}

interface WordGroup {
  id: number
  name: string
  type: number
  seasons: number
  link?: string
  words: CustomWord[]
  [key: string]: unknown
}

interface GroupsResult {
  code: number
  msg?: string
  result: WordGroup[]
}

interface WordDetailResult {
  code: number
  msg?: string
  data: CustomWord
}

const modal = useModalStore()
const loading = ref(false)
const groups = ref<WordGroup[]>([])
const selectedIds = ref<Set<string>>(new Set())

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await doAction<GroupsResult>('get_customwords', {})
    if (res.code === 0) {
      groups.value = res.result || []
    } else {
      modal.error(res.msg || '获取识别词失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取识别词失败')
  } finally {
    loading.value = false
  }
}

const selectedCount = computed(() => selectedIds.value.size)

function toggleSelect(key: string) {
  if (selectedIds.value.has(key)) selectedIds.value.delete(key)
  else selectedIds.value.add(key)
  selectedIds.value = new Set(selectedIds.value)
}

function selectGroupAll(group: WordGroup, checked: boolean) {
  for (const w of group.words) {
    const key = `${group.id}_${w.id}`
    if (checked) selectedIds.value.add(key)
    else selectedIds.value.delete(key)
  }
  selectedIds.value = new Set(selectedIds.value)
}

const editVisible = ref(false)
const editTitle = ref('新增识别词')
const form = reactive({
  id: '' as string | number,
  gid: '' as string | number,
  group_type: 1 as number,
  new_replaced: '',
  new_replace: '',
  new_front: '',
  new_back: '',
  new_offset: '',
  new_help: '',
  season: -1,
  type: 1,
  enabled: 1,
  regex: 1
})

function openAdd(group: WordGroup) {
  editTitle.value = '新增识别词'
  form.id = ''
  form.gid = group.id
  form.group_type = group.type
  form.new_replaced = ''
  form.new_replace = ''
  form.new_front = ''
  form.new_back = ''
  form.new_offset = ''
  form.new_help = ''
  form.season = group.type === 1 ? -1 : -1
  form.type = 1
  form.enabled = 1
  form.regex = 1
  editVisible.value = true
}

async function openEdit(word: CustomWord, group: WordGroup) {
  editTitle.value = '编辑识别词'
  try {
    const res = await doAction<WordDetailResult>('get_custom_word', { wid: word.id })
    if (res.code === 0 && res.data) {
      const w = res.data
      form.id = word.id
      form.gid = group.id
      form.group_type = group.type
      form.new_replaced = w.replaced
      form.new_replace = w.replace
      form.new_front = w.front
      form.new_back = w.back
      form.new_offset = w.offset
      form.new_help = w.help
      form.season = group.type === 1 ? -1 : w.season
      form.type = w.type
      form.enabled = w.enabled
      form.regex = w.regex
      editVisible.value = true
    } else {
      modal.error(res.msg || '获取识别词详情失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '获取识别词详情失败')
  }
}

async function submitWord() {
  if (form.type === 1 && !form.new_replaced) return modal.warning('请填写被替换词')
  if ((form.type === 2 || form.type === 3) && (!form.new_replaced || !form.new_replace)) return modal.warning('请填写被替换词和替换词')
  if ((form.type === 3 || form.type === 4) && (!form.new_front || !form.new_back || !form.new_offset)) return modal.warning('请填写定位词和偏移集数')
  try {
    const payload: Record<string, string> = {}
    for (const [k, v] of Object.entries(form)) {
      payload[k] = String(v)
    }
    const res = await doAction<{ code: number; msg?: string }>('add_or_edit_custom_word', payload)
    if (res.code === 0) {
      modal.success('保存成功')
      editVisible.value = false
      load()
    } else {
      modal.error(res.msg || '保存失败')
    }
  } catch (e) {
    modal.error(e instanceof Error ? e.message : '保存失败')
  }
}

async function deleteWord(word: CustomWord) {
  const ok = await modal.confirm(`确认删除识别词「${word.replaced || word.front}」？`, '删除识别词')
  if (!ok) return
  const res = await doAction<{ code: number; msg?: string }>('delete_custom_word', { id: word.id })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  } else {
    modal.error(res.msg || '删除失败')
  }
}

async function deleteGroup(group: WordGroup) {
  const ok = await modal.confirm('组内识别词将被同步删除，是否确认？', '删除识别词组')
  if (!ok) return
  const res = await doAction<{ code: number; msg?: string }>('delete_custom_word_group', { gid: group.id })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  } else {
    modal.error(res.msg || '删除失败')
  }
}

const groupVisible = ref(false)
const groupForm = reactive({ tmdb_id: '', tmdb_type: 'tv' })

async function addGroup() {
  if (!groupForm.tmdb_id || isNaN(Number(groupForm.tmdb_id))) return modal.warning('请填写有效的 TMDB ID')
  const res = await doAction<{ code: number; msg?: string }>('add_custom_word_group', { ...groupForm })
  if (res.code === 0) {
    modal.success('新增成功')
    groupVisible.value = false
    groupForm.tmdb_id = ''
    load()
  } else {
    modal.error(res.msg || '新增失败')
  }
}

async function batchCheck(flag: 'enable' | 'disable') {
  if (selectedCount.value === 0) return modal.warning('请先选择识别词')
  const ids = Array.from(selectedIds.value)
  const res = await doAction<{ code: number; msg?: string }>('check_custom_words', { flag, ids_info: ids })
  if (res.code === 0) {
    modal.success('操作成功')
    selectedIds.value = new Set()
    load()
  } else {
    modal.error(res.msg || '操作失败')
  }
}

const exportNoteVisible = ref(false)
const exportNote = ref('')
const exportIdsInfo = ref('')
const exportCodeVisible = ref(false)
const exportCode = ref('')

function openExport() {
  if (selectedCount.value === 0) return modal.warning('请先选择识别词')
  exportIdsInfo.value = Array.from(selectedIds.value).join('@')
  exportNote.value = ''
  exportNoteVisible.value = true
}

async function doExport() {
  if (!exportNote.value) return modal.warning('请填写分享备注')
  exportNoteVisible.value = false
  const res = await doAction<{ code: number; string?: string; msg?: string }>('export_custom_words', {
    note: exportNote.value,
    ids_info: exportIdsInfo.value
  })
  if (res.code === 0) {
    exportCode.value = (res as any).string || ''
    exportCodeVisible.value = true
  } else {
    modal.error(res.msg || '导出失败')
  }
}

async function copyExport() {
  try {
    await navigator.clipboard.writeText(exportCode.value)
    modal.success('已复制到剪贴板')
  } catch {
    modal.error('复制失败')
  }
}

const importVisible = ref(false)
const importCode = ref('')

async function doImport() {
  if (!importCode.value) return modal.warning('请粘贴分享代码')
  const res = await doAction<{ code: number; msg?: string }>('import_custom_words', {
    content: importCode.value,
    ids_info: Array.from(selectedIds.value)
  })
  if (res.code === 0) {
    modal.success('导入成功')
    importVisible.value = false
    importCode.value = ''
    selectedIds.value = new Set()
    load()
  } else {
    modal.error(res.msg || '导入失败')
  }
}

function seasonLabel(s: number): string {
  if (s === -1) return '全部季'
  if (s === -2) return ''
  return `第${s}季`
}
</script>

<template>
  <div class="customwords" v-loading="loading">
    <PageHeader title="自定义识别词" description="屏蔽/替换/集偏移规则管理">
      <template #actions>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="success" :icon="VideoPlay" @click="batchCheck('enable')">启用</el-button>
        <el-button :icon="VideoPause" @click="batchCheck('disable')">停用</el-button>
        <el-button :icon="DownloadIcon" @click="openExport">导出</el-button>
        <el-button :icon="UploadIcon" @click="importVisible = true">导入</el-button>
        <el-button type="primary" :icon="Plus" @click="groupVisible = true">新增组</el-button>
      </template>
    </PageHeader>

    <el-empty v-if="!loading && groups.length === 0" description="未配置任何识别词组" />

    <el-card v-for="group in groups" :key="group.id" shadow="never" class="group-card">
      <template #header>
        <div class="group-header">
          <a v-if="group.link" :href="group.link" target="_blank" class="group-name">{{ group.name }}</a>
          <span v-else class="group-name">{{ group.name }}</span>
          <div class="group-actions">
            <el-button :icon="Plus" size="small" link @click="openAdd(group)">新增识别词</el-button>
            <el-button
              v-if="group.id !== -1"
              :icon="Delete"
              size="small"
              type="danger"
              link
              @click="deleteGroup(group)"
            >
              删除组
            </el-button>
          </div>
        </div>
      </template>

      <el-table :data="group.words" stripe empty-text="未配置" row-key="id">
        <el-table-column width="40">
          <template #header>
            <el-checkbox
              :model-value="group.words.length > 0 && group.words.every((w) => selectedIds.has(`${group.id}_${w.id}`))"
              @change="(v: boolean) => selectGroupAll(group, v)"
            />
          </template>
          <template #default="{ row }">
            <el-checkbox
              :model-value="selectedIds.has(`${group.id}_${row.id}`)"
              @change="() => toggleSelect(`${group.id}_${row.id}`)"
            />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="70">
          <template #default="{ row }">
            <el-tag :type="row.enabled === 1 ? 'success' : 'danger'" size="small" effect="dark">
              {{ row.enabled === 1 ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="被替换词" prop="replaced" min-width="140" show-overflow-tooltip />
        <el-table-column label="替换词" prop="replace" min-width="140" show-overflow-tooltip />
        <el-table-column label="偏移集数" prop="offset" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.offset" type="info" size="small" effect="plain">{{ row.offset }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="前定位词" prop="front" min-width="120" show-overflow-tooltip />
        <el-table-column label="后定位词" prop="back" min-width="120" show-overflow-tooltip />
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.regex === 1" size="small">RegEx</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="120">
          <template #default="{ row }">
            <el-tag v-if="seasonLabel(row.season)" type="warning" size="small" effect="plain" style="margin-right: 4px">
              {{ seasonLabel(row.season) }}
            </el-tag>
            <span v-if="row.help" :title="row.help" class="help-mark">?</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button :icon="Edit" size="small" link @click="openEdit(row, group)" />
            <el-button :icon="Delete" size="small" type="danger" link @click="deleteWord(row)" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="editVisible" :title="editTitle" width="720px" destroy-on-close>
      <el-form :model="form" label-width="100px">
        <el-radio-group v-model="form.type" class="type-radio">
          <el-radio :value="1">屏蔽</el-radio>
          <el-radio :value="2">替换</el-radio>
          <el-radio :value="3">替换+集偏移</el-radio>
          <el-radio :value="4">集偏移</el-radio>
        </el-radio-group>

        <el-form-item v-if="form.type !== 4" label="被替换词" required>
          <el-input v-model="form.new_replaced" placeholder="被替换词" />
        </el-form-item>
        <el-form-item v-if="form.type === 2 || form.type === 3" label="替换词" required>
          <el-input v-model="form.new_replace" placeholder="替换词" />
        </el-form-item>

        <template v-if="form.type === 3 || form.type === 4">
          <el-row :gutter="12">
            <el-col :span="8">
              <el-form-item label="前定位词" required>
                <el-input v-model="form.new_front" placeholder="如 第" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="后定位词" required>
                <el-input v-model="form.new_back" placeholder="如 話" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item required>
                <template #label>
                  偏移集数
                  <el-tooltip placement="top">
                    <template #content>
                      <div>1、前定位词/后定位词用于标定集数位置。</div>
                      <div>例如 第{ep}話 中 {ep} 表示集数位置，前定位词填 第、后定位词填 話；如果文件名中没有变化的部分，只标定 {ep} 位置即可。</div>
                      <div style="margin-top: 4px">2、偏移集数：例如定位出的集数是 11，实际是第 1 集，偏移填 -10，以应付多季合集的场景。</div>
                    </template>
                    <el-icon style="cursor: help; margin-left: 4px; color: var(--el-text-color-secondary); font-size: 14px;">
                      <QuestionFilled />
                    </el-icon>
                  </el-tooltip>
                </template>
                <el-input v-model="form.new_offset" placeholder="如 -10" />
              </el-form-item>
            </el-col>
          </el-row>
        </template>

        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="正则表达式">
              <el-select v-model="form.regex" :disabled="form.type === 3 || form.type === 4">
                <el-option :value="1" label="使用" />
                <el-option :value="0" label="不使用" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col v-if="form.group_type === 2" :span="8">
            <el-form-item label="季">
              <el-select v-model="form.season">
                <el-option :value="-1" label="全部" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="状态">
              <el-select v-model="form.enabled">
                <el-option :value="1" label="启用" />
                <el-option :value="0" label="停用" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="备注">
          <el-input v-model="form.new_help" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="submitWord">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="groupVisible" title="新增识别词组" width="480px" destroy-on-close>
      <el-form :model="groupForm" label-width="100px">
        <el-form-item label="TMDB ID" required>
          <el-input v-model="groupForm.tmdb_id" placeholder="TMDB的编号" />
        </el-form-item>
        <el-form-item label="类型" required>
          <el-select v-model="groupForm.tmdb_type">
            <el-option value="movie" label="电影" />
            <el-option value="tv" label="电视剧" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="groupVisible = false">取消</el-button>
        <el-button type="primary" @click="addGroup">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="exportNoteVisible" title="导出自定义识别词" width="600px">
      <el-form label-width="100px">
        <el-form-item label="分享备注" required>
          <el-input v-model="exportNote" type="textarea" :rows="6" placeholder="应用的资源标题/文件名/站点等" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="exportNoteVisible = false">取消</el-button>
        <el-button type="primary" @click="doExport">生成分享代码</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="exportCodeVisible" title="导出自定义识别词" width="640px">
      <pre class="export-code">{{ exportCode }}</pre>
      <template #footer>
        <el-button :icon="DocumentCopy" @click="copyExport">复制</el-button>
        <el-button type="primary" @click="exportCodeVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="importVisible" title="导入自定义识别词" width="640px">
      <el-form label-width="120px">
        <el-form-item label="分享代码" required>
          <el-input v-model="importCode" type="textarea" :rows="8" placeholder="粘贴分享的规则内容" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" @click="doImport">导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.customwords {
  padding: 16px;
}
.group-card {
  margin-bottom: 12px;
}
.group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.group-name {
  font-weight: 600;
  color: var(--el-color-primary);
  text-decoration: none;
}
.group-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}
.help-mark {
  cursor: help;
  color: var(--el-text-color-secondary);
}
.type-radio {
  margin-bottom: 16px;
}
.export-code {
  background-color: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow: auto;
  font-size: 13px;
}
</style>