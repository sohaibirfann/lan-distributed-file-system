export interface TransferProgressProps {
  label: string
  completedChunks: number
  totalChunks: number
}

export function TransferProgress({ label, completedChunks, totalChunks }: TransferProgressProps) {
  return (
    <div className="files-page__transfer-progress">
      <div className="files-page__transfer-progress-header">
        <span>{label}</span>
        <span>
          {completedChunks}/{totalChunks} chunks
        </span>
      </div>
      <div className="files-page__transfer-progress-bar">
        <div
          className="files-page__transfer-progress-fill"
          style={{ width: `${Math.round((completedChunks / totalChunks) * 100)}%` }}
        />
      </div>
    </div>
  )
}
