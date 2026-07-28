import { FolderClosed, LayoutGrid, Settings } from 'lucide-react'
import './Sidebar.css'

export type SidebarView = 'overview' | 'files' | 'settings'

const NAV_ITEMS: { id: SidebarView; label: string; icon: typeof LayoutGrid }[] = [
  { id: 'overview', label: 'Overview', icon: LayoutGrid },
  { id: 'files', label: 'Files', icon: FolderClosed },
]

export interface SidebarProps {
  active: SidebarView
  onSelect: (view: SidebarView) => void
}

/** The app's icon-only left navigation rail. */
export function Sidebar({ active, onSelect }: SidebarProps) {
  return (
    <nav className="ds-sidebar">
      <div className="ds-sidebar__group">
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className={`ds-sidebar__item ${active === id ? 'ds-sidebar__item--active' : ''}`}
            aria-label={label}
            aria-current={active === id}
            onClick={() => onSelect(id)}
          >
            <Icon size={18} />
          </button>
        ))}
      </div>
      <div className="ds-sidebar__group">
        <button
          type="button"
          className={`ds-sidebar__item ${active === 'settings' ? 'ds-sidebar__item--active' : ''}`}
          aria-label="Settings"
          aria-current={active === 'settings'}
          onClick={() => onSelect('settings')}
        >
          <Settings size={18} />
        </button>
      </div>
    </nav>
  )
}
