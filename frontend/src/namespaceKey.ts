import { argon2id } from 'hash-wasm'
import sodium from 'libsodium-wrappers'

// Encrypted under the derived key and stored server-side to detect a wrong passphrase early.
const VERIFIER_PLAINTEXT = 'distributed-file-system-namespace-verifier-v1'

const DB_NAME = 'dfs'
const STORE_NAME = 'namespace-key'
const RECORD_ID = 'derived-key'

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1)
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE_NAME)
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

export async function setDerivedKey(key: Uint8Array): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).put(key, RECORD_ID)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

export async function clearDerivedKey(): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).delete(RECORD_ID)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

export async function deriveKey(passphrase: string, salt: string): Promise<Uint8Array> {
  return argon2id({
    password: passphrase,
    salt,
    iterations: 3,
    parallelism: 1,
    memorySize: 65536,
    hashLength: 32, // ChaCha20-Poly1305 key size
    outputType: 'binary',
  })
}

export async function createVerifier(key: Uint8Array): Promise<string> {
  await sodium.ready
  const nonce = sodium.randombytes_buf(sodium.crypto_aead_chacha20poly1305_IETF_NPUBBYTES)
  const ciphertext = sodium.crypto_aead_chacha20poly1305_ietf_encrypt(
    VERIFIER_PLAINTEXT,
    null,
    null,
    nonce,
    key,
  )
  const combined = new Uint8Array(nonce.length + ciphertext.length)
  combined.set(nonce)
  combined.set(ciphertext, nonce.length)
  return sodium.to_base64(combined)
}

export async function verifyPassphrase(key: Uint8Array, verifierBlob: string): Promise<boolean> {
  await sodium.ready
  try {
    const combined = sodium.from_base64(verifierBlob)
    const nonceLength = sodium.crypto_aead_chacha20poly1305_IETF_NPUBBYTES
    const nonce = combined.slice(0, nonceLength)
    const ciphertext = combined.slice(nonceLength)
    const plaintext = sodium.crypto_aead_chacha20poly1305_ietf_decrypt(
      null,
      ciphertext,
      null,
      nonce,
      key,
      'text',
    )
    return plaintext === VERIFIER_PLAINTEXT
  } catch {
    return false
  }
}
