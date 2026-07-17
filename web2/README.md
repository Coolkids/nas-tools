# NAStool Web2 (Vue3 前端)

基于 **Vue 3 + Element Plus + Vite + TypeScript** 的全新前端，前后端分离架构。

> 本目录与旧 `web/` 完全独立，旧前端代码保持不动，可并行运行、渐进迁移。

## 技术栈

| 能力 | 选型 |
|------|------|
| 框架 | Vue 3 (`<script setup lang="ts">`) |
| 构建 | Vite 5 |
| UI | Element Plus 2 |
| 路由 | Vue Router 4 |
| 状态 | Pinia |
| HTTP | Axios |
| 类型 | TypeScript 5 + vue-tsc |

## 架构：前后端分离

```
浏览器 ──→ nginx:80 (生产) / Vite:5173 (开发)
              │  静态资源：web2/dist
              │  反向代理 /do /login_json /api/v1 /img ... →
              └──→ Flask:3000 (后端)
```

- **开发模式**：Vite dev server 自带反向代理，把后端接口转发到 Flask :3000，同源请求，无 CORS 问题。
- **生产模式**：nginx 托管 `web2/dist` 静态文件，反向代理 API 到 Flask（见 `nginx.conf`）。

## 目录结构

```
web2/
├── index.html                # Vite 入口
├── vite.config.ts            # 构建 + 开发反向代理
├── nginx.conf                # 生产 nginx 配置示例
├── .env.development          # 开发环境变量（FLASK_BASE 留空，走代理）
├── .env.production           # 生产环境变量（同源，留空）
├── src/
│   ├── main.ts               # 入口
│   ├── App.vue               # 根据 route.meta.public 切换登录页/主布局
│   ├── router/index.ts       # 全部路由 + 登录守卫
│   ├── api/                  # Axios 封装 + 各业务 API 模块
│   │   ├── request.ts        # doAction()，未登录跳 /login
│   │   ├── auth.ts           # login / logout / checkAuth
│   │   ├── config.ts / site.ts / rss.ts / download.ts
│   │   ├── rename.ts / discovery.ts / media.ts / system.ts / brush.ts
│   ├── stores/               # Pinia（app / modal）
│   ├── components/           # 通用组件（MediaCard/PersonCard/PageHeader...）
│   ├── composables/          # useConfigForm 等
│   ├── layouts/DefaultLayout.vue
│   └── views/                # 40 个页面（对应旧 templates/）
└── dist/                     # 构建产物（git 忽略）
```

## 开发流程

### 1. 启动后端 Flask

在项目根目录：

```bash
export NASTOOL_CONFIG=./config/config.yaml
.venv/bin/python run.py
# Flask 监听 :3000
```

> 前后端分离所需的后端新增接口（均非破坏性，旧前端不受影响）：
> - `/login_json` / `/logout_json`：JSON 登录/登出，设置 session cookie
> - `/do` 新增 `get_config` / `get_tmdb_cache` 两个 action
> - 全局 CORS 头（`after_request`）

### 2. 启动前端 Dev Server

在 `web2/` 目录：

```bash
pnpm install     # 首次
pnpm dev         # Vite :5173，自动反向代理 /do /login_json /api/v1 到 :3000
```

打开 `http://localhost:5173/`，自动跳转到 Vue 登录页（admin / password）。

### 3. 登录流程

- 新前端自带 Vue 登录页（`/login`），表单提交到 `/login_json`（经 Vite 代理同源请求）。
- 登录成功后设置 Flask session cookie，后续 `/do` 请求自动带 cookie。
- 未登录时路由守卫拦截，重定向到 `/login?redirect=原路径`。

## 生产部署（nginx）

### 1. 构建前端

```bash
cd web2
pnpm install
pnpm build      # 产物输出到 web2/dist/
```

### 2. 配置 nginx

```bash
# 复制配置（修改 server_name 和 root 路径）
cp web2/nginx.conf /etc/nginx/conf.d/nastool.conf
vim /etc/nginx/conf.d/nastool.conf
# 主要改：server_name、root 指向 <项目路径>/web2/dist

nginx -t && nginx -s reload
```

nginx 会：
- 托管 `web2/dist` 静态文件（`/assets/` 长缓存）
- 反向代理 `/do` `/login_json` `/api/v1` `/img` 等到 Flask :3000
- SPA history 模式回退（`try_files $uri /index.html`）

### 3. 访问

浏览器打开 `http://<服务器IP>/`，进入 Vue 登录页。

## 构建产物

| 产物 | 大小（gzip） |
|------|------|
| 主 chunk（Vue+EP+Pinia+Router） | ~410 KB |
| 各页面 chunk | 1-23 KB（按需懒加载） |

## CORS 说明

- 开发模式：Vite 反向代理，同源 :5173，无 CORS。
- 生产模式：nginx 同源，无 CORS。
- 后端额外加了 CORS 头（`after_request`），以防需直接跨域调试，生产环境靠 nginx 不依赖它。