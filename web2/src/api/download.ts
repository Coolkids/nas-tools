import { doAction } from './request'

export interface DownloadTask {
  id: string
  name: string
  title: string
  image: string
  speed: string
  state: string
  progress: number
}

interface DownloadingResult {
  code: number
  msg?: string
  result: DownloadTask[]
}

interface PtResult {
  retcode: number
  retmsg?: string
  id: string | string[]
}

export function getDownloading(): Promise<DownloadingResult> {
  return doAction<DownloadingResult>('get_downloading', {})
}

export function ptStart(id: string | string[]): Promise<PtResult> {
  return doAction<PtResult>('pt_start', { id })
}

export function ptStop(id: string | string[]): Promise<PtResult> {
  return doAction<PtResult>('pt_stop', { id })
}

export function ptRemove(id: string | string[]): Promise<PtResult> {
  return doAction<PtResult>('pt_remove', { id })
}
