import { decryptChunk, hashChunk } from './chunking'

export class ChunkDownloadError extends Error {
  readonly sequenceIndex: number

  constructor(sequenceIndex: number, message: string) {
    super(message)
    this.sequenceIndex = sequenceIndex
  }
}

export interface ChunkReplica {
  nodeId: number
  address: string
  token: string
}

export interface ChunkLocation {
  sequenceIndex: number
  hash: string
  replicas: ChunkReplica[]
}

export interface DownloadDeps {
  fetchChunk: (address: string, chunkId: string, token: string) => Promise<Uint8Array | null>
  reportUnavailable: (chunkId: string, nodeId: number) => Promise<void>
}

const DEFAULT_CHUNK_CONCURRENCY = 4
const CHUNK_FETCH_TIMEOUT_MS = 30_000

export async function fetchChunkFromNode(
  address: string,
  chunkId: string,
  token: string,
): Promise<Uint8Array | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), CHUNK_FETCH_TIMEOUT_MS)
  try {
    const response = await fetch(`http://${address}/chunks/${chunkId}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
    if (!response.ok) return null
    return new Uint8Array(await response.arrayBuffer())
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

// First replica whose ciphertext hashes to the expected value is authoritative.
export async function fetchChunkFromReplicas(
  sequenceIndex: number,
  chunkId: string,
  replicas: ChunkReplica[],
  deps: DownloadDeps,
): Promise<Uint8Array> {
  for (const replica of replicas) {
    const bytes = await deps.fetchChunk(replica.address, chunkId, replica.token)
    if (bytes !== null && (await hashChunk(bytes)) === chunkId) return bytes
    await deps.reportUnavailable(chunkId, replica.nodeId).catch(() => {})
  }
  throw new ChunkDownloadError(sequenceIndex, `chunk ${chunkId} unavailable from any replica`)
}

export async function downloadFile(
  chunks: ChunkLocation[],
  key: Uint8Array,
  deps: DownloadDeps,
  sink: SaveTarget,
  concurrency = DEFAULT_CHUNK_CONCURRENCY,
  onChunkDone?: (sequenceIndex: number) => void,
): Promise<void> {
  const pending = new Map<number, Uint8Array>()
  let nextToWrite = 0
  let flushing = false
  let nextIndex = 0
  let firstError: ChunkDownloadError | null = null

  async function flush() {
    if (flushing) return
    flushing = true
    try {
      while (pending.has(nextToWrite)) {
        const bytes = pending.get(nextToWrite)!
        pending.delete(nextToWrite)
        await sink.write(bytes)
        onChunkDone?.(nextToWrite)
        nextToWrite++
      }
    } finally {
      flushing = false
    }
  }

  async function worker() {
    while (true) {
      if (firstError) return
      const i = nextIndex++
      if (i >= chunks.length) return
      const chunk = chunks[i]

      try {
        const encrypted = await fetchChunkFromReplicas(
          chunk.sequenceIndex,
          chunk.hash,
          chunk.replicas,
          deps,
        )
        pending.set(chunk.sequenceIndex, await decryptChunk(encrypted, key))
        await flush()
      } catch (err) {
        firstError = err as ChunkDownloadError
        return
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, chunks.length) }, worker))
  if (firstError) throw firstError
  await sink.close()
}

interface FileSystemWritableFileStream {
  write(data: BufferSource): Promise<void>
  close(): Promise<void>
  abort(): Promise<void>
}

interface SaveFilePickerWindow {
  showSaveFilePicker(options: { suggestedName: string }): Promise<{
    createWritable(): Promise<FileSystemWritableFileStream>
  }>
}

export interface SaveTarget {
  write(chunk: Uint8Array): Promise<void>
  close(): Promise<void>
  // No-op on the fallback path; on the streaming path, discards the already-created file.
  abort(): Promise<void>
}

// Call immediately on the click that starts a download -- showSaveFilePicker needs
// a live user gesture, which fetching/decrypting the file could otherwise burn through.
export async function prepareSaveTarget(name: string): Promise<SaveTarget> {
  if ('showSaveFilePicker' in window) {
    const handle = await (window as unknown as SaveFilePickerWindow).showSaveFilePicker({
      suggestedName: name,
    })
    const writable = await handle.createWritable()
    return {
      write: (chunk) => writable.write(chunk as BufferSource),
      close: () => writable.close(),
      abort: () => writable.abort(),
    }
  }

  const parts: Uint8Array[] = []
  return {
    abort: async () => {},
    async write(chunk) {
      parts.push(chunk)
    },
    async close() {
      const blob = new Blob(parts.map((c) => c as BlobPart))
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      a.click()
      URL.revokeObjectURL(url)
    },
  }
}
