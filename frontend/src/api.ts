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

export class ApiError extends Error {}

async function parse<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ApiError(body?.detail ?? `request failed (${response.status})`)
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
