import { doAction } from './request'
import instance from './request'

export interface MediaCountResult {
  code: number
  msg?: string
  Movie?: string
  Series?: string
  Episodes?: string
  Music?: string
  User?: number
}

export interface LibrarySpaceResult {
  code: number
  UsedPercent?: number | string
  FreeSpace?: string
  UsedSapce?: string
  TotalSpace?: string
}

export interface TransferStatisticsResult {
  code: number
  MovieChartLabels: string[]
  MovieNums: number[]
  TvChartLabels: string[]
  TvNums: number[]
  AnimeNums: number[]
}

export interface PlayHistoryItem {
  type: string
  event: string
  date: string
}

export interface PlayHistoryResult {
  code: number
  result: PlayHistoryItem[]
}

export function getLibraryMediacount(): Promise<MediaCountResult> {
  return doAction<MediaCountResult>('get_library_mediacount', {})
}

export function getLibrarySpacesize(): Promise<LibrarySpaceResult> {
  return doAction<LibrarySpaceResult>('get_library_spacesize', {})
}

export function getTransferStatistics(): Promise<TransferStatisticsResult> {
  return doAction<TransferStatisticsResult>('get_transfer_statistics', {})
}

export function getLibraryPlayhistory(): Promise<PlayHistoryResult> {
  return doAction<PlayHistoryResult>('get_library_playhistory', {})
}

export interface RefreshProcessResult {
  code: number
  value: number
  text: string
}

export function refreshProcess(type: string): Promise<RefreshProcessResult> {
  return doAction<RefreshProcessResult>('refresh_process', { type })
}

export interface SchResult {
  retmsg?: string
  item?: string
}

export function runScheduler(item: string): Promise<SchResult> {
  return doAction<SchResult>('sch', { item })
}

export interface TruncateResult {
  code: number
  msg?: string
}

export function truncateBlacklist(): Promise<TruncateResult> {
  return doAction<TruncateResult>('truncate_blacklist', {})
}

export function truncateRsshistory(): Promise<TruncateResult> {
  return doAction<TruncateResult>('truncate_rsshistory', {})
}

export interface NameTestData {
  type: string
  name: string
  title: string
  year: string | number
  season_episode: string
  part: string
  tmdbid: number | string
  tmdblink: string
  tmdb_S_E_link: string
  category: string
  restype: string
  effect: string
  pix: string
  team: string
  video_codec: string
  audio_codec: string
  org_string: string
  ignored_words: string
  replaced_words: string
  offset_words: string
}

export interface NameTestResult {
  code: number
  data?: NameTestData | { name: string }
}

export function nameTest(name: string): Promise<NameTestResult> {
  return doAction<NameTestResult>('name_test', { name })
}

export interface NetTestResult {
  res: boolean
  time: string
}

export function netTest(url: string): Promise<NetTestResult> {
  const form = new URLSearchParams()
  form.append('cmd', 'net_test')
  form.append('data', JSON.stringify(url))
  return instance
    .post<NetTestResult>(`/do?random=${Math.random()}`, form, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    })
    .then((r) => r.data)
}

export const NETTEST_TARGETS: string[] = [
  'www.themoviedb.org',
  'api.themoviedb.org',
  'api.tmdb.org',
  'image.tmdb.org',
  'webservice.fanart.tv',
  'api.telegram.org',
  'qyapi.weixin.qq.com',
  'www.opensubtitles.org'
]
