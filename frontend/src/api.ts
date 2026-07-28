export interface Account {
  id: number
  username: string
  created_at: string
}

export class ApiError extends Error {}

async function parse(response: Response): Promise<Account> {
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ApiError(body?.detail ?? `request failed (${response.status})`)
  }
  return body as Account
}

async function post(path: string, body: unknown): Promise<Account> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  return parse(response)
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
  return parse(response)
}
