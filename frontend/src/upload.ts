import { DEFAULT_CHUNK_SIZE_BYTES, encryptChunk, hashChunk, splitIntoChunks } from './chunking'

export class ChunkUploadError extends Error {
  readonly sequenceIndex: number

  constructor(sequenceIndex: number, message: string) {
    super(message)
    this.sequenceIndex = sequenceIndex
  }
}

export interface PlacementNode {
  id: number
  address: string
}

// 'client-error' means the body itself is invalid and will fail against every node --
// abort immediately instead of burning retries on the rest of the ring.
export type PutChunkResult = 'ok' | 'retryable' | 'client-error'

export interface UploadDeps {
  getPlacement: (chunkId: string, exclude: string[]) => Promise<PlacementNode[]>
  putChunk: (address: string, chunkId: string, bytes: Uint8Array) => Promise<PutChunkResult>
  writeQuorum: number
}

export interface UploadedChunk {
  sequenceIndex: number
  hash: string
  sizeBytes: number
  nodeIds: number[]
}

const MAX_RETRY_ROUNDS = 5
const DEFAULT_CHUNK_CONCURRENCY = 4
const CHUNK_PUT_TIMEOUT_MS = 30_000

export async function putChunkToNode(
  address: string,
  chunkId: string,
  bytes: Uint8Array,
): Promise<PutChunkResult> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), CHUNK_PUT_TIMEOUT_MS)
  try {
    const response = await fetch(`http://${address}/chunks/${chunkId}`, {
      method: 'PUT',
      body: bytes as BodyInit,
      signal: controller.signal,
    })
    if (response.ok) return 'ok'
    // 400/422 mean the body doesn't match its claimed id -- a client-side bug, not this node's fault.
    if (response.status === 400 || response.status === 422) return 'client-error'
    return 'retryable'
  } catch {
    return 'retryable'
  } finally {
    clearTimeout(timer)
  }
}

export async function uploadChunkWithQuorum(
  sequenceIndex: number,
  chunkId: string,
  bytes: Uint8Array,
  deps: UploadDeps,
): Promise<number[]> {
  const acked = new Set<number>()
  const attempted = new Set<string>()

  for (let round = 0; round < MAX_RETRY_ROUNDS; round++) {
    let candidates: PlacementNode[]
    try {
      candidates = await deps.getPlacement(chunkId, [...attempted])
    } catch {
      throw new ChunkUploadError(sequenceIndex, `not enough nodes available for chunk ${chunkId}`)
    }

    const fresh = candidates.filter((n) => !attempted.has(String(n.id)))
    if (fresh.length === 0) {
      throw new ChunkUploadError(sequenceIndex, `ring exhausted for chunk ${chunkId}`)
    }

    const outcomes = await Promise.all(
      fresh.map(async (n) => ({ node: n, result: await deps.putChunk(n.address, chunkId, bytes) })),
    )
    for (const { node, result } of outcomes) {
      attempted.add(String(node.id))
      if (result === 'ok') acked.add(node.id)
      if (result === 'client-error') {
        throw new ChunkUploadError(
          sequenceIndex,
          `chunk ${chunkId} was rejected as invalid -- this indicates a bug in the client's chunking or encryption, not a placement problem`,
        )
      }
    }

    if (acked.size >= deps.writeQuorum) return [...acked]
  }

  throw new ChunkUploadError(
    sequenceIndex,
    `chunk ${chunkId} failed to reach write quorum (${deps.writeQuorum}) after ${MAX_RETRY_ROUNDS} rounds`,
  )
}

export async function uploadFileChunks(
  file: Blob,
  key: Uint8Array,
  deps: UploadDeps,
  concurrency = DEFAULT_CHUNK_CONCURRENCY,
  onChunkDone?: (sequenceIndex: number) => void,
  chunkSize = DEFAULT_CHUNK_SIZE_BYTES,
): Promise<UploadedChunk[]> {
  const blobs = splitIntoChunks(file, chunkSize)
  const results: UploadedChunk[] = new Array(blobs.length)
  let nextIndex = 0
  let firstError: ChunkUploadError | null = null

  async function worker() {
    while (true) {
      if (firstError) return
      const i = nextIndex++
      if (i >= blobs.length) return

      // Bytes materialize only when a worker reaches this index, bounding memory to concurrency * chunkSize.
      const plaintext = new Uint8Array(await blobs[i].arrayBuffer())
      const encrypted = await encryptChunk(plaintext, key)
      const hash = await hashChunk(encrypted)

      try {
        const nodeIds = await uploadChunkWithQuorum(i, hash, encrypted, deps)
        results[i] = { sequenceIndex: i, hash, sizeBytes: plaintext.length, nodeIds }
        onChunkDone?.(i)
      } catch (err) {
        firstError = err as ChunkUploadError
        return
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, blobs.length) }, worker))
  if (firstError) throw firstError
  return results
}
