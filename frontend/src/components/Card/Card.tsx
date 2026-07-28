import type { ReactNode } from 'react'
import './Card.css'

export interface CardProps {
  title: string
  children: ReactNode
}

/** A titled white card, the standard content container for dashboard pages. */
export function Card({ title, children }: CardProps) {
  return (
    <div className="ds-card">
      <h2 className="ds-card__title">{title}</h2>
      {children}
    </div>
  )
}
