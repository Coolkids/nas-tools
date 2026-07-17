import { doAction } from './request'

export interface RssHistoryItem {
  ID: number
  TYPE: string
  RSSID: string
  NAME: string
  YEAR: string
  TMDBID: string
  SEASON: string
  IMAGE: string
  DESC: string
  TOTAL?: number
  START?: number
  FINISH_TIME: string
  NOTE?: string
}

export interface RssMediaItem {
  id: number
  name: string
  year: string
  tmdbid: string
  image: string
  overview: string
  rss_sites: string[]
  search_sites: string[]
  over_edition: boolean
  filter_restype: string
  filter_pix: string
  filter_team: string
  filter_rule: string
  save_path: string
  download_setting: string | number
  fuzzy_match: boolean
  state: string
  poster?: string
  release_date?: string
  vote?: string
  keyword?: string
  season?: string
  total?: number
  lack?: number
  total_ep?: number
  current_ep?: number
}

export interface RssParser {
  id: number
  name: string
  type: string
  format: string
  params: string
  note?: string
}

export type RssType = 'MOV' | 'TV'

interface ListResult<T> {
  code: number
  msg?: string
  result: T
}

interface RssHistoryResult extends ListResult<RssHistoryItem[]> {}
interface RssListResult extends ListResult<Record<string, RssMediaItem>> {}
interface RssParserListResult {
  code: number
  msg?: string
  detail: RssParser[]
}
interface RssParserDetailResult {
  code: number
  detail: RssParser
}
interface SimpleResult {
  code: number
  msg?: string
}

export function getMovieRssList(): Promise<RssListResult> {
  return doAction<RssListResult>('get_movie_rss_list', {})
}

export function getTvRssList(): Promise<RssListResult> {
  return doAction<RssListResult>('get_tv_rss_list', {})
}

export function getRssHistory(type?: RssType): Promise<RssHistoryResult> {
  return doAction<RssHistoryResult>('get_rss_history', type ? { type } : {})
}

export function deleteRssHistory(rssid: string | number): Promise<SimpleResult> {
  return doAction<SimpleResult>('delete_rss_history', { rssid })
}

export function reRssHistory(rssid: string | number, type: RssType): Promise<SimpleResult> {
  return doAction<SimpleResult>('re_rss_history', { rssid, type })
}

export interface RemoveRssMediaParams {
  name: string
  type: RssType
  year?: string
  season?: string
  rssid?: string | number
  tmdbid?: string | number
  [key: string]: unknown
}

export function removeRssMedia(params: RemoveRssMediaParams): Promise<SimpleResult> {
  return doAction<SimpleResult>('remove_rss_media', params)
}

export function getRssParserList(): Promise<RssParserListResult> {
  return doAction<RssParserListResult>('get_rssparser', {})
}

export function getRssParserDetail(id: string | number): Promise<RssParserDetailResult> {
  return doAction<RssParserDetailResult>('get_rssparser', { id })
}

export interface RssParserPayload {
  id?: string | number
  name: string
  type: string
  format: string
  params: string
  [key: string]: unknown
}

export function updateRssParser(parser: RssParserPayload): Promise<SimpleResult> {
  return doAction<SimpleResult>('update_rssparser', parser)
}

export function deleteRssParser(id: string | number): Promise<SimpleResult> {
  return doAction<SimpleResult>('delete_rssparser', { id })
}

export interface UserRssTask {
  id: number
  name: string
  address: string
  parser: number | string
  parser_name?: string
  interval: number | string
  uses: 'D' | 'R'
  uses_text?: string
  include?: string
  exclude?: string
  filter?: string | number
  filter_name?: string
  state: string
  save_path?: string
  download_setting?: string | number
  recognization?: string
  over_edition?: number | string
  sites?: { rss_sites: Array<string | number>; search_sites: Array<string | number> }
  filter_args?: { restype?: string; pix?: string; team?: string }
  update_time?: string
  counter?: number
  [key: string]: unknown
}

export interface UserRssArticle {
  title: string
  enclosure?: string
  link?: string
  date?: string
  size?: string | number
  finish_flag?: boolean | number
  [key: string]: unknown
}

export interface UserRssHistory {
  title: string
  downloader?: string
  date: string
  [key: string]: unknown
}

interface UserRssListResult {
  code: number
  detail: UserRssTask | UserRssTask[]
}

interface UserRssArticlesResult {
  code: number
  msg?: string
  data?: UserRssArticle[]
  count?: number
  uses?: string
}

interface UserRssHistoryResult {
  code: number
  msg?: string
  data?: UserRssHistory[]
  count?: number
}

export interface UpdateUserRssTaskParams {
  id?: string | number
  name: string
  address: string
  parser: string | number
  interval: string | number
  uses: 'D' | 'R'
  state: string
  include?: string
  exclude?: string
  rule?: string | number
  save_path?: string
  download_setting?: string | number
  recognization?: string
  over_edition?: string | number
  sites?: { rss_sites: Array<string | number>; search_sites: Array<string | number> }
  restype?: string
  pix?: string
  team?: string
  [key: string]: unknown
}

export function getUserRssTasks(): Promise<UserRssListResult> {
  return doAction<UserRssListResult>('get_userrss_task', {})
}

export function getUserRssTaskDetail(id: string | number): Promise<UserRssListResult> {
  return doAction<UserRssListResult>('get_userrss_task', { id })
}

export function updateUserRssTask(params: UpdateUserRssTaskParams): Promise<SimpleResult> {
  return doAction<SimpleResult>('update_userrss_task', params)
}

export function deleteUserRssTask(id: string | number): Promise<SimpleResult> {
  return doAction<SimpleResult>('delete_userrss_task', { id })
}

export function runUserRss(id: string | number): Promise<SimpleResult> {
  return doAction<SimpleResult>('run_userrss', { id })
}

export function listRssArticles(id: string | number): Promise<UserRssArticlesResult> {
  return doAction<UserRssArticlesResult>('list_rss_articles', { id })
}

export function rssArticlesDownload(
  taskid: string | number,
  articles: Array<{ title: string; enclosure?: string }>
): Promise<SimpleResult> {
  return doAction<SimpleResult>('rss_articles_download', { taskid, articles })
}

export function rssArticlesCheck(
  flag: 'set_finished' | 'set_unfinish',
  articles: Array<{ title: string; enclosure?: string }>
): Promise<SimpleResult> {
  return doAction<SimpleResult>('rss_articles_check', { flag, articles })
}

export function rssArticleTest(
  taskid: string | number,
  title: string
): Promise<{ code: number; data?: Record<string, unknown> }> {
  return doAction('rss_article_test', { taskid, title })
}

export function listRssHistory(id: string | number): Promise<UserRssHistoryResult> {
  return doAction<UserRssHistoryResult>('list_rss_history', { id })
}
