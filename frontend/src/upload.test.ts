import { describe, expect, it, vi } from 'vitest'
import sodium from 'libsodium-wrappers'
import {
  ChunkUploadError,
  type PlacementNode,
  type PutChunkResult,
  type UploadDeps,
  uploadChunkWithQuorum,
  uploadFileChunks,
} from './upload'

function node(id: number): PlacementNode {
  return { id, address: `node-${id}:9000` }
}

describe('uploadChunkWithQuorum', () => {
  it('succeeds when the first round reaches quorum', async () => {
    const putChunk = vi.fn().mockResolvedValue('ok')
    const deps: UploadDeps = {
      getPlacement: vi.fn().mockResolvedValue([node(1), node(2), node(3)]),
      putChunk,
      writeQuorum: 2,
    }

    const acked = await uploadChunkWithQuorum(0, 'chunk-a', new Uint8Array(), deps)

    expect(acked.length).toBeGreaterThanOrEqual(2)
    expect(putChunk).toHaveBeenCalledTimes(3)
  })

  it('retries against fresh candidates when the first round falls short', async () => {
    const getPlacement = vi
      .fn()
      .mockResolvedValueOnce([node(1), node(2)])
      .mockResolvedValueOnce([node(3)])
    // node 1 fails, node 2 succeeds, then node 3 (the retry candidate) succeeds too
    const putChunk = vi.fn(async (_address: string, _chunkId: string): Promise<PutChunkResult> => {
      return 'ok'
    })
    putChunk
      .mockResolvedValueOnce('retryable')
      .mockResolvedValueOnce('ok')
      .mockResolvedValueOnce('ok')
    const deps: UploadDeps = { getPlacement, putChunk, writeQuorum: 2 }

    const acked = await uploadChunkWithQuorum(0, 'chunk-a', new Uint8Array(), deps)

    expect(acked.sort()).toEqual([2, 3])
    expect(getPlacement).toHaveBeenCalledTimes(2)
    expect(getPlacement).toHaveBeenNthCalledWith(2, 'chunk-a', ['1', '2'])
  })

  it('never re-PUTs a node that already acked', async () => {
    const getPlacement = vi
      .fn()
      .mockResolvedValueOnce([node(1), node(2)])
      .mockResolvedValueOnce([node(1), node(2), node(3)]) // still offers 1 and 2 again
    const putChunk = vi
      .fn()
      .mockResolvedValueOnce('ok') // node 1 succeeds
      .mockResolvedValueOnce('retryable') // node 2 fails
      .mockResolvedValueOnce('ok') // node 3 succeeds on retry
    const deps: UploadDeps = { getPlacement, putChunk, writeQuorum: 2 }

    const acked = await uploadChunkWithQuorum(0, 'chunk-a', new Uint8Array(), deps)

    expect(acked.sort()).toEqual([1, 3])
    expect(putChunk).toHaveBeenCalledTimes(3) // not 4 -- node 1 was never retried
  })

  it('throws a ChunkUploadError naming the chunk when placement 503s', async () => {
    const deps: UploadDeps = {
      getPlacement: vi.fn().mockRejectedValue(new Error('503')),
      putChunk: vi.fn(),
      writeQuorum: 2,
    }

    await expect(uploadChunkWithQuorum(5, 'chunk-b', new Uint8Array(), deps)).rejects.toMatchObject(
      { sequenceIndex: 5, message: expect.stringContaining('chunk-b') },
    )
  })

  it('throws when the ring is exhausted (no fresh candidates left)', async () => {
    const deps: UploadDeps = {
      getPlacement: vi.fn().mockResolvedValueOnce([node(1)]).mockResolvedValueOnce([]),
      putChunk: vi.fn().mockResolvedValue('retryable'),
      writeQuorum: 2,
    }

    await expect(uploadChunkWithQuorum(0, 'chunk-c', new Uint8Array(), deps)).rejects.toBeInstanceOf(
      ChunkUploadError,
    )
  })

  it('gives up after MAX_RETRY_ROUNDS against a flapping node', async () => {
    const deps: UploadDeps = {
      // Always offers a fresh, never-tried node, but it never succeeds -- this must
      // still terminate rather than looping forever.
      getPlacement: vi.fn(async (_chunkId: string, exclude: string[]) => {
        const nextId = exclude.length + 1
        return [node(nextId)]
      }),
      putChunk: vi.fn().mockResolvedValue('retryable'),
      writeQuorum: 2,
    }

    await expect(uploadChunkWithQuorum(0, 'chunk-d', new Uint8Array(), deps)).rejects.toBeInstanceOf(
      ChunkUploadError,
    )
  })

  it('aborts immediately on a client-error outcome instead of retrying elsewhere', async () => {
    const getPlacement = vi.fn().mockResolvedValue([node(1), node(2), node(3)])
    const putChunk = vi.fn().mockResolvedValue('client-error')
    const deps: UploadDeps = { getPlacement, putChunk, writeQuorum: 2 }

    await expect(uploadChunkWithQuorum(0, 'chunk-e', new Uint8Array(), deps)).rejects.toBeInstanceOf(
      ChunkUploadError,
    )
    expect(getPlacement).toHaveBeenCalledTimes(1) // no retry round was attempted
  })
})

describe('uploadFileChunks', () => {
  it('uploads every chunk and reports each completion once', async () => {
    await sodium.ready
    const key = sodium.randombytes_buf(32)
    const file = new Blob([new Uint8Array(25)])
    const deps: UploadDeps = {
      getPlacement: vi.fn().mockResolvedValue([node(1), node(2)]),
      putChunk: vi.fn().mockResolvedValue('ok'),
      writeQuorum: 1,
    }
    const done: number[] = []

    const results = await uploadFileChunks(file, key, deps, 2, (i) => done.push(i), 10)

    expect(results.map((r) => r.sequenceIndex)).toEqual([0, 1, 2])
    expect(results.map((r) => r.sizeBytes)).toEqual([10, 10, 5])
    expect(done.sort()).toEqual([0, 1, 2])
  })

  it('stops scheduling new chunks after the first failure and rejects with it', async () => {
    await sodium.ready
    const key = sodium.randombytes_buf(32)
    const file = new Blob([new Uint8Array(50)]) // 5 chunks of size 10
    const attemptedChunkIds = new Set<string>()
    const deps: UploadDeps = {
      // Always offers the same single node, which always fails -- so the very
      // first chunk exhausts its only candidate on round 1 and the pool must stop
      // before ever reaching chunks 2-5.
      getPlacement: vi.fn(async (chunkId: string) => {
        attemptedChunkIds.add(chunkId)
        return [node(1)]
      }),
      putChunk: vi.fn().mockResolvedValue('retryable'),
      writeQuorum: 1,
    }

    await expect(uploadFileChunks(file, key, deps, 1, undefined, 10)).rejects.toBeInstanceOf(
      ChunkUploadError,
    )
    expect(attemptedChunkIds.size).toBe(1)
  })

  it('respects the concurrency bound', async () => {
    await sodium.ready
    const key = sodium.randombytes_buf(32)
    const file = new Blob([new Uint8Array(40)]) // 4 chunks of size 10
    let inFlight = 0
    let maxInFlight = 0
    const deps: UploadDeps = {
      getPlacement: vi.fn().mockResolvedValue([node(1)]),
      putChunk: vi.fn(async () => {
        inFlight++
        maxInFlight = Math.max(maxInFlight, inFlight)
        await new Promise((resolve) => setTimeout(resolve, 5))
        inFlight--
        return 'ok' as const
      }),
      writeQuorum: 1,
    }

    await uploadFileChunks(file, key, deps, 2, undefined, 10)

    expect(maxInFlight).toBeLessThanOrEqual(2)
  })
})
