import { useEffect, useState } from 'react'
import { AuthScreen } from './AuthScreen'
import { me, type Account } from './api'

function App() {
  const [account, setAccount] = useState<Account | null>(null)
  const [checkingSession, setCheckingSession] = useState(true)

  useEffect(() => {
    me()
      .then(setAccount)
      .finally(() => setCheckingSession(false))
  }, [])

  if (checkingSession) return null

  if (!account) {
    return <AuthScreen onAuthenticated={setAccount} />
  }

  return <p>Signed in as {account.username}.</p>
}

export default App
