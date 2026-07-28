import { useEffect, useState } from 'react'
import { Badge, type BadgeTone } from './components/Badge/Badge'
import { StatTile } from './components/StatTile/StatTile'
import { getNodes, type Node } from './api'
import './OverviewPage.css'

const STATE_TONE: Record<Node['state'], BadgeTone> = {
  up: 'success',
  suspect: 'warning',
  down: 'danger',
  draining: 'neutral',
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = -1
  do {
    value /= 1024
    unit += 1
  } while (value >= 1024 && unit < units.length - 1)
  return `${value.toFixed(1)} ${units[unit]}`
}

function formatRelativeTime(iso: string): string {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export interface OverviewPageProps {
  events: { id: number; created_at: string; kind: string; message: string }[]
}

export function OverviewPage({ events }: OverviewPageProps) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getNodes()
      .then(setNodes)
      .finally(() => setLoading(false))
  }, [])

  const up = nodes.filter((n) => n.state === 'up').length
  const atRisk = nodes.filter((n) => n.state === 'suspect' || n.state === 'down').length

  return (
    <div className="overview-page">
      <div className="overview-page__stats">
        <StatTile
          highlighted
          label="Total Nodes"
          value={nodes.length}
          description="Registered in this namespace"
        />
        <StatTile label="Healthy" value={up} description="Nodes reporting normally" />
        <StatTile label="At Risk" value={atRisk} description="Suspect or down" />
        <StatTile
          label="Recent Events"
          value={events.length}
          description="Most recent activity"
        />
      </div>

      <div className="overview-page__panels">
        <div className="overview-page__card">
          <h2 className="overview-page__card-title">Nodes</h2>
          {loading && <p className="overview-page__empty">Loading…</p>}
          {!loading && nodes.length === 0 && (
            <p className="overview-page__empty">No nodes registered yet.</p>
          )}
          {nodes.map((node) => (
            <div key={node.id} className="overview-page__row">
              <div className="overview-page__row-main">
                <span className="overview-page__row-title">{node.address}</span>
                <Badge tone={STATE_TONE[node.state]}>{node.state}</Badge>
              </div>
              <div className="overview-page__row-meta">
                <span>
                  {formatBytes(node.used_bytes)} / {formatBytes(node.effective_capacity_bytes)}
                </span>
                <span>heartbeat {formatRelativeTime(node.last_heartbeat_at)}</span>
              </div>
            </div>
          ))}
        </div>

        <div className="overview-page__card">
          <h2 className="overview-page__card-title">Recent Events</h2>
          {events.length === 0 && <p className="overview-page__empty">No events yet.</p>}
          {events.slice(0, 20).map((event) => (
            <div key={event.id} className="overview-page__row">
              <div className="overview-page__row-main">
                <span className="overview-page__row-title">{event.message}</span>
              </div>
              <div className="overview-page__row-meta">
                <span>{formatRelativeTime(event.created_at)}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
