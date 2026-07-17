import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { setNeedsLoginHandler } from '@/api/request'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/index' },

  // 登录页（免登录）
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true }
  },

  // 顶层页面
  {
    path: '/index',
    name: 'home',
    component: () => import('@/views/HomeView.vue'),
    meta: { title: '首页' }
  },
  {
    path: '/search',
    name: 'search',
    component: () => import('@/views/SearchView.vue'),
    meta: { title: '资源搜索' }
  },
  {
    path: '/service',
    name: 'service',
    component: () => import('@/views/ServiceView.vue'),
    meta: { title: '服务' }
  },

  // 发现
  {
    path: '/recommend',
    name: 'recommend',
    component: () => import('@/views/discovery/RecommendView.vue'),
    meta: { title: '推荐' }
  },
  {
    path: '/ranking',
    name: 'ranking',
    component: () => import('@/views/discovery/RankingView.vue'),
    meta: { title: '榜单' }
  },
  {
    path: '/media_detail',
    name: 'media_detail',
    component: () => import('@/views/discovery/MediaDetailView.vue'),
    meta: { title: '媒体详情' }
  },
  {
    path: '/discovery_person',
    name: 'discovery_person',
    component: () => import('@/views/discovery/PersonView.vue'),
    meta: { title: '人物' }
  },
  {
    path: '/douban_movie',
    name: 'douban_movie',
    component: () => import('@/views/discovery/RecommendView.vue'),
    meta: { title: '豆瓣电影' }
  },
  {
    path: '/douban_tv',
    name: 'douban_tv',
    component: () => import('@/views/discovery/RecommendView.vue'),
    meta: { title: '豆瓣剧集' }
  },
  {
    path: '/tmdb_movie',
    name: 'tmdb_movie',
    component: () => import('@/views/discovery/RecommendView.vue'),
    meta: { title: 'TMDB电影' }
  },
  {
    path: '/tmdb_tv',
    name: 'tmdb_tv',
    component: () => import('@/views/discovery/RecommendView.vue'),
    meta: { title: 'TMDB剧集' }
  },
  {
    path: '/bangumi',
    name: 'bangumi',
    component: () => import('@/views/discovery/RankingView.vue'),
    meta: { title: 'Bangumi' }
  },
  {
    path: '/downloaded',
    name: 'downloaded',
    component: () => import('@/views/discovery/RecommendView.vue'),
    meta: { title: '最近下载' }
  },

  // 订阅
  {
    path: '/movie_rss',
    name: 'movie_rss',
    component: () => import('@/views/rss/MovieRssView.vue'),
    meta: { title: '电影订阅' }
  },
  {
    path: '/tv_rss',
    name: 'tv_rss',
    component: () => import('@/views/rss/TvRssView.vue'),
    meta: { title: '电视剧订阅' }
  },
  {
    path: '/rss_history',
    name: 'rss_history',
    component: () => import('@/views/rss/RssHistoryView.vue'),
    meta: { title: '订阅历史' }
  },
  {
    path: '/rss_calendar',
    name: 'rss_calendar',
    component: () => import('@/views/rss/RssCalendarView.vue'),
    meta: { title: '订阅日历' }
  },
  {
    path: '/user_rss',
    name: 'user_rss',
    component: () => import('@/views/rss/UserRssView.vue'),
    meta: { title: '自定义订阅' }
  },
  {
    path: '/rss_parser',
    name: 'rss_parser',
    component: () => import('@/views/rss/RssParserView.vue'),
    meta: { title: 'RSS解析器' }
  },

  // 下载
  {
    path: '/downloading',
    name: 'downloading',
    component: () => import('@/views/download/DownloadingView.vue'),
    meta: { title: '正在下载' }
  },
  {
    path: '/torrent_remove',
    name: 'torrent_remove',
    component: () => import('@/views/download/TorrentRemoveView.vue'),
    meta: { title: '自动删种' }
  },
  {
    path: '/userdownloader',
    name: 'userdownloader',
    component: () => import('@/views/download/UserDownloaderView.vue'),
    meta: { title: '自定义下载器' }
  },

  // 媒体整理
  {
    path: '/history',
    name: 'history',
    component: () => import('@/views/rename/HistoryView.vue'),
    meta: { title: '转移历史' }
  },
  {
    path: '/tmdbcache',
    name: 'tmdbcache',
    component: () => import('@/views/rename/TmdbCacheView.vue'),
    meta: { title: 'TMDB缓存' }
  },
  {
    path: '/unidentification',
    name: 'unidentification',
    component: () => import('@/views/rename/UnidentificationView.vue'),
    meta: { title: '手动识别' }
  },
  {
    path: '/mediafile',
    name: 'mediafile',
    component: () => import('@/views/rename/MediaFileView.vue'),
    meta: { title: '文件管理' }
  },

  // 系统设置
  {
    path: '/basic',
    name: 'basic',
    component: () => import('@/views/setting/BasicView.vue'),
    meta: { title: '基础设置' }
  },
  {
    path: '/customwords',
    name: 'customwords',
    component: () => import('@/views/setting/CustomWordsView.vue'),
    meta: { title: '自定义识别词' }
  },
  {
    path: '/directorysync',
    name: 'directorysync',
    component: () => import('@/views/setting/DirectorySyncView.vue'),
    meta: { title: '目录同步' }
  },
  {
    path: '/douban',
    name: 'douban',
    component: () => import('@/views/setting/DoubanView.vue'),
    meta: { title: '豆瓣同步' }
  },
  {
    path: '/downloader',
    name: 'downloader',
    component: () => import('@/views/setting/DownloaderView.vue'),
    meta: { title: '下载器' }
  },
  {
    path: '/download_setting',
    name: 'download_setting',
    component: () => import('@/views/setting/DownloadSettingView.vue'),
    meta: { title: '下载设置' }
  },
  {
    path: '/indexer',
    name: 'indexer',
    component: () => import('@/views/setting/IndexerView.vue'),
    meta: { title: '索引器' }
  },
  {
    path: '/library',
    name: 'library',
    component: () => import('@/views/setting/LibraryView.vue'),
    meta: { title: '媒体库' }
  },
  {
    path: '/mediaserver',
    name: 'mediaserver',
    component: () => import('@/views/setting/MediaServerView.vue'),
    meta: { title: '媒体服务器' }
  },
  {
    path: '/notification',
    name: 'notification',
    component: () => import('@/views/setting/NotificationView.vue'),
    meta: { title: '消息通知' }
  },
  {
    path: '/subtitle',
    name: 'subtitle',
    component: () => import('@/views/setting/SubtitleView.vue'),
    meta: { title: '字幕' }
  },
  {
    path: '/users',
    name: 'users',
    component: () => import('@/views/setting/UsersView.vue'),
    meta: { title: '用户管理' }
  },
  {
    path: '/filterrule',
    name: 'filterrule',
    component: () => import('@/views/setting/FilterRuleView.vue'),
    meta: { title: '过滤规则' }
  },

  // 404
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在' }
  }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior() {
    return { top: 0 }
  }
})

router.afterEach((to) => {
  const title = (to.meta?.title as string) || ''
  document.title = title ? `${title} - NAStool` : 'NAStool'
})

let authChecked = false
router.beforeEach(async (to) => {
  if (to.meta?.public) return true
  if (authChecked) return true
  const { checkAuth } = await import('@/api/auth')
  authChecked = true
  const ok = await checkAuth()
  if (ok) return true
  return { path: '/login', query: { redirect: to.fullPath } }
})

setNeedsLoginHandler(() => {
  authChecked = false
  const next = window.location.pathname + window.location.search
  router.push({ path: '/login', query: { redirect: next } })
})

export default router
