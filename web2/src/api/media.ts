import { doAction } from './request'

export interface SearchParams {
  search_word: string
  tmdbid?: string | number
  media_type?: string
  unident?: boolean
  filters?: Record<string, unknown>
}

export interface SearchResult {
  code: number
  msg?: string
}

export function search(params: SearchParams): Promise<SearchResult> {
  return doAction<SearchResult>('search', params)
}

export interface TorrentItem {
  id: number | string
  seeders: number | string
  enclosure: string
  site: string
  torrent_name: string
  description?: string
  pageurl: string
  uploadvalue: number
  downloadvalue: number
  size: string
  respix?: string
  restype?: string
  reseffect?: string
  releasegroup?: string
  video_encode?: string
}

export interface TorrentGroup {
  group_info: { respix: string; restype: string }
  group_total: number
  group_torrents: Record<
    string,
    {
      unique_info: {
        video_encode: string
        size: string
        reseffect: string
        releasegroup: string
      }
      torrent_list: TorrentItem[]
    }
  >
}

export type SeasonDict = Record<string, TorrentGroup>

export interface SearchResultItem {
  key: string | number
  title: string
  year?: string
  type_key?: string
  image?: string
  type?: string
  vote?: string | number
  tmdbid?: string | number
  backdrop?: string
  poster?: string
  overview?: string
  exist?: boolean
  torrent_dict: Array<[string, SeasonDict]>
  filter: {
    site: string[]
    free: Array<{ value: string; name: string }>
    video: string[]
    season: string[]
  }
}

export interface GetSearchResultResult {
  code: number
  total: number
  result: Record<string, SearchResultItem>
}

export function getSearchResult(): Promise<GetSearchResultResult> {
  return doAction<GetSearchResultResult>('get_search_result', {})
}
