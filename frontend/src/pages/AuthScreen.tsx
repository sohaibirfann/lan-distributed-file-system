import { useState, type FormEvent } from 'react'
import { Button } from '../components/Button/Button'
import { TextField } from '../components/TextField/TextField'
import { ApiError, login, register, type Account } from '../lib/api'
import './AuthScreen.css'

export function AuthScreen({ onAuthenticated }: { onAuthenticated: (account: Account) => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [namespacePassphrase, setNamespacePassphrase] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const account =
        mode === 'login'
          ? await login(username, password)
          : await register(username, password, namespacePassphrase)
      onAuthenticated(account)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'something went wrong')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-screen">
      <form className="auth-form" onSubmit={handleSubmit}>
        <span className="auth-form__brand">Distributed File System</span>
        <h1 className="auth-form__heading">
          {mode === 'login' ? 'Sign in' : 'Create an account'}
        </h1>
        <TextField
          label="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
        />
        <TextField
          label="Password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {mode === 'register' && (
          <TextField
            label="Namespace passphrase"
            type="password"
            value={namespacePassphrase}
            onChange={(e) => setNamespacePassphrase(e.target.value)}
            required
          />
        )}
        {error && <p className="auth-form__error">{error}</p>}
        <div className="auth-form__actions">
          <Button type="submit" disabled={submitting}>
            {mode === 'login' ? 'Sign in' : 'Create account'}
          </Button>
          <Button
            type="button"
            variant="secondary"
            disabled={submitting}
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login')
              setError(null)
            }}
          >
            {mode === 'login' ? 'Create an account instead' : 'Sign in instead'}
          </Button>
        </div>
      </form>
    </div>
  )
}
