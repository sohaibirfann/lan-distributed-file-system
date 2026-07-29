import { useState, type FormEvent } from 'react'
import { Card } from './components/Card/Card'
import { Button } from './components/Button/Button'
import { TextField } from './components/TextField/TextField'
import { ApiError, getNamespaceSalt, type Account } from './api'
import './SettingsPage.css'

const UNLOCKED_KEY = 'dfs-namespace-unlocked'

export function SettingsPage({ account }: { account: Account }) {
  const [passphrase, setPassphrase] = useState('')
  const [unlocked, setUnlocked] = useState(() => localStorage.getItem(UNLOCKED_KEY) === 'true')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await getNamespaceSalt()
      localStorage.setItem(UNLOCKED_KEY, 'true')
      setUnlocked(true)
      setPassphrase('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'could not reach the coordinator')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="settings-page">
      <Card title="Account">
        <div className="settings-page__field">
          <span className="settings-page__field-label">Username</span>
          <span className="settings-page__field-value">{account.username}</span>
        </div>
        <div className="settings-page__field">
          <span className="settings-page__field-label">Member since</span>
          <span className="settings-page__field-value">
            {new Date(account.created_at).toLocaleDateString()}
          </span>
        </div>
      </Card>

      <Card title="Namespace Passphrase">
        {unlocked ? (
          <div>
            <p className="settings-page__unlocked">Unlocked on this browser.</p>
            <Button
              variant="secondary"
              onClick={() => {
                localStorage.removeItem(UNLOCKED_KEY)
                setUnlocked(false)
              }}
            >
              Re-enter passphrase
            </Button>
          </div>
        ) : (
          <form className="settings-page__form" onSubmit={handleSubmit}>
            <p className="settings-page__hint">
              Enter the namespace passphrase shared by your group. It's used only on this
              browser and is never sent anywhere.
            </p>
            <TextField
              label="Namespace passphrase"
              type="password"
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              required
            />
            {error && <p className="settings-page__error">{error}</p>}
            <Button type="submit" disabled={submitting}>
              Unlock
            </Button>
          </form>
        )}
      </Card>
    </div>
  )
}
