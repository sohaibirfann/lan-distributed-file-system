import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import { Download, Pencil, Trash2, Upload } from 'lucide-react'
import { Card } from './components/Card/Card'
import { Button } from './components/Button/Button'
import {
  ApiError,
  createFile,
  deleteFile,
  getFileDetail,
  getFiles,
  getPlacement,
  getReplicationConfig,
  reportChunkUnavailable,
  renameFile,
  type FileDetail,
  type FileEntry,
} from './api'
import { getDerivedKey } from './namespaceKey'
import { ChunkUploadError, putChunkToNode, uploadFileChunks, type UploadedChunk } from './upload'
import {
  ChunkDownloadError,
  downloadFile,
  fetchChunkFromNode,
  prepareSaveTarget,
  type ChunkLocation,
} from './download'
import { DEFAULT_CHUNK_SIZE_BYTES } from './chunking'
import { formatBytes, formatRelativeTime } from './format'
import './FilesPage.css'

interface TransferState {
  fileName: string
  totalChunks: number
  completedChunks: number
  label: string
}

const FILE_DETAIL_CACHE_TTL_MS = 60_000
const fileDetailCache = new Map<number, { detail: FileDetail; cachedAt: number }>()

async function getCachedFileDetail(id: number): Promise<FileDetail> {
  const cached = fileDetailCache.get(id)
  if (cached && Date.now() - cached.cachedAt < FILE_DETAIL_CACHE_TTL_MS) return cached.detail
  const detail = await getFileDetail(id)
  fileDetailCache.set(id, { detail, cachedAt: Date.now() })
  return detail
}

// A deleted file's id can be reused, so its cache entry must not survive.
function invalidateFileDetailCache(id: number) {
  fileDetailCache.delete(id)
}

export function FilesPage() {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editingName, setEditingName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [transferState, setTransferState] = useState<TransferState | null>(null)
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
      invalidateFileDetailCache(file.id)
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
      invalidateFileDetailCache(existing.id)
      setFiles((current) => current.filter((f) => f.id !== existing.id))
    } catch {
      setError(
        `Upload succeeded, but the previous file could not be removed automatically -- delete "${existing.name}" manually.`,
      )
    }
  }

  function bumpCompletedChunks() {
    setTransferState((s) => (s ? { ...s, completedChunks: s.completedChunks + 1 } : s))
  }

  async function handleFileSelected(e: ChangeEvent<HTMLInputElement>) {
    const selected = e.target.files?.[0]
    e.target.value = ''
    if (!selected || transferState) return

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
    setTransferState({
      fileName: selected.name,
      totalChunks,
      completedChunks: 0,
      label: `Uploading ${selected.name}…`,
    })

    try {
      const { write_quorum } = await getReplicationConfig()
      const chunks = await uploadFileChunks(
        selected,
        key,
        { getPlacement, putChunk: putChunkToNode, writeQuorum: write_quorum },
        4,
        bumpCompletedChunks,
      )
      setTransferState((s) => (s ? { ...s, label: `Finalizing ${selected.name}…` } : s))
      await finalizeUpload(selected, chunks, existing)
    } catch (err) {
      if (err instanceof ChunkUploadError) setError(err.message)
      else setError(err instanceof ApiError ? err.message : 'upload failed')
    } finally {
      setTransferState(null)
    }
  }

  async function handleDownload(file: FileEntry) {
    if (transferState) return
    setError(null)

    let saveTarget
    try {
      // Called first to keep the click's user gesture -- showSaveFilePicker needs it.
      saveTarget = await prepareSaveTarget(file.name)
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') return // user cancelled the picker
      setError('could not start the download')
      return
    }

    const key = await getDerivedKey()
    if (!key) {
      setError('Unlock the namespace passphrase in Settings before downloading.')
      await saveTarget.abort()
      return
    }

    const detail = await getCachedFileDetail(file.id)
    if (detail.id !== file.id) {
      setError('File details are out of date -- try again.')
      await saveTarget.abort()
      return
    }
    const chunks: ChunkLocation[] = detail.chunks.map((c) => ({
      sequenceIndex: c.sequence_index,
      hash: c.hash,
      replicas: c.nodes.map((n) => ({ nodeId: n.node_id, address: n.address })),
    }))
    setTransferState({
      fileName: file.name,
      totalChunks: chunks.length,
      completedChunks: 0,
      label: `Downloading ${file.name}…`,
    })

    try {
      const decrypted = await downloadFile(
        chunks,
        key,
        { fetchChunk: fetchChunkFromNode, reportUnavailable: reportChunkUnavailable },
        4,
        bumpCompletedChunks,
      )
      setTransferState((s) => (s ? { ...s, label: `Saving ${file.name}…` } : s))
      await saveTarget.write(decrypted)
    } catch (err) {
      await saveTarget.abort()
      if (err instanceof ChunkDownloadError) setError(err.message)
      else setError(err instanceof ApiError ? err.message : 'download failed')
    } finally {
      setTransferState(null)
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
          disabled={transferState !== null}
          onClick={() => fileInputRef.current?.click()}
        >
          <Upload size={14} /> Upload
        </Button>
      </div>
      {transferState && (
        <div className="files-page__transfer-progress">
          <div className="files-page__transfer-progress-header">
            <span>{transferState.label}</span>
            <span>
              {transferState.completedChunks}/{transferState.totalChunks} chunks
            </span>
          </div>
          <div className="files-page__transfer-progress-bar">
            <div
              className="files-page__transfer-progress-fill"
              style={{
                width: `${Math.round((transferState.completedChunks / transferState.totalChunks) * 100)}%`,
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
              aria-label={`Download ${file.name}`}
              disabled={transferState !== null}
              onClick={() => handleDownload(file)}
            >
              <Download size={14} />
            </button>
            <button
              type="button"
              className="files-page__icon-button"
              aria-label={`Rename ${file.name}`}
              disabled={transferState !== null}
              onClick={() => startEditing(file)}
            >
              <Pencil size={14} />
            </button>
            <button
              type="button"
              className="files-page__icon-button"
              aria-label={`Delete ${file.name}`}
              disabled={transferState !== null}
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
