import { useEffect, useState } from 'react'
import { AuthScreen } from './AuthScreen'
import { DashboardShell } from './DashboardShell'
import { Button } from './components/Button/Button'
import { logout, me, type Account } from './api'

function App() {
  const [account, setAccount] = useState<Account | null>(null)
  const [checkingSession, setCheckingSession] = useState(true)
  const [connectionError, setConnectionError] = useState(false)

  function checkSession() {
    setConnectionError(false)
    setCheckingSession(true)
    me()
      .then(setAccount)
      .catch(() => setConnectionError(true))
      .finally(() => setCheckingSession(false))
  }

  useEffect(checkSession, [])

  function handleLogout() {
    // Don't wait on the network call -- a failed request shouldn't strand the user.
    setAccount(null)
    logout().catch(() => {})
  }

  if (checkingSession) return null

  if (connectionError) {
    return (
      <div className="app-connection-error">
        <p>Can't reach the coordinator. Check your connection and try again.</p>
        <Button onClick={checkSession}>Retry</Button>
      </div>
    )
  }

  if (!account) {
    return <AuthScreen onAuthenticated={setAccount} />
  }

  return <DashboardShell account={account} onLogout={handleLogout} />
}

export default App
