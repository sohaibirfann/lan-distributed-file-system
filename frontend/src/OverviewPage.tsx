import { useEffect, useState } from 'react'
import { Badge, type BadgeTone } from './components/Badge/Badge'
import { StatTile } from './components/StatTile/StatTile'
import { Card } from './components/Card/Card'
import { formatBytes, formatRelativeTime } from './format'
import { getNodes, type Node } from './api'
import './OverviewPage.css'

const STATE_TONE: Record<Node['state'], BadgeTone> = {
  up: 'success',
  suspect: 'warning',
  down: 'danger',
  draining: 'neutral',
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
        <Card title="Nodes">
          {loading && <p className="ds-empty">Loading…</p>}
          {!loading && nodes.length === 0 && <p className="ds-empty">No nodes registered yet.</p>}
          {nodes.map((node) => (
            <div key={node.id} className="overview-page__row">
              <div className="overview-page__row-main">
                <span className="overview-page__row-title">{node.address}</span>
                <Badge tone={STATE_TONE[node.state]}>{node.state}</Badge>
              </div>
              <div className="ds-row-meta">
                <span>
                  {formatBytes(node.used_bytes)} / {formatBytes(node.effective_capacity_bytes)}
                </span>
                <span>heartbeat {formatRelativeTime(node.last_heartbeat_at)}</span>
              </div>
            </div>
          ))}
        </Card>

        <Card title="Recent Events">
          {events.length === 0 && <p className="ds-empty">No events yet.</p>}
          {events.slice(0, 20).map((event) => (
            <div key={event.id} className="overview-page__row">
              <div className="overview-page__row-main">
                <span className="overview-page__row-title">{event.message}</span>
              </div>
              <div className="ds-row-meta">
                <span>{formatRelativeTime(event.created_at)}</span>
              </div>
            </div>
          ))}
        </Card>
      </div>
    </div>
  )
}
