import { useEffect, useState } from 'react'
import { Pencil, Trash2 } from 'lucide-react'
import { Card } from './components/Card/Card'
import { ApiError, deleteFile, getFiles, renameFile, type FileEntry } from './api'
import { formatBytes, formatRelativeTime } from './format'
import './FilesPage.css'

export function FilesPage() {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editingName, setEditingName] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getFiles()
      .then(setFiles)
      .finally(() => setLoading(false))
  }, [])

  function startEditing(file: FileEntry) {
    setError(null)
    setEditingId(file.id)
    setEditingName(file.name)
  }

  async function commitRename(id: number) {
    const name = editingName.trim()
    setEditingId(null)
    if (!name) return
    try {
      const updated = await renameFile(id, name)
      setFiles((current) => current.map((f) => (f.id === id ? updated : f)))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'could not rename file')
      setFiles(await getFiles())
    }
  }

  async function handleDelete(file: FileEntry) {
    if (!window.confirm(`Delete "${file.name}"? This cannot be undone.`)) return
    setError(null)
    try {
      await deleteFile(file.id)
      setFiles((current) => current.filter((f) => f.id !== file.id))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'could not delete file')
      setFiles(await getFiles())
    }
  }

  return (
    <Card title="Files">
      {error && <p className="files-page__error">{error}</p>}
      {loading && <p className="ds-empty">Loading…</p>}
      {!loading && files.length === 0 && <p className="ds-empty">No files uploaded yet.</p>}
      {files.map((file) => (
        <div key={file.id} className="files-page__row">
          <div className="files-page__row-main">
            {editingId === file.id ? (
              <input
                className="files-page__rename-input"
                value={editingName}
                autoFocus
                onChange={(e) => setEditingName(e.target.value)}
                onBlur={() => commitRename(file.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitRename(file.id)
                  if (e.key === 'Escape') setEditingId(null)
                }}
              />
            ) : (
              <span className="files-page__row-title">{file.name}</span>
            )}
          </div>
          <div className="ds-row-meta">
            <span>{formatBytes(file.size_bytes)}</span>
            <span>
              {file.chunk_count} {file.chunk_count === 1 ? 'chunk' : 'chunks'}
            </span>
            <span>uploaded {formatRelativeTime(file.created_at)}</span>
          </div>
          <div className="files-page__row-actions">
            <button
              type="button"
              className="files-page__icon-button"
              aria-label={`Rename ${file.name}`}
              onClick={() => startEditing(file)}
            >
              <Pencil size={14} />
            </button>
            <button
              type="button"
              className="files-page__icon-button"
              aria-label={`Delete ${file.name}`}
              onClick={() => handleDelete(file)}
            >
              <Trash2 size={14} />
            </button>
          </div>
        </div>
      ))}
    </Card>
  )
}
