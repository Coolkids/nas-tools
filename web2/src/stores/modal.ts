import { defineStore } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'

interface ModalState {
  /** 全屏等待遮罩 */
  loading: boolean
  loadingText: string
  /** 进度对话框 */
  progressVisible: boolean
  progressValue: number
  progressText: string
}

/**
 * 全局弹窗/遮罩状态管理。
 *
 * 用于替代旧 navigation.html 中的全局 Modal：
 * - show_wait_modal   → showLoading
 * - show_success_modal → success
 * - show_fail_modal    → error
 * - show_confirm_modal → confirm
 * - 进度框             → showProgress / setProgress / hideProgress
 */
export const useModalStore = defineStore('modal', {
  state: (): ModalState => ({
    loading: false,
    loadingText: '',
    progressVisible: false,
    progressValue: 0,
    progressText: ''
  }),
  actions: {
    showLoading(text = '处理中...') {
      this.loading = true
      this.loadingText = text
    },
    hideLoading() {
      this.loading = false
      this.loadingText = ''
    },
    showProgress(text = '') {
      this.progressVisible = true
      this.progressValue = 0
      this.progressText = text
    },
    setProgress(value: number, text?: string) {
      this.progressValue = Math.max(0, Math.min(100, value))
      if (text !== undefined) this.progressText = text
    },
    hideProgress() {
      this.progressVisible = false
      this.progressValue = 0
      this.progressText = ''
    },
    success(msg: string) {
      ElMessage.success(msg)
    },
    error(msg: string) {
      ElMessage.error(msg)
    },
    warning(msg: string) {
      ElMessage.warning(msg)
    },
    info(msg: string) {
      ElMessage.info(msg)
    },
    async confirm(msg: string, title = '确认操作'): Promise<boolean> {
      try {
        await ElMessageBox.confirm(msg, title, {
          confirmButtonText: '确定',
          cancelButtonText: '取消',
          type: 'warning'
        })
        return true
      } catch {
        return false
      }
    },
    async alert(msg: string, title = '提示'): Promise<void> {
      await ElMessageBox.alert(msg, title, { type: 'info' })
    }
  }
})
