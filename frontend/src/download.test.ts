import { describe, expect, it, vi } from 'vitest'
import sodium from 'libsodium-wrappers'
import { encryptChunk, hashChunk } from './chunking'
import {
  ChunkDownloadError,
  type ChunkLocation,
  type ChunkReplica,
  type DownloadDeps,
  downloadFile,
  fetchChunkFromReplicas,
} from './download'

function replica(nodeId: number): ChunkReplica {
  return { nodeId, address: `node-${nodeId}:9000` }
}

describe('fetchChunkFromReplicas', () => {
  it('returns the bytes from the first replica that hashes correctly', async () => {
    const bytes = new Uint8Array([1, 2, 3])
    const hash = await hashChunk(bytes)
    const fetchChunk = vi.fn().mockResolvedValue(bytes)
    const deps: DownloadDeps = { fetchChunk, reportUnavailable: vi.fn().mockResolvedValue(undefined) }

    const result = await fetchChunkFromReplicas(0, hash, [replica(1)], deps)

    expect(result).toEqual(bytes)
    expect(fetchChunk).toHaveBeenCalledTimes(1)
  })

  it('fails over to the next replica when the first returns null', async () => {
    const bytes = new Uint8Array([4, 5, 6])
    const hash = await hashChunk(bytes)
    const fetchChunk = vi.fn().mockResolvedValueOnce(null).mockResolvedValueOnce(bytes)
    const reportUnavailable = vi.fn().mockResolvedValue(undefined)
    const deps: DownloadDeps = { fetchChunk, reportUnavailable }

    const result = await fetchChunkFromReplicas(0, hash, [replica(1), replica(2)], deps)

    expect(result).toEqual(bytes)
    expect(reportUnavailable).toHaveBeenCalledWith(hash, 1)
  })

  it('fails over when a replica returns bytes that hash-mismatch (corrupt/wrong data)', async () => {
    const bytes = new Uint8Array([7, 8, 9])
    const hash = await hashChunk(bytes)
    const wrongBytes = new Uint8Array([9, 9, 9])
    const fetchChunk = vi.fn().mockResolvedValueOnce(wrongBytes).mockResolvedValueOnce(bytes)
    const deps: DownloadDeps = {
      fetchChunk,
      reportUnavailable: vi.fn().mockResolvedValue(undefined),
    }

    const result = await fetchChunkFromReplicas(0, hash, [replica(1), replica(2)], deps)

    expect(result).toEqual(bytes)
  })

  it('throws a ChunkDownloadError naming the chunk when every replica fails', async () => {
    const deps: DownloadDeps = {
      fetchChunk: vi.fn().mockResolvedValue(null),
      reportUnavailable: vi.fn().mockResolvedValue(undefined),
    }

    await expect(
      fetchChunkFromReplicas(3, 'deadbeef', [replica(1), replica(2)], deps),
    ).rejects.toMatchObject({ sequenceIndex: 3, message: expect.stringContaining('deadbeef') })
  })

  it('does not let a reportUnavailable failure abort the failover', async () => {
    const bytes = new Uint8Array([1])
    const hash = await hashChunk(bytes)
    const deps: DownloadDeps = {
      fetchChunk: vi.fn().mockResolvedValueOnce(null).mockResolvedValueOnce(bytes),
      reportUnavailable: vi.fn().mockRejectedValue(new Error('network down')),
    }

    const result = await fetchChunkFromReplicas(0, hash, [replica(1), replica(2)], deps)

    expect(result).toEqual(bytes)
  })
})

describe('downloadFile', () => {
  it('decrypts every chunk and reassembles in sequence order regardless of completion order', async () => {
    await sodium.ready
    const key = sodium.randombytes_buf(32)
    const plaintexts = [new Uint8Array([1]), new Uint8Array([2]), new Uint8Array([3])]
    const encryptedByHash = new Map<string, Uint8Array>()
    const locations: ChunkLocation[] = []
    for (let i = 0; i < plaintexts.length; i++) {
      const encrypted = await encryptChunk(plaintexts[i], key)
      const hash = await hashChunk(encrypted)
      encryptedByHash.set(hash, encrypted)
      locations.push({ sequenceIndex: i, hash, replicas: [replica(1)] })
    }
    // Chunk 2 resolves fastest, chunk 0 slowest -- completion order is reversed.
    const delays = [30, 20, 10]
    const deps: DownloadDeps = {
      fetchChunk: vi.fn(async (_address: string, chunkId: string) => {
        const location = locations.find((l) => l.hash === chunkId)!
        await new Promise((resolve) => setTimeout(resolve, delays[location.sequenceIndex]))
        return encryptedByHash.get(chunkId)!
      }),
      reportUnavailable: vi.fn(),
    }
    const done: number[] = []

    const results = await downloadFile(locations, key, deps, 3, (i) => done.push(i))

    expect(results).toEqual(plaintexts)
    expect(done.sort()).toEqual([0, 1, 2])
  })

  it('stops scheduling new chunks after the first failure, even with concurrency > 1', async () => {
    // Chunk 0 fails fast and frees its worker while chunk 1 is still in flight --
    // that worker must not pick up chunk 2+ once chunk 0 has already failed.
    const locations: ChunkLocation[] = Array.from({ length: 5 }, (_, i) => ({
      sequenceIndex: i,
      hash: `hash-${i}`,
      replicas: [replica(1)],
    }))
    const attempted = new Set<number>()
    const deps: DownloadDeps = {
      fetchChunk: vi.fn(async (_address: string, chunkId: string) => {
        const index = Number(chunkId.split('-')[1])
        attempted.add(index)
        if (index !== 0) await new Promise((resolve) => setTimeout(resolve, 20))
        return null
      }),
      reportUnavailable: vi.fn().mockResolvedValue(undefined),
    }

    await expect(
      downloadFile(locations, new Uint8Array(32), deps, 2),
    ).rejects.toBeInstanceOf(ChunkDownloadError)
    expect(attempted.has(2)).toBe(false)
    expect(attempted.has(3)).toBe(false)
    expect(attempted.has(4)).toBe(false)
  })

  it('respects the concurrency bound', async () => {
    await sodium.ready
    const key = sodium.randombytes_buf(32)
    const encrypted = await encryptChunk(new Uint8Array([1]), key)
    const hash = await hashChunk(encrypted)
    const locations: ChunkLocation[] = Array.from({ length: 4 }, (_, i) => ({
      sequenceIndex: i,
      hash,
      replicas: [replica(1)],
    }))
    let inFlight = 0
    let maxInFlight = 0
    const deps: DownloadDeps = {
      fetchChunk: vi.fn(async () => {
        inFlight++
        maxInFlight = Math.max(maxInFlight, inFlight)
        await new Promise((resolve) => setTimeout(resolve, 5))
        inFlight--
        return encrypted
      }),
      reportUnavailable: vi.fn(),
    }

    await downloadFile(locations, key, deps, 2)

    expect(maxInFlight).toBeLessThanOrEqual(2)
  })
})
