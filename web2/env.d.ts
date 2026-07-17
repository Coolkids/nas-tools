/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>
  export default component
}

interface ImportMetaEnv {
  /** Flask 后端地址，开发期为 http://localhost:3000，生产同源时留空 */
  readonly VITE_FLASK_BASE: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
