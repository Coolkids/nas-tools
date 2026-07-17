<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const activeMenu = computed(() => route.path)
</script>

<template>
  <el-menu
    :default-active="activeMenu"
    router
    class="app-sidebar-menu"
    background-color="transparent"
  >
    <el-menu-item index="/index">
      <el-icon><HomeFilled /></el-icon>
      <span>首页</span>
    </el-menu-item>

    <el-sub-menu index="discovery">
      <template #title>
        <el-icon><Compass /></el-icon>
        <span>探索</span>
      </template>
      <el-menu-item index="/recommend">推荐</el-menu-item>
      <el-menu-item index="/ranking">榜单</el-menu-item>
    </el-sub-menu>

    <el-menu-item index="/search">
      <el-icon><Search /></el-icon>
      <span>资源搜索</span>
    </el-menu-item>

    <el-sub-menu index="rss">
      <template #title>
        <el-icon><Bell /></el-icon>
        <span>订阅管理</span>
      </template>
      <el-menu-item index="/movie_rss">电影订阅</el-menu-item>
      <el-menu-item index="/tv_rss">电视剧订阅</el-menu-item>
      <el-menu-item index="/user_rss">自定义订阅</el-menu-item>
      <el-menu-item index="/rss_history">订阅历史</el-menu-item>
      <el-menu-item index="/rss_calendar">订阅日历</el-menu-item>
      <el-menu-item index="/rss_parser">RSS解析器</el-menu-item>
    </el-sub-menu>

    <el-sub-menu index="download">
      <template #title>
        <el-icon><Download /></el-icon>
        <span>下载管理</span>
      </template>
      <el-menu-item index="/downloading">正在下载</el-menu-item>
      <el-menu-item index="/downloaded">最近下载</el-menu-item>
      <el-menu-item index="/torrent_remove">自动删种</el-menu-item>
      <el-menu-item index="/userdownloader">自定义下载器</el-menu-item>
    </el-sub-menu>

    <el-sub-menu index="rename">
      <template #title>
        <el-icon><Files /></el-icon>
        <span>媒体整理</span>
      </template>
      <el-menu-item index="/mediafile">文件管理</el-menu-item>
      <el-menu-item index="/history">转移历史</el-menu-item>
      <el-menu-item index="/unidentification">手动识别</el-menu-item>
      <el-menu-item index="/tmdbcache">TMDB缓存</el-menu-item>
    </el-sub-menu>

    <el-menu-item index="/service">
      <el-icon><Setting /></el-icon>
      <span>服务</span>
    </el-menu-item>

    <el-sub-menu index="setting">
      <template #title>
        <el-icon><Tools /></el-icon>
        <span>系统设置</span>
      </template>
      <el-menu-item index="/basic">基础设置</el-menu-item>
      <el-menu-item index="/downloader">下载器</el-menu-item>
      <el-menu-item index="/download_setting">下载设置</el-menu-item>
      <el-menu-item index="/directorysync">目录同步</el-menu-item>
      <el-menu-item index="/indexer">索引器</el-menu-item>
      <el-menu-item index="/mediaserver">媒体服务器</el-menu-item>
      <el-menu-item index="/library">媒体库</el-menu-item>
      <el-menu-item index="/notification">消息通知</el-menu-item>
      <el-menu-item index="/subtitle">字幕</el-menu-item>
      <el-menu-item index="/douban">豆瓣同步</el-menu-item>
      <el-menu-item index="/customwords">自定义识别词</el-menu-item>
      <el-menu-item index="/filterrule">过滤规则</el-menu-item>
      <el-menu-item index="/users">用户管理</el-menu-item>
    </el-sub-menu>
  </el-menu>
</template>

<style scoped>
.app-sidebar-menu {
  border-right: none;
  height: 100%;
}
</style>
