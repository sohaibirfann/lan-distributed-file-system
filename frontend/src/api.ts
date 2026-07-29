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

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  return parse<T>(response)
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
  return post<Account>('/register', { username, password, namespace_passphrase: namespacePassphrase })
}

export function login(username: string, password: string): Promise<Account> {
  return post<Account>('/login', { username, password })
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

export interface ReplicationConfig {
  replication_factor: number
  write_quorum: number
  max_file_size_bytes: number
  registered_node_count: number
  eligible_node_count: number
}

export function getReplicationConfig(): Promise<ReplicationConfig> {
  return get('/config/replication')
}

export function getPlacement(chunkId: string, exclude: string[]): Promise<Node[]> {
  const params = new URLSearchParams()
  for (const id of exclude) params.append('exclude', id)
  const query = params.toString()
  return get(`/placement/${chunkId}${query ? `?${query}` : ''}`)
}

export interface ChunkCreateIn {
  sequence_index: number
  hash: string
  size_bytes: number
  node_ids: number[]
}

export interface FileCreateBody {
  name: string
  size_bytes: number
  chunks: ChunkCreateIn[]
}

export function createFile(body: FileCreateBody): Promise<FileEntry> {
  return post<FileEntry>('/files', body)
}

export interface ChunkPlacement {
  node_id: number
  address: string
}

export interface ChunkDetail {
  sequence_index: number
  hash: string
  size_bytes: number
  nodes: ChunkPlacement[]
}

export interface FileDetail {
  id: number
  name: string
  size_bytes: number
  uploader_account_id: number
  created_at: string
  updated_at: string
  chunks: ChunkDetail[]
}

export function getFileDetail(id: number): Promise<FileDetail> {
  return get(`/files/${id}`)
}

export function reportChunkUnavailable(chunkId: string, nodeId: number): Promise<void> {
  return post(`/chunks/${chunkId}/report-unavailable`, { node_id: nodeId })
}
