import { Download, Pencil, Trash2 } from 'lucide-react'
import type { FileEntry } from '../../lib/api'
import { formatBytes, formatRelativeTime } from '../../lib/format'

export interface FileRowProps {
  file: FileEntry
  editing: boolean
  editingName: string
  transferInProgress: boolean
  onEditingNameChange: (name: string) => void
  onCommitRename: () => void
  onCancelEdit: () => void
  onStartEditing: () => void
  onDownload: () => void
  onDelete: () => void
}

export function FileRow({
  file,
  editing,
  editingName,
  transferInProgress,
  onEditingNameChange,
  onCommitRename,
  onCancelEdit,
  onStartEditing,
  onDownload,
  onDelete,
}: FileRowProps) {
  return (
    <div className="files-page__row">
      <div className="files-page__row-main">
        {editing ? (
          <input
            className="files-page__rename-input"
            value={editingName}
            autoFocus
            onChange={(e) => onEditingNameChange(e.target.value)}
            onBlur={onCommitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onCommitRename()
              if (e.key === 'Escape') onCancelEdit()
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
          disabled={transferInProgress}
          onClick={onDownload}
        >
          <Download size={14} />
        </button>
        <button
          type="button"
          className="files-page__icon-button"
          aria-label={`Rename ${file.name}`}
          disabled={transferInProgress}
          onClick={onStartEditing}
        >
          <Pencil size={14} />
        </button>
        <button
          type="button"
          className="files-page__icon-button"
          aria-label={`Delete ${file.name}`}
          disabled={transferInProgress}
          onClick={onDelete}
        >
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  )
}
