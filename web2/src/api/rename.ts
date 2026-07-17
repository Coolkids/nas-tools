import { doAction } from './request'

export interface TransferHistoryItem {
  ID: number
  TYPE: string
  TITLE: string
  YEAR: string
  CATEGORY: string
  SEASON_EPISODE: string
  TMDBID: string
  SOURCE: string
  SOURCE_PATH: string
  SOURCE_FILENAME: string
  DEST: string
  DEST_PATH: string
  DEST_FILENAME: string
  DATE: string
  MODE: string
  SYNC_MODE: string
  RMT_MODE: string
  [key: string]: unknown
}

export interface TransferHistoryResult {
  code: number
  msg?: string
  total: number
  result: TransferHistoryItem[]
  totalPage: number
  pageNum: number
  currentPage: number
}

export interface UnknownItem {
  id: number
  path: string
  to: string
  name: string
  sync_mode: string
  rmt_mode: string
}

export interface TmdbCacheInfo {
  id: number
  title: string
  year: string
  media_type: string
  poster_path: string
  backdrop_path: string
}

export type TmdbCacheItem = [string, TmdbCacheInfo, string]

export interface TmdbCacheResult {
  code: number
  msg?: string
  total: number
  result: TmdbCacheItem[]
  totalPage: number
  pageNum: number
  currentPage: number
}

export function getTransferHistory(
  params: { page?: number; keyword?: string; pagenum?: number } = {}
): Promise<TransferHistoryResult> {
  return doAction<TransferHistoryResult>('get_transfer_history', {
    page: params.page ?? 1,
    keyword: params.keyword ?? '',
    pagenum: params.pagenum ?? 30
  })
}

export function deleteHistory(
  flag: 'del_source' | 'del_dest' | 'del_all',
  logids: Array<number | string>
): Promise<{ code: number; msg?: string }> {
  return doAction('delete_history', { flag, logids })
}

export function reIdentification(
  flag: 'history' | 'unidentification',
  ids: Array<number | string>
): Promise<{ retcode: number; retmsg: string }> {
  return doAction('re_identification', { flag, ids })
}

export function getUnknownList(): Promise<{ code: number; msg?: string; items: UnknownItem[] }> {
  return doAction('get_unknown_list', {})
}

export function getTmdbCache(
  params: { page?: number; keyword?: string; pagenum?: number } = {}
): Promise<TmdbCacheResult> {
  return doAction<TmdbCacheResult>('get_tmdb_cache', {
    page: params.page ?? 1,
    keyword: params.keyword ?? '',
    pagenum: params.pagenum ?? 30
  })
}

export function deleteTmdbCache(cacheKey: string): Promise<{ code: number; msg?: string }> {
  return doAction('delete_tmdb_cache', { cache_key: cacheKey })
}

export function modifyTmdbCache(key: string, title: string): Promise<{ code: number; msg?: string }> {
  return doAction('modify_tmdb_cache', { key, title })
}

export function clearTmdbCache(): Promise<{ code: number; msg?: string }> {
  return doAction('clear_tmdb_cache', {})
}

export type TransferMode = 'copy' | 'link' | 'softlink' | 'move' | 'rclone' | 'rclonecopy' | 'minio' | 'miniocopy'

export type ManualMediaType = 'movie' | 'tv' | 'anime'

export interface RenameUdfParams {
  inpath: string
  outpath?: string
  syncmod: TransferMode
  tmdb?: string | number
  type?: ManualMediaType
  season?: string | number
  episode_format?: string
  episode_details?: string
  episode_offset?: string | number
  min_filesize?: string | number
  [key: string]: unknown
}

export function delUnknownPath(id: string | number | Array<string | number>): Promise<{ retcode: number }> {
  return doAction('del_unknown_path', { id })
}

export function renameUdf(params: RenameUdfParams): Promise<{ retcode: number; retmsg?: string }> {
  return doAction('rename_udf', params)
}
