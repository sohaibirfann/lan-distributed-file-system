import sodium from 'libsodium-wrappers'

export const DEFAULT_CHUNK_SIZE_BYTES = 4 * 1024 * 1024

export function splitIntoChunks(file: Blob, chunkSize = DEFAULT_CHUNK_SIZE_BYTES): Blob[] {
  if (chunkSize <= 0) throw new Error('chunkSize must be positive')
  const chunks: Blob[] = []
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    chunks.push(file.slice(offset, offset + chunkSize))
  }
  return chunks.length > 0 ? chunks : [file]
}

// Wire format: nonce ‖ ciphertext ‖ AEAD tag, concatenated -- this is both the PUT
// body sent to a node and the exact bytes hashed to produce the chunk id.
export async function encryptChunk(chunk: Uint8Array, key: Uint8Array): Promise<Uint8Array> {
  await sodium.ready
  const nonce = sodium.randombytes_buf(sodium.crypto_aead_chacha20poly1305_IETF_NPUBBYTES)
  const ciphertext = sodium.crypto_aead_chacha20poly1305_ietf_encrypt(chunk, null, null, nonce, key)
  const combined = new Uint8Array(nonce.length + ciphertext.length)
  combined.set(nonce)
  combined.set(ciphertext, nonce.length)
  return combined
}

export async function decryptChunk(encryptedChunk: Uint8Array, key: Uint8Array): Promise<Uint8Array> {
  await sodium.ready
  const nonceLength = sodium.crypto_aead_chacha20poly1305_IETF_NPUBBYTES
  const nonce = encryptedChunk.slice(0, nonceLength)
  const ciphertext = encryptedChunk.slice(nonceLength)
  return sodium.crypto_aead_chacha20poly1305_ietf_decrypt(null, ciphertext, null, nonce, key)
}

export async function hashChunk(encryptedChunk: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', encryptedChunk as BufferSource)
  await sodium.ready
  return sodium.to_hex(new Uint8Array(digest))
}
