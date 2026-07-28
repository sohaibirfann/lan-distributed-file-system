import type { ButtonHTMLAttributes } from 'react'
import './Button.css'

export type ButtonVariant = 'primary' | 'secondary' | 'danger'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual style. Defaults to 'primary'. */
  variant?: ButtonVariant
}

/** A clickable action button in the app's primary, secondary, or danger style. */
export function Button({ variant = 'primary', className, ...rest }: ButtonProps) {
  const classes = ['ds-button', `ds-button--${variant}`, className].filter(Boolean).join(' ')
  return <button type="button" className={classes} {...rest} />
}
