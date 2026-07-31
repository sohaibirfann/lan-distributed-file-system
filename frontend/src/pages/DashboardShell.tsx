import { useEffect, useState } from 'react'
import { Sidebar, type SidebarView } from '../components/Sidebar/Sidebar'
import { TopBar } from '../components/TopBar/TopBar'
import { OverviewPage } from './OverviewPage'
import { FilesPage } from './FilesPage'
import { SettingsPage } from './SettingsPage'
import { getEvents, type Account, type Event } from '../lib/api'
import './DashboardShell.css'

const TITLES: Record<SidebarView, string> = {
  overview: 'Overview',
  files: 'Files',
  settings: 'Settings',
}

export function DashboardShell({
  account,
  onLogout,
}: {
  account: Account
  onLogout: () => void
}) {
  const [view, setView] = useState<SidebarView>('overview')
  const [events, setEvents] = useState<Event[]>([])

  useEffect(() => {
    getEvents().then(setEvents).catch(() => {})
  }, [])

  return (
    <div className="dashboard-shell">
      <Sidebar active={view} onSelect={setView} />
      <div className="dashboard-shell__main">
        <TopBar title={TITLES[view]} events={events} />
        <div className="dashboard-shell__content">
          {view === 'overview' && <OverviewPage events={events} />}
          {view === 'files' && <FilesPage />}
          {view === 'settings' && <SettingsPage account={account} onLogout={onLogout} />}
        </div>
      </div>
    </div>
  )
}
