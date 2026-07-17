import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'
import NProgress from 'nprogress'

/**
 * Flask 后端基地址。
 * - 开发期：通过 .env.development 配置为 http://localhost:3000
 * - 生产期：与前端同源，留空即可
 */
const FLASK_BASE = import.meta.env.VITE_FLASK_BASE || ''

const instance: AxiosInstance = axios.create({
  baseURL: FLASK_BASE,
  timeout: 0,
  withCredentials: true
})

instance.interceptors.request.use((config) => {
  NProgress.start()
  return config
})

instance.interceptors.response.use(
  (response) => {
    NProgress.done()
    return response
  },
  (error) => {
    NProgress.done()
    return Promise.reject(error)
  }
)

/** /do 端点返回的原始结构（未经过 api_action 包装） */
export interface DoResult {
  code?: number
  msg?: string
  retcode?: number
  retmsg?: string
  [key: string]: unknown
}

let needsLoginHandler: (() => void) | null = null

/** 注入未登录时的跳转回调（由 router 守卫设置，避免在此处直接 import router 造成循环依赖） */
export function setNeedsLoginHandler(fn: () => void): void {
  needsLoginHandler = fn
}

function redirectToLogin(): void {
  if (needsLoginHandler) {
    needsLoginHandler()
  } else {
    const next = window.location.pathname + window.location.search
    window.location.href = `/login?redirect=${encodeURIComponent(next)}`
  }
}

/**
 * 调用后端 /do 端点（等价于旧 util.js 的 ajax_post）。
 *
 * @param cmd    action.py 中注册的命令名
 * @param params 业务参数对象，会被 JSON.stringify 后放进 data 字段
 * @returns      后端返回的原始 JSON 对象
 */
export async function doAction<T = DoResult>(cmd: string, params: object = {}): Promise<T> {
  const form = new URLSearchParams()
  form.append('cmd', cmd)
  form.append('data', JSON.stringify(params))
  const resp = await instance.post<T>(`/do?random=${Math.random()}`, form, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
  })
  const result = resp.data as unknown as DoResult
  if (result && result.code === -1 && result.msg === '用户未登录') {
    redirectToLogin()
    throw new Error('未登录')
  }
  return resp.data
}

/** GET 请求（用于 REST API /api/v1/* 等） */
export async function getJson<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const resp = await instance.get<T>(url, config)
  return resp.data
}

/** POST 请求（用于 REST API /api/v1/* 等） */
export async function postJson<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  const resp = await instance.post<T>(url, data, config)
  return resp.data
}

export default instance
