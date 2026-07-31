import { getFileDetail, type FileDetail } from './api'

const FILE_DETAIL_CACHE_TTL_MS = 60_000
const fileDetailCache = new Map<number, { detail: FileDetail; cachedAt: number }>()

export async function getCachedFileDetail(id: number): Promise<FileDetail> {
  const cached = fileDetailCache.get(id)
  if (cached && Date.now() - cached.cachedAt < FILE_DETAIL_CACHE_TTL_MS) return cached.detail
  const detail = await getFileDetail(id)
  fileDetailCache.set(id, { detail, cachedAt: Date.now() })
  return detail
}

// A deleted file's id can be reused, so its cache entry must not survive.
export function invalidateFileDetailCache(id: number) {
  fileDetailCache.delete(id)
}
