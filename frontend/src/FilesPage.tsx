import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import { Pencil, Trash2, Upload } from 'lucide-react'
import { Card } from './components/Card/Card'
import { Button } from './components/Button/Button'
import {
  ApiError,
  createFile,
  deleteFile,
  getFiles,
  getPlacement,
  getReplicationConfig,
  renameFile,
  type FileEntry,
} from './api'
import { getDerivedKey } from './namespaceKey'
import { ChunkUploadError, putChunkToNode, uploadFileChunks, type UploadedChunk } from './upload'
import { DEFAULT_CHUNK_SIZE_BYTES } from './chunking'
import { formatBytes, formatRelativeTime } from './format'
import './FilesPage.css'

interface UploadState {
  fileName: string
  totalChunks: number
  completedChunks: number
  phase: 'uploading' | 'finalizing'
}

export function FilesPage() {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editingName, setEditingName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [uploadState, setUploadState] = useState<UploadState | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

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

  // Deleting the replaced file is best-effort: the new upload already succeeded.
  async function finalizeUpload(
    selected: File,
    chunks: UploadedChunk[],
    existing: FileEntry | undefined,
  ) {
    const created = await createFile({
      name: selected.name,
      size_bytes: selected.size,
      chunks: chunks.map((c) => ({
        sequence_index: c.sequenceIndex,
        hash: c.hash,
        size_bytes: c.sizeBytes,
        node_ids: c.nodeIds,
      })),
    })
    setFiles((current) => [...current.filter((f) => f.id !== created.id), created])

    if (!existing) return
    try {
      await deleteFile(existing.id)
      setFiles((current) => current.filter((f) => f.id !== existing.id))
    } catch {
      setError(
        `Upload succeeded, but the previous file could not be removed automatically -- delete "${existing.name}" manually.`,
      )
    }
  }

  async function handleFileSelected(e: ChangeEvent<HTMLInputElement>) {
    const selected = e.target.files?.[0]
    e.target.value = ''
    if (!selected || uploadState) return

    setError(null)
    const key = await getDerivedKey()
    if (!key) {
      setError('Unlock the namespace passphrase in Settings before uploading.')
      return
    }

    const existing = files.find((f) => f.name === selected.name)
    if (existing) {
      const confirmed = window.confirm(
        `Replace "${existing.name}"? The existing file will be deleted after the new upload finishes.`,
      )
      if (!confirmed) return
    }

    const totalChunks = Math.max(1, Math.ceil(selected.size / DEFAULT_CHUNK_SIZE_BYTES))
    setUploadState({ fileName: selected.name, totalChunks, completedChunks: 0, phase: 'uploading' })

    try {
      const { write_quorum } = await getReplicationConfig()
      const chunks = await uploadFileChunks(selected, key, { getPlacement, putChunk: putChunkToNode, writeQuorum: write_quorum }, 4, () =>
        setUploadState((s) => (s ? { ...s, completedChunks: s.completedChunks + 1 } : s)),
      )
      setUploadState((s) => (s ? { ...s, phase: 'finalizing' } : s))
      await finalizeUpload(selected, chunks, existing)
    } catch (err) {
      if (err instanceof ChunkUploadError) setError(err.message)
      else setError(err instanceof ApiError ? err.message : 'upload failed')
    } finally {
      setUploadState(null)
    }
  }

  return (
    <Card title="Files">
      <div className="files-page__upload-bar">
        <input
          ref={fileInputRef}
          type="file"
          hidden
          onChange={handleFileSelected}
        />
        <Button
          variant="secondary"
          disabled={uploadState !== null}
          onClick={() => fileInputRef.current?.click()}
        >
          <Upload size={14} /> Upload
        </Button>
      </div>
      {uploadState && (
        <div className="files-page__upload-progress">
          <div className="files-page__upload-progress-header">
            <span>
              {uploadState.phase === 'finalizing'
                ? `Finalizing ${uploadState.fileName}…`
                : `Uploading ${uploadState.fileName}…`}
            </span>
            <span>
              {uploadState.completedChunks}/{uploadState.totalChunks} chunks
            </span>
          </div>
          <div className="files-page__upload-progress-bar">
            <div
              className="files-page__upload-progress-fill"
              style={{
                width: `${Math.round((uploadState.completedChunks / uploadState.totalChunks) * 100)}%`,
              }}
            />
          </div>
        </div>
      )}
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
              disabled={uploadState !== null}
              onClick={() => startEditing(file)}
            >
              <Pencil size={14} />
            </button>
            <button
              type="button"
              className="files-page__icon-button"
              aria-label={`Delete ${file.name}`}
              disabled={uploadState !== null}
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
