import { useEffect, useState } from 'react'
import { Bell, Clock, Search, WifiOff } from 'lucide-react'
import type { Event } from '../../api'
import './TopBar.css'

const CONNECTION_CHECK_INTERVAL_MS = 10_000

function useClock(): string {
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000 * 30)
    return () => clearInterval(id)
  }, [])

  return now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function useConnectionState(): 'online' | 'offline' {
  const [state, setState] = useState<'online' | 'offline'>('online')

  useEffect(() => {
    let cancelled = false
    async function check() {
      try {
        const response = await fetch('/health', { cache: 'no-store' })
        if (!cancelled) setState(response.ok ? 'online' : 'offline')
      } catch {
        if (!cancelled) setState('offline')
      }
    }
    check()
    const id = setInterval(check, CONNECTION_CHECK_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return state
}

function NotificationBell({ events }: { events: Event[] }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="ds-top-bar__notifications">
      <button
        type="button"
        className="ds-top-bar__icon-button"
        aria-label="Notifications"
        onClick={() => setOpen((o) => !o)}
      >
        <Bell size={16} />
        {events.length > 0 && (
          <span className="ds-top-bar__badge">{Math.min(events.length, 99)}</span>
        )}
      </button>
      {open && (
        <div className="ds-top-bar__panel">
          {events.length === 0 && (
            <p className="ds-top-bar__panel-empty">No events yet.</p>
          )}
          {events.slice(0, 8).map((event) => (
            <div key={event.id} className="ds-top-bar__panel-row">
              <span className="ds-top-bar__panel-message">{event.message}</span>
              <span className="ds-top-bar__panel-time">
                {new Date(event.created_at).toLocaleString([], {
                  month: 'short',
                  day: 'numeric',
                  hour: 'numeric',
                  minute: '2-digit',
                })}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export interface TopBarProps {
  title: string
  events: Event[]
}

/** The app's header bar: title, search, clock, and notifications. */
export function TopBar({ title, events }: TopBarProps) {
  const clock = useClock()
  const connection = useConnectionState()

  return (
    <header className="ds-top-bar">
      <h1 className="ds-top-bar__title">{title}</h1>
      <div className="ds-top-bar__actions">
        <div className="ds-top-bar__search">
          <Search size={16} />
          <input type="search" placeholder="Search" aria-label="Search" />
        </div>
        {connection === 'offline' ? (
          <div className="ds-top-bar__chip ds-top-bar__chip--offline">
            <WifiOff size={14} />
            Disconnected
          </div>
        ) : (
          <div className="ds-top-bar__chip">
            <Clock size={14} />
            {clock}
          </div>
        )}
        <NotificationBell events={events} />
      </div>
    </header>
  )
}
