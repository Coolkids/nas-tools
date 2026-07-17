/**
 * 通用工具函数（从旧 web/static/js/util.js 迁移，去除 jQuery 依赖）。
 */

/** bytes 转 可读大小 */
export function bytesToSize(bytes: number): string {
  if (bytes < 0.1 * 1024) return `${bytes} B`
  if (bytes < 0.1 * 1024 * 1024) return `${(bytes / 1024).toFixed(2)} KB`
  if (bytes < 0.1 * 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
  if (bytes < 0.1 * 1024 * 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
  if (bytes < 0.1 * 1024 * 1024 * 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024 * 1024)).toFixed(2)} TB`
  return `${(bytes / (1024 * 1024 * 1024 * 1024 * 1024)).toFixed(2)} PB`
}

/** 暂停 */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 版本号比较：v1>v2 返回 1，相等 0，小于 -1 */
export function compareVersion(version1: string, version2: string): number {
  const v1Parts = version1.split(' ')
  const v2Parts = version2.split(' ')
  const c1 = v1Parts[1]
  const c2 = v2Parts[1]
  const v1 = v1Parts[0].replace('v', '').split('.')
  const v2 = v2Parts[0].replace('v', '').split('.')
  const len = Math.max(v1.length, v2.length)
  while (v1.length < len) v1.push('0')
  while (v2.length < len) v2.push('0')
  for (let i = 0; i < len; i++) {
    const num1 = parseInt(v1[i], 10)
    const num2 = parseInt(v2[i], 10)
    if (num1 > num2) return 1
    if (num1 < num2) return -1
  }
  if (c1 && c2) return c1 === c2 ? 0 : 2
  return 0
}

/** 从 URL query 中取参数 */
export function getQueryVariable(variable: string, url?: string): string | false {
  const search = url ? new URL(url, window.location.origin).search.substring(1) : window.location.search.substring(1)
  const vars = search.split('&')
  for (const v of vars) {
    const pair = v.split('=')
    if (pair[0] === variable) return pair[1]
  }
  return false
}

/** 对象数组排序 */
export function dictArraySorting<T>(arr: T[], key: keyof T, sortType: 'asc' | 'desc' = 'asc'): T[] {
  return [...arr].sort((a, b) => {
    let v1 = a[key] as unknown as string | number
    let v2 = b[key] as unknown as string | number
    if (!isNaN(Number(v1)) && !isNaN(Number(v2))) {
      v1 = Number(v1)
      v2 = Number(v2)
    }
    if (sortType === 'asc') return v1 < v2 ? -1 : v1 > v2 ? 1 : 0
    return v1 > v2 ? -1 : v1 < v2 ? 1 : 0
  })
}
