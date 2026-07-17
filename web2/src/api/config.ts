import { doAction } from './request'

export type AppConfig = Record<string, Record<string, unknown>>

interface GetConfigResult {
  code: number
  msg?: string
  config: AppConfig
}

interface UpdateConfigResult {
  code: number
  msg?: string
}

/** 获取完整配置（需后端 get_config action） */
export function getConfig(): Promise<GetConfigResult> {
  return doAction<GetConfigResult>('get_config', {})
}

/**
 * 更新配置项
 * @param items 形如 { "app.logtype": "console", "media.movie_path": ["/path"] } 的键值对
 * @param test  仅测试不保存
 */
export function updateConfig(items: Record<string, unknown>, test = false): Promise<UpdateConfigResult> {
  const params = { ...items, test }
  return doAction<UpdateConfigResult>('update_config', params)
}

/** 测试连接 */
export function testConnection(command: string): Promise<{ code: number; msg?: string }> {
  return doAction('test_connection', { command })
}

/** 获取系统设置（数据库存储，如 CustomScript / SpeedLimit / CookieCloud） */
export function getSystemConfig(key: string): Promise<{ code: number; value: Record<string, unknown> }> {
  return doAction<{ code: number; value: Record<string, unknown> }>('get_system_config', { key })
}

/** 设置系统设置（数据库存储） */
export function setSystemConfig(key: string, value: unknown): Promise<{ code: number; msg?: string }> {
  return doAction('set_system_config', { key, value })
}

/** 更新目录配置（增删路径） */
export function updateDirectory(oper: 'add' | 'sub', key: string, value: string): Promise<{ code: number }> {
  return doAction('update_directory', { oper, key, value })
}
