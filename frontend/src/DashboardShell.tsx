import { useEffect, useState } from 'react'
import { Sidebar, type SidebarView } from './components/Sidebar/Sidebar'
import { TopBar } from './components/TopBar/TopBar'
import { OverviewPage } from './OverviewPage'
import { getEvents, type Event } from './api'
import './DashboardShell.css'

const TITLES: Record<SidebarView, string> = {
  overview: 'Overview',
  files: 'Files',
  settings: 'Settings',
}

export function DashboardShell() {
  const [view, setView] = useState<SidebarView>('overview')
  const [events, setEvents] = useState<Event[]>([])

  useEffect(() => {
    getEvents().then(setEvents)
  }, [])

  return (
    <div className="dashboard-shell">
      <Sidebar active={view} onSelect={setView} />
      <div className="dashboard-shell__main">
        <TopBar title={TITLES[view]} events={events} />
        <div className="dashboard-shell__content">
          {view === 'overview' && <OverviewPage events={events} />}
          {view === 'files' && <p>Coming soon.</p>}
          {view === 'settings' && <p>Coming soon.</p>}
        </div>
      </div>
    </div>
  )
}
