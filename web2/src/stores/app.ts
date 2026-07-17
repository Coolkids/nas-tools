import { defineStore } from 'pinia'

interface AppState {
  initialized: boolean
  username: string
  version: string
}

export const useAppStore = defineStore('app', {
  state: (): AppState => ({
    initialized: false,
    username: '',
    version: ''
  }),
  actions: {
    /**
     * 应用初始化。P0 阶段仅标记初始化完成；登录态校验由各页面调用 /do 时
     * 通过 request.ts 的拦截器自动处理（未登录会重定向到 Flask 登录页）。
     */
    init() {
      this.initialized = true
    },
    setUsername(name: string) {
      this.username = name
    }
  }
})
