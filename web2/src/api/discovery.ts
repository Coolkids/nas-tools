import { doAction } from './request'

export interface RecommendItem {
  id: string | number
  orgid?: string | number
  tmdbid?: string | number
  title: string
  type?: string
  media_type?: string
  year?: string
  vote?: string | number
  image?: string
  overview?: string
  fav?: string
  rssid?: string | number
  weekday?: string
  date?: string
  site?: string
}

export interface GetRecommendParams {
  type: string
  subtype?: string
  page?: number
  week?: string
  tmdbid?: string | number
  personid?: string | number
  keyword?: string
  source?: string
  params?: Record<string, unknown>
}

export interface GetRecommendResult {
  code: number
  msg?: string
  Items: RecommendItem[]
}

export function getRecommend(params: GetRecommendParams): Promise<GetRecommendResult> {
  return doAction<GetRecommendResult>('get_recommend', params)
}

export interface PersonItem {
  id: string | number
  name: string
  role?: string
  image?: string
  gender?: number
  known_for_department?: string
  original_name?: string
  popularity?: number
  cast_id?: number
  credit_id?: string
  order?: number
  profile?: string
}

export interface MediaDetail {
  tmdbid: string | number
  douban_id?: string
  background?: string
  image?: string
  vote?: string | number
  year?: string
  title: string
  genres?: string
  overview?: string
  runtime?: string
  fact?: Array<Record<string, string>>
  crews?: Array<Record<string, string>>
  actors?: PersonItem[]
  link?: string
  douban_link?: string
  fav?: string
  rssid?: string | number
}

export interface MediaDetailResult {
  code: number
  msg?: string
  data: MediaDetail
}

export interface MediaListResult {
  code: number
  msg?: string
  data: RecommendItem[]
}

export interface PersonListResult {
  code: number
  msg?: string
  data: PersonItem[]
}

export function mediaDetail(type: string, tmdbid: string | number): Promise<MediaDetailResult> {
  return doAction<MediaDetailResult>('media_detail', { type, tmdbid })
}

export function mediaRecommendations(
  type: string,
  tmdbid: string | number,
  page = 1
): Promise<MediaListResult> {
  return doAction<MediaListResult>('media_recommendations', { type, tmdbid, page })
}

export function mediaPerson(type: string, tmdbid: string | number): Promise<PersonListResult> {
  return doAction<PersonListResult>('media_person', { type, tmdbid })
}

export function personMedias(
  personid: string | number,
  type?: string,
  page = 1
): Promise<MediaListResult> {
  return doAction<MediaListResult>('person_medias', { personid, type, page })
}

export function proxyDoubanImage(url?: string): string {
  if (!url) return ''
  if (url.indexOf('doubanio.com') !== -1) {
    return `/img?url=${url}`
  }
  return url
}
