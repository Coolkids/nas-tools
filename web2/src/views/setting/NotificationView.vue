<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Plus, Edit, Delete, Promotion, Connection } from '@element-plus/icons-vue'
import { doAction } from '@/api'
import { useModalStore } from '@/stores/modal'
import PageHeader from '@/components/PageHeader.vue'

interface FieldDef {
  id: string
  required?: boolean
  title: string
  tooltip?: string
  type: 'text' | 'password' | 'switch' | 'select'
  placeholder?: string
  default?: string
  options?: Record<string, string>
}
interface ChannelDef {
  id: string
  name: string
  search_type?: boolean
  config: Record<string, FieldDef>
}
interface MessageClient {
  id: number
  name: string
  type: string
  config: Record<string, unknown>
  switchs: string[]
  interactive: number
  enabled: number
}

const CHANNELS: ChannelDef[] = [
  {
    id: 'telegram', name: 'Telegram', search_type: true,
    config: {
      token: { id: 'telegram_token', required: true, title: 'Bot Token', tooltip: 'telegram机器人的Token', type: 'text' },
      chat_id: { id: 'telegram_chat_id', required: true, title: 'Chat ID', tooltip: '接受消息通知的Chat ID', type: 'text' },
      user_ids: { id: 'telegram_user_ids', required: false, title: 'User IDs', tooltip: '允许交互的用户Chat ID', type: 'text', placeholder: '使用,分隔多个Id' },
      admin_ids: { id: 'telegram_admin_ids', required: false, title: 'Admin IDs', tooltip: '允许管理命令的用户Chat ID', type: 'text', placeholder: '使用,分隔多个Id' },
      webhook: { id: 'telegram_webhook', required: false, title: 'Webhook', tooltip: '开启后使用Webhook方式', type: 'switch' }
    }
  },
  {
    id: 'wechat', name: '微信', search_type: true,
    config: {
      corpid: { id: 'wechat_corpid', required: true, title: '企业ID', type: 'text' },
      corpsecret: { id: 'wechat_corpsecret', required: true, title: '应用Secret', type: 'text', placeholder: 'Secret' },
      agentid: { id: 'wechat_agentid', required: true, title: '应用ID', type: 'text', placeholder: 'AgentId' },
      default_proxy: { id: 'wechat_default_proxy', required: false, title: '消息推送代理', type: 'text', placeholder: 'https://wechat.nastool.cn' },
      token: { id: 'wechat_token', required: false, title: 'Token', type: 'text', placeholder: 'API接收消息Token' },
      encodingAESKey: { id: 'wechat_encodingAESKey', required: false, title: 'EncodingAESKey', type: 'text' }
    }
  },
  {
    id: 'serverchan', name: 'Server酱',
    config: {
      sckey: { id: 'serverchan_sckey', required: true, title: 'SCKEY', type: 'text', placeholder: 'SCT...' }
    }
  },
  {
    id: 'bark', name: 'Bark',
    config: {
      server: { id: 'bark_server', required: true, title: 'Bark服务器地址', type: 'text', placeholder: 'https://api.day.app', default: 'https://api.day.app' },
      apikey: { id: 'bark_apikey', required: true, title: 'API Key', type: 'text' },
      params: { id: 'bark_params', required: false, title: '附加参数', type: 'text', placeholder: 'group=xxx&sound=xxx&url=xxx' }
    }
  },
  {
    id: 'pushdeer', name: 'PushDeer',
    config: {
      server: { id: 'pushdeer_server', required: true, title: 'PushDeer服务器地址', type: 'text', placeholder: 'https://api2.pushdeer.com', default: 'https://api2.pushdeer.com' },
      apikey: { id: 'pushdeer_apikey', required: true, title: 'API Key', type: 'text' }
    }
  },
  {
    id: 'pushplus', name: 'PushPlus',
    config: {
      token: { id: 'pushplus_token', required: true, title: 'Token', type: 'text' },
      channel: { id: 'pushplus_channel', required: true, title: '推送渠道', type: 'select', options: { wechat: '微信', mail: '邮箱', webhook: '第三方Webhook' }, default: 'wechat' },
      topic: { id: 'pushplus_topic', required: false, title: '群组编码', type: 'text' },
      webhook: { id: 'pushplus_webhook', required: false, title: 'Webhook编码', type: 'text' }
    }
  },
  {
    id: 'iyuu', name: '爱语飞飞',
    config: {
      token: { id: 'iyuumsg_token', required: true, title: '令牌Token', type: 'text', placeholder: '登录https://iyuu.cn获取' }
    }
  },
  {
    id: 'slack', name: 'Slack', search_type: true,
    config: {
      bot_token: { id: 'slack_bot_token', required: true, title: 'Bot User OAuth Token', type: 'text', placeholder: 'xoxb-...' },
      app_token: { id: 'slack_app_token', required: true, title: 'App-Level Token', type: 'text', placeholder: 'xapp-...' },
      channel: { id: 'slack_channel', required: false, title: '频道名称', type: 'text', placeholder: '全体' }
    }
  },
  {
    id: 'gotify', name: 'Gotify',
    config: {
      server: { id: 'gotify_server', required: true, title: 'Gotify服务器地址', type: 'text', placeholder: 'http://localhost:8800' },
      token: { id: 'gotify_token', required: true, title: '令牌Token', type: 'text' },
      priority: { id: 'gotify_priority', required: false, title: '消息Priority', type: 'text', placeholder: '8' }
    }
  },
  {
    id: 'chanify', name: 'Chanify',
    config: {
      server: { id: 'chanify_server', required: true, title: 'Chanify服务器地址', type: 'text', placeholder: 'https://api.chanify.net', default: 'https://api.chanify.net' },
      token: { id: 'chanify_token', required: true, title: '令牌', type: 'text' }
    }
  },
  {
    id: 'synologychat', name: 'Synology Chat', search_type: true,
    config: {
      webhook_url: { id: 'synologychat_webhook_url', required: true, title: '机器人传入URL', type: 'text' },
      token: { id: 'synologychat_token', required: true, title: '令牌', type: 'text' }
    }
  }
]

const SWITCHS = [
  { id: 'download_start', name: '新增下载' },
  { id: 'download_fail', name: '下载失败' },
  { id: 'transfer_finished', name: '入库完成' },
  { id: 'transfer_fail', name: '入库失败' },
  { id: 'rss_added', name: '新增订阅' },
  { id: 'rss_finished', name: '订阅完成' },
  { id: 'site_signin', name: '站点签到' },
  { id: 'site_message', name: '站点消息' },
  { id: 'brushtask_added', name: '刷流下种' },
  { id: 'brushtask_remove', name: '刷流删种' },
  { id: 'mediaserver_message', name: '媒体服务' },
  { id: 'custom_message', name: '自定义消息' }
]

const modal = useModalStore()
const list = ref<MessageClient[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const saving = ref(false)
const testing = ref(false)
const form = reactive({
  cid: '' as string | number,
  name: '',
  type: CHANNELS[0].id,
  enabled: 1,
  interactive: 1,
  switchs: SWITCHS.map((s) => s.id),
  config: {} as Record<string, unknown>
})

const customVisible = ref(false)
const customSending = ref(false)
const customForm = reactive({ title: '', image: '', text: '' })

const currentChannel = computed(() => CHANNELS.find((c) => c.id === form.type) || CHANNELS[0])

onMounted(load)

async function load() {
  loading.value = true
  try {
    const res = await doAction<{ code: number; detail: Record<string, MessageClient> }>('get_message_client', {})
    if (res.code === 0 && res.detail) {
      list.value = Object.values(res.detail)
    }
  } finally {
    loading.value = false
  }
}

function channelName(type: string) {
  return CHANNELS.find((c) => c.id === type)?.name || type
}

function switchNames(switchs: string[]) {
  return switchs.map((s) => SWITCHS.find((sw) => sw.id === s)?.name || s)
}

function resetConfigFields() {
  const cfg: Record<string, unknown> = {}
  for (const [key, f] of Object.entries(currentChannel.value.config)) {
    if (f.type === 'switch') cfg[key] = !!f.default
    else cfg[key] = f.default || ''
  }
  form.config = cfg
}

function openAdd() {
  form.cid = ''
  form.name = ''
  form.type = CHANNELS[0].id
  form.enabled = 1
  form.interactive = 1
  form.switchs = SWITCHS.map((s) => s.id)
  resetConfigFields()
  dialogVisible.value = true
}

function openEdit(row: MessageClient) {
  form.cid = row.id
  form.name = row.name
  form.type = row.type
  form.enabled = row.enabled
  form.interactive = row.interactive
  form.switchs = row.switchs && row.switchs.length ? [...row.switchs] : SWITCHS.map((s) => s.id)
  const cfg: Record<string, unknown> = {}
  const ch = CHANNELS.find((c) => c.id === row.type)
  if (ch) {
    for (const [key, f] of Object.entries(ch.config)) {
      const v = row.config?.[key]
      if (f.type === 'switch') cfg[key] = !!v
      else cfg[key] = v ?? f.default ?? ''
    }
  }
  form.config = cfg
  dialogVisible.value = true
}

function onTypeChange() {
  resetConfigFields()
}

function buildParams() {
  const configObj: Record<string, unknown> = {}
  for (const [key, f] of Object.entries(currentChannel.value.config)) {
    if (f.type === 'switch') configObj[key] = form.config[key] ? 1 : 0
    else configObj[key] = form.config[key] ?? ''
  }
  return {
    cid: form.cid,
    name: form.name,
    type: form.type,
    config: JSON.stringify(configObj),
    switchs: form.switchs,
    enabled: form.enabled,
    interactive: form.interactive
  }
}

async function submit() {
  if (!form.name) {
    modal.warning('名称不能为空')
    return
  }
  for (const [, f] of Object.entries(currentChannel.value.config)) {
    if (f.required && f.type !== 'switch' && !form.config[f.id.replace(/^[a-z]+_/, '')]) {
      const key = Object.entries(currentChannel.value.config).find(([, v]) => v.id === f.id)?.[0]
      if (key && !form.config[key]) {
        modal.warning(`${f.title}不能为空`)
        return
      }
    }
  }
  saving.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('update_message_client', buildParams())
    if (res.code === 0) {
      dialogVisible.value = false
      modal.success('保存成功')
      load()
    } else {
      modal.error(res.msg || '保存失败')
    }
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('test_message_client', buildParams())
    if (res.code === 0) modal.success('测试成功')
    else modal.error('测试失败')
  } finally {
    testing.value = false
  }
}

async function toggle(row: MessageClient, flag: 'interactive' | 'enable', checked: boolean) {
  const res = await doAction<{ code: number }>('check_message_client', {
    flag, cid: row.id, checked, type: row.type
  })
  if (res.code === 0) {
    if (flag === 'interactive') row.interactive = checked ? 1 : 0
    else row.enabled = checked ? 1 : 0
    load()
  }
}

async function remove(row: MessageClient) {
  const ok = await modal.confirm(`确认删除消息服务「${row.name}」？`)
  if (!ok) return
  const res = await doAction<{ code: number }>('delete_message_client', { cid: row.id })
  if (res.code === 0) {
    modal.success('删除成功')
    load()
  }
}

function openCustom() {
  customForm.title = ''
  customForm.image = ''
  customForm.text = ''
  customVisible.value = true
}

async function sendCustom() {
  if (!customForm.title) {
    modal.warning('标题不能为空')
    return
  }
  customSending.value = true
  try {
    const res = await doAction<{ code: number; msg?: string }>('send_custom_message', {
      title: customForm.title,
      text: customForm.text,
      image: customForm.image
    })
    if (res.code === 0) {
      modal.success('自定义消息已发送')
      customVisible.value = false
    }
  } finally {
    customSending.value = false
  }
}
</script>

<template>
  <div class="notification-view" v-loading="loading">
    <PageHeader title="消息通知" description="管理消息推送渠道与通知开关">
      <template #actions>
        <el-button :icon="Promotion" @click="openCustom">发送自定义消息</el-button>
        <el-button type="primary" :icon="Plus" @click="openAdd">新增消息通知</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <el-table :data="list" stripe>
        <el-table-column label="类型" width="130">
          <template #default="{ row }">
            <el-tag size="small">{{ channelName(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="名称" prop="name" min-width="140" />
        <el-table-column label="推送内容" min-width="280">
          <template #default="{ row }">
            <el-tag
              v-for="s in switchNames(row.switchs)"
              :key="s"
              size="small"
              type="info"
              class="switch-tag"
            >{{ s }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="交互" width="80" align="center">
          <template #default="{ row }">
            <el-switch
              v-if="CHANNELS.find((c) => c.id === row.type)?.search_type"
              :model-value="row.interactive === 1"
              @change="(v: boolean) => toggle(row, 'interactive', v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled === 1"
              @change="(v: boolean) => toggle(row, 'enable', v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" align="center">
          <template #default="{ row }">
            <el-button :icon="Edit" link @click="openEdit(row)">编辑</el-button>
            <el-button :icon="Delete" link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="form.cid ? '编辑消息通知' : '新增消息通知'"
      width="760px"
      :close-on-click-modal="false"
    >
      <el-form label-width="160px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="别名" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="状态">
              <el-select v-model="form.enabled">
                <el-option :value="1" label="启用" />
                <el-option :value="0" label="停用" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6" v-if="currentChannel.search_type">
            <el-form-item label="交互">
              <el-select v-model="form.interactive">
                <el-option :value="1" label="是" />
                <el-option :value="0" label="否" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="类型" required>
          <el-radio-group v-model="form.type" @change="onTypeChange">
            <el-radio-button v-for="c in CHANNELS" :key="c.id" :value="c.id">{{ c.name }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-row :gutter="12">
          <el-col
            v-for="[key, f] in Object.entries(currentChannel.config)"
            :key="f.id"
            :span="f.type === 'switch' ? 6 : 12"
          >
            <el-form-item :label="f.title" :required="f.required">
              <el-switch v-if="f.type === 'switch'" v-model="form.config[key]" />
              <el-select v-else-if="f.type === 'select'" v-model="form.config[key]">
                <el-option v-for="(lbl, val) in f.options" :key="val" :value="val" :label="lbl" />
              </el-select>
              <el-input
                v-else
                v-model="form.config[key]"
                :type="f.type === 'password' ? 'password' : 'text'"
                :show-password="f.type === 'password'"
                :placeholder="f.placeholder"
              />
            </el-form-item>
          </el-col>
        </el-row>
        <el-divider content-position="left">推送设置</el-divider>
        <el-checkbox-group v-model="form.switchs" class="switch-group">
          <el-checkbox v-for="s in SWITCHS" :key="s.id" :value="s.id">{{ s.name }}</el-checkbox>
        </el-checkbox-group>
      </el-form>
      <template #footer>
        <el-button :icon="Connection" :loading="testing" @click="test">测试</el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="customVisible" title="发送自定义消息" width="560px" :close-on-click-modal="false">
      <el-form label-width="80px">
        <el-form-item label="标题" required>
          <el-input v-model="customForm.title" />
        </el-form-item>
        <el-form-item label="图片">
          <el-input v-model="customForm.image" placeholder="url" />
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="customForm.text" type="textarea" :rows="4" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="customVisible = false">取消</el-button>
        <el-button type="primary" :loading="customSending" @click="sendCustom">发送</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.notification-view {
  padding: 16px;
}
.switch-tag {
  margin-right: 4px;
  margin-bottom: 4px;
}
.switch-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
}
</style>
