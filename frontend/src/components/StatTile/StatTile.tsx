import type { ReactNode } from 'react'
import './StatTile.css'

export interface StatTileProps {
  label: string
  value: ReactNode
  description?: string
  /** Solid accent fill, for the one tile that should stand out. */
  highlighted?: boolean
}

/** A card showing one number: a label, the value, and an optional description. */
export function StatTile({ label, value, description, highlighted = false }: StatTileProps) {
  const classes = ['ds-stat-tile', highlighted && 'ds-stat-tile--highlighted']
    .filter(Boolean)
    .join(' ')

  return (
    <div className={classes}>
      <span className="ds-stat-tile__label">{label}</span>
      <span className="ds-stat-tile__value">{value}</span>
      {description && <span className="ds-stat-tile__description">{description}</span>}
    </div>
  )
}
