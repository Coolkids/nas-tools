<script setup lang="ts">
import { useModalStore } from '@/stores/modal'

const modal = useModalStore()
</script>

<template>
  <!-- 全屏等待遮罩（替代旧 show_wait_modal） -->
  <div v-if="modal.loading" class="global-loading">
    <el-icon class="is-loading" :size="28"><Loading /></el-icon>
    <span>{{ modal.loadingText }}</span>
  </div>

  <!-- 进度对话框（替代旧进度框） -->
  <el-dialog
    v-model="modal.progressVisible"
    title="进度"
    width="420px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    align-center
  >
    <div style="padding: 8px 0">
      <el-progress :percentage="modal.progressValue" :stroke-width="14" />
      <div v-if="modal.progressText" style="margin-top: 12px; color: var(--el-text-color-secondary)">
        {{ modal.progressText }}
      </div>
    </div>
  </el-dialog>
</template>
