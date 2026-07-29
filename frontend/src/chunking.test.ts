import { describe, expect, it } from 'vitest'
import sodium from 'libsodium-wrappers'
import { DEFAULT_CHUNK_SIZE_BYTES, encryptChunk, hashChunk, splitIntoChunks } from './chunking'

describe('splitIntoChunks', () => {
  it('splits a file that is an exact multiple of the chunk size', () => {
    const file = new Blob([new Uint8Array(20)])
    const chunks = splitIntoChunks(file, 10)
    expect(chunks.map((c) => c.size)).toEqual([10, 10])
  })

  it('splits a file with a remainder into a smaller final chunk', () => {
    const file = new Blob([new Uint8Array(25)])
    const chunks = splitIntoChunks(file, 10)
    expect(chunks.map((c) => c.size)).toEqual([10, 10, 5])
  })

  it('returns a single chunk for a file smaller than the chunk size', () => {
    const file = new Blob([new Uint8Array(3)])
    const chunks = splitIntoChunks(file, 10)
    expect(chunks.map((c) => c.size)).toEqual([3])
  })

  it('returns a single empty chunk for an empty file', () => {
    const file = new Blob([])
    const chunks = splitIntoChunks(file, 10)
    expect(chunks).toHaveLength(1)
    expect(chunks[0].size).toBe(0)
  })

  it('defaults to a 4 MiB chunk size', () => {
    const file = new Blob([new Uint8Array(DEFAULT_CHUNK_SIZE_BYTES + 1)])
    expect(splitIntoChunks(file).map((c) => c.size)).toEqual([DEFAULT_CHUNK_SIZE_BYTES, 1])
  })

  it('rejects a non-positive chunk size instead of looping forever', () => {
    const file = new Blob([new Uint8Array(10)])
    expect(() => splitIntoChunks(file, 0)).toThrow()
    expect(() => splitIntoChunks(file, -1)).toThrow()
  })
})

describe('encryptChunk', () => {
  it('round-trips through libsodium decryption', async () => {
    await sodium.ready
    const key = sodium.randombytes_buf(32)
    const plaintext = new TextEncoder().encode('hello chunk')

    const encrypted = await encryptChunk(plaintext, key)
    const nonceLength = sodium.crypto_aead_chacha20poly1305_IETF_NPUBBYTES
    const nonce = encrypted.slice(0, nonceLength)
    const ciphertext = encrypted.slice(nonceLength)
    const decrypted = sodium.crypto_aead_chacha20poly1305_ietf_decrypt(null, ciphertext, null, nonce, key)

    expect(decrypted).toEqual(plaintext)
  })

  it('uses a fresh nonce every call, so identical input encrypts differently', async () => {
    await sodium.ready
    const key = sodium.randombytes_buf(32)
    const plaintext = new Uint8Array([1, 2, 3])

    const first = await encryptChunk(plaintext, key)
    const second = await encryptChunk(plaintext, key)

    expect(first).not.toEqual(second)
  })
})

describe('hashChunk', () => {
  it('matches the known SHA-256 digest of an empty input', async () => {
    const hash = await hashChunk(new Uint8Array())
    expect(hash).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
  })

  it('is deterministic for the same input', async () => {
    const bytes = new Uint8Array([1, 2, 3, 4, 5])
    expect(await hashChunk(bytes)).toBe(await hashChunk(bytes))
  })
})
