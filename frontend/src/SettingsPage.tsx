import { useEffect, useState, type FormEvent } from 'react'
import { Card } from './components/Card/Card'
import { Button } from './components/Button/Button'
import { TextField } from './components/TextField/TextField'
import { ApiError, getNamespaceSalt, getNamespaceVerifier, putNamespaceVerifier, type Account } from './api'
import { clearDerivedKey, createVerifier, deriveKey, setDerivedKey, verifyPassphrase } from './namespaceKey'
import './SettingsPage.css'

const UNLOCKED_KEY = 'dfs-namespace-unlocked'

export function SettingsPage({ account }: { account: Account }) {
  const [passphrase, setPassphrase] = useState('')
  const [confirmPassphrase, setConfirmPassphrase] = useState('')
  const [isFirstTimeSetup, setIsFirstTimeSetup] = useState(false)
  const [unlocked, setUnlocked] = useState(() => localStorage.getItem(UNLOCKED_KEY) === 'true')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (unlocked) return
    getNamespaceVerifier()
      .then(({ verifier }) => setIsFirstTimeSetup(verifier === null))
      .catch(() => {})
  }, [unlocked])

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (isFirstTimeSetup && passphrase !== confirmPassphrase) {
      setError('Passphrases do not match')
      return
    }
    setSubmitting(true)
    try {
      const { salt } = await getNamespaceSalt()
      const key = await deriveKey(passphrase, salt)

      let { verifier } = await getNamespaceVerifier()
      if (verifier === null) {
        const blob = await createVerifier(key)
        try {
          await putNamespaceVerifier(blob)
        } catch (err) {
          if (!(err instanceof ApiError) || err.status !== 409) throw err
          // Someone else set it first -- validate against theirs.
          verifier = (await getNamespaceVerifier()).verifier
        }
      }
      if (verifier !== null && !(await verifyPassphrase(key, verifier))) {
        setError('Incorrect namespace passphrase')
        return
      }

      await setDerivedKey(key)
      localStorage.setItem(UNLOCKED_KEY, 'true')
      setUnlocked(true)
      setPassphrase('')
      setConfirmPassphrase('')
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
                clearDerivedKey()
                setUnlocked(false)
              }}
            >
              Re-enter passphrase
            </Button>
          </div>
        ) : (
          <form className="settings-page__form" onSubmit={handleSubmit}>
            <p className="settings-page__hint">
              {isFirstTimeSetup
                ? "You're the first to set this up -- choose the passphrase your group will share. It's used only on this browser and is never sent anywhere, so a typo here can't be corrected later."
                : "Enter the namespace passphrase shared by your group. It's used only on this browser and is never sent anywhere."}
            </p>
            <TextField
              label="Namespace passphrase"
              type="password"
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              required
            />
            {isFirstTimeSetup && (
              <TextField
                label="Confirm passphrase"
                type="password"
                value={confirmPassphrase}
                onChange={(e) => setConfirmPassphrase(e.target.value)}
                required
              />
            )}
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
