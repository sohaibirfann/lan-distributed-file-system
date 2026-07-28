import type { ReactNode } from 'react'
import './Badge.css'

export type BadgeTone = 'success' | 'warning' | 'danger' | 'neutral'

export interface BadgeProps {
  tone?: BadgeTone
  children: ReactNode
}

/** A small status pill, e.g. for a node's state or an event's kind. */
export function Badge({ tone = 'neutral', children }: BadgeProps) {
  return <span className={`ds-badge ds-badge--${tone}`}>{children}</span>
}
