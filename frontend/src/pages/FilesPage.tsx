import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import { Upload } from 'lucide-react'
import { Card } from '../components/Card/Card'
import { Button } from '../components/Button/Button'
import { FileRow } from '../components/FileRow/FileRow'
import { TransferProgress } from '../components/TransferProgress/TransferProgress'
import {
  ApiError,
  createFile,
  deleteFile,
  getFiles,
  getPlacement,
  getReplicationConfig,
  reportChunkUnavailable,
  renameFile,
  type FileEntry,
} from '../lib/api'
import { getCachedFileDetail, invalidateFileDetailCache } from '../lib/fileDetailCache'
import { getDerivedKey } from '../lib/namespaceKey'
import { ChunkUploadError, putChunkToNode, uploadFileChunks, type UploadedChunk } from '../lib/upload'
import {
  ChunkDownloadError,
  downloadFile,
  fetchChunkFromNode,
  prepareSaveTarget,
  type ChunkLocation,
} from '../lib/download'
import { DEFAULT_CHUNK_SIZE_BYTES } from '../lib/chunking'
import './FilesPage.css'

interface TransferState {
  fileName: string
  totalChunks: number
  completedChunks: number
  label: string
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
      .catch(() => setError('could not load files'))
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
        {
          getPlacement: async (chunkId, exclude) =>
            (await getPlacement(chunkId, exclude)).map((n) => ({
              id: n.id,
              address: n.address,
              chunkToken: n.chunk_token,
            })),
          putChunk: putChunkToNode,
          writeQuorum: write_quorum,
        },
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
      replicas: c.nodes.map((n) => ({ nodeId: n.node_id, address: n.address, token: n.chunk_token })),
    }))
    setTransferState({
      fileName: file.name,
      totalChunks: chunks.length,
      completedChunks: 0,
      label: `Downloading ${file.name}…`,
    })

    try {
      await downloadFile(
        chunks,
        key,
        { fetchChunk: fetchChunkFromNode, reportUnavailable: reportChunkUnavailable },
        saveTarget,
        4,
        bumpCompletedChunks,
      )
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
        <TransferProgress
          label={transferState.label}
          completedChunks={transferState.completedChunks}
          totalChunks={transferState.totalChunks}
        />
      )}
      {error && <p className="files-page__error">{error}</p>}
      {loading && <p className="ds-empty">Loading…</p>}
      {!loading && !error && files.length === 0 && <p className="ds-empty">No files uploaded yet.</p>}
      {files.map((file) => (
        <FileRow
          key={file.id}
          file={file}
          editing={editingId === file.id}
          editingName={editingName}
          transferInProgress={transferState !== null}
          onEditingNameChange={setEditingName}
          onCommitRename={() => commitRename(file.id)}
          onCancelEdit={() => setEditingId(null)}
          onStartEditing={() => startEditing(file)}
          onDownload={() => handleDownload(file)}
          onDelete={() => handleDelete(file)}
        />
      ))}
    </Card>
  )
}
