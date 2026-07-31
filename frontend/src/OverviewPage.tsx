import { useEffect, useState } from 'react'
import { LogOut } from 'lucide-react'
import { Badge, type BadgeTone } from './components/Badge/Badge'
import { StatTile } from './components/StatTile/StatTile'
import { Card } from './components/Card/Card'
import { formatBytes, formatRelativeTime } from './format'
import {
  ApiError,
  drainNode,
  getFiles,
  getNodes,
  getReplicationHealth,
  type ChunkHealth,
  type FileEntry,
  type Node,
} from './api'
import './OverviewPage.css'

const STATE_TONE: Record<Node['state'], BadgeTone> = {
  up: 'success',
  suspect: 'warning',
  down: 'danger',
  draining: 'neutral',
}

const HEALTH_TONE: Record<ChunkHealth['status'], BadgeTone> = {
  under_replicated: 'warning',
  unavailable: 'danger',
}

const HEALTH_LABEL: Record<ChunkHealth['status'], string> = {
  under_replicated: 'under-replicated',
  unavailable: 'unavailable',
}

export interface OverviewPageProps {
  events: { id: number; created_at: string; kind: string; message: string }[]
}

export function OverviewPage({ events }: OverviewPageProps) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [files, setFiles] = useState<FileEntry[]>([])
  const [health, setHealth] = useState<ChunkHealth[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drainingAddresses, setDrainingAddresses] = useState<Set<string>>(new Set())

  useEffect(() => {
    Promise.all([getNodes(), getFiles(), getReplicationHealth()])
      .then(([n, f, h]) => {
        setNodes(n)
        setFiles(f)
        setHealth(h)
      })
      .catch(() => setLoadError(true))
      .finally(() => setLoading(false))
  }, [])

  async function refreshNodesQuietly() {
    try {
      setNodes(await getNodes())
    } catch {
      // Keep whatever drain result was already shown; a refresh failure isn't a drain failure.
    }
  }

  async function handleDrain(node: Node) {
    if (!window.confirm(`Drain ${node.address}? The coordinator will try to move its chunks to other nodes.`)) return
    setError(null)
    setDrainingAddresses((current) => new Set(current).add(node.address))
    try {
      const { remaining_chunks } = await drainNode(node.address)
      if (remaining_chunks > 0) {
        setError(
          `${node.address}: ${remaining_chunks} chunk(s) could not be moved yet (no spare capacity).`,
        )
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'could not drain node')
    } finally {
      setDrainingAddresses((current) => {
        const next = new Set(current)
        next.delete(node.address)
        return next
      })
    }
    await refreshNodesQuietly()
  }

  const up = nodes.filter((n) => n.state === 'up').length
  const atRisk = nodes.filter((n) => n.state === 'suspect' || n.state === 'down').length

  const totalChunks = files.reduce((sum, f) => sum + f.chunk_count, 0)
  const underReplicated = health.filter((h) => h.status === 'under_replicated').length
  const unavailable = health.filter((h) => h.status === 'unavailable').length
  const fullyReplicated = totalChunks - underReplicated - unavailable
  const filesById = new Map(files.map((f) => [f.id, f]))

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
        <StatTile
          label="Replication"
          value={`${fullyReplicated}/${totalChunks}`}
          description="Chunks at full replication factor"
        />
      </div>

      <div className="overview-page__panels">
        <Card title="Nodes">
          {loading && <p className="ds-empty">Loading…</p>}
          {!loading && !loadError && nodes.length === 0 && <p className="ds-empty">No nodes registered yet.</p>}
          {loadError && <p className="ds-empty">Could not load nodes.</p>}
          {error && <p className="overview-page__error">{error}</p>}
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
              <div className="overview-page__row-actions">
                <button
                  type="button"
                  className="overview-page__icon-button"
                  aria-label={`Drain ${node.address}`}
                  disabled={node.draining || drainingAddresses.has(node.address)}
                  onClick={() => handleDrain(node)}
                >
                  <LogOut size={14} />
                </button>
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

        <Card title="Replication Health">
          {!loading && !loadError && health.length === 0 && (
            <p className="ds-empty">All chunks are at full replication factor.</p>
          )}
          {loadError && <p className="ds-empty">Could not load replication health.</p>}
          {health.map((chunk) => (
            <div key={chunk.chunk_id} className="overview-page__row">
              <div className="overview-page__row-main">
                <span className="overview-page__row-title">
                  {filesById.get(chunk.file_id)?.name ?? `file ${chunk.file_id}`} — chunk{' '}
                  {chunk.sequence_index}
                </span>
                <Badge tone={HEALTH_TONE[chunk.status]}>{HEALTH_LABEL[chunk.status]}</Badge>
              </div>
              <div className="ds-row-meta">
                <span>
                  {chunk.healthy_node_ids.length}/{chunk.replication_factor} replicas healthy
                </span>
              </div>
            </div>
          ))}
        </Card>
      </div>
    </div>
  )
}
