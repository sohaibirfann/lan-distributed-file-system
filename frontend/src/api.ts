export interface Account {
  id: number
  username: string
  created_at: string
}

export type NodeState = 'up' | 'suspect' | 'down' | 'draining'

export interface Node {
  id: number
  address: string
  capacity_budget_bytes: number
  free_disk_bytes: number
  used_bytes: number
  effective_capacity_bytes: number
  state: NodeState
  draining: boolean
  registered_at: string
  last_heartbeat_at: string
}

export interface Event {
  id: number
  created_at: string
  kind: 'node_state_transition' | 'repair'
  message: string
}

export interface FileEntry {
  id: number
  name: string
  size_bytes: number
  uploader_account_id: number
  created_at: string
  updated_at: string
  chunk_count: number
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

// FastAPI's own errors (401/404/etc.) send a plain string `detail`; Pydantic
// validation errors (422) send an array of {msg, loc, ...} objects instead.
function errorMessage(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length > 0) {
    return detail.map((e) => e?.msg ?? JSON.stringify(e)).join('; ')
  }
  return `request failed (${status})`
}

async function parse<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ApiError(errorMessage(body, response.status), response.status)
  }
  return body as T
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: 'include' })
  return parse<T>(response)
}

async function post(path: string, body: unknown): Promise<Account> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  return parse<Account>(response)
}

async function patch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  return parse<T>(response)
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  return parse<T>(response)
}

async function del(path: string): Promise<void> {
  const response = await fetch(path, { method: 'DELETE', credentials: 'include' })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ApiError(errorMessage(body, response.status), response.status)
  }
}

export function register(
  username: string,
  password: string,
  namespacePassphrase: string,
): Promise<Account> {
  return post('/register', { username, password, namespace_passphrase: namespacePassphrase })
}

export function login(username: string, password: string): Promise<Account> {
  return post('/login', { username, password })
}

export async function me(): Promise<Account | null> {
  const response = await fetch('/me', { credentials: 'include' })
  if (response.status === 401) return null
  return parse<Account>(response)
}

export function getNodes(): Promise<Node[]> {
  return get('/nodes')
}

export function getEvents(): Promise<Event[]> {
  return get('/events')
}

export function getFiles(): Promise<FileEntry[]> {
  return get('/files')
}

export function renameFile(id: number, name: string): Promise<FileEntry> {
  return patch(`/files/${id}`, { name })
}

export function deleteFile(id: number): Promise<void> {
  return del(`/files/${id}`)
}

export function getNamespaceSalt(): Promise<{ salt: string }> {
  return get('/namespace/salt')
}

export function getNamespaceVerifier(): Promise<{ verifier: string | null }> {
  return get('/namespace/verifier')
}

export function putNamespaceVerifier(verifier: string): Promise<{ verifier: string }> {
  return put('/namespace/verifier', { verifier })
}
