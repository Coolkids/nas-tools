<script setup lang="ts">
import { onMounted } from 'vue'
import { useRoute } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()
onMounted(() => {
  appStore.init()
})
</script>

<template>
  <RouterView v-if="route.meta?.public" />
  <DefaultLayout v-else />
</template>