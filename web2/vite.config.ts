import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时后端 Flask 地址（run.py 默认 3000 端口）
const FLASK_TARGET = process.env.FLASK_TARGET || 'http://localhost:3000'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  base: '/',
  css: {
    preprocessorOptions: {
      scss: { api: 'modern-compiler' }
    }
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    // 开发期把后端接口代理到 Flask，同源请求，cookie 跨端口共享（浏览器 cookie 不区分端口）
    proxy: {
      '/do': { target: FLASK_TARGET, changeOrigin: true },
      '/login_json': { target: FLASK_TARGET, changeOrigin: true },
      '/logout_json': { target: FLASK_TARGET, changeOrigin: true },
      '/dirlist': { target: FLASK_TARGET, changeOrigin: true },
      '/api/v1': { target: FLASK_TARGET, changeOrigin: true },
      '/img': { target: FLASK_TARGET, changeOrigin: true },
      '/backup': { target: FLASK_TARGET, changeOrigin: true },
      '/upload': { target: FLASK_TARGET, changeOrigin: true },
      '/static': { target: FLASK_TARGET, changeOrigin: true },
      '/wechat': { target: FLASK_TARGET, changeOrigin: true },
      '/plex': { target: FLASK_TARGET, changeOrigin: true },
      '/jellyfin': { target: FLASK_TARGET, changeOrigin: true },
      '/emby': { target: FLASK_TARGET, changeOrigin: true },
      '/telegram': { target: FLASK_TARGET, changeOrigin: true },
      '/subscribe': { target: FLASK_TARGET, changeOrigin: true }
    }
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1500
  }
})
