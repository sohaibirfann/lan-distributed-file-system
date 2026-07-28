import { useId, type InputHTMLAttributes } from 'react'
import './TextField.css'

export interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Visible label text. */
  label: string
  /** Validation or helper message shown below the field. */
  error?: string
}

/** A labeled text input with an optional error message. */
export function TextField({ label, error, id, className, ...rest }: TextFieldProps) {
  const generatedId = useId()
  const inputId = id ?? generatedId

  return (
    <div className={['ds-text-field', className].filter(Boolean).join(' ')}>
      <label className="ds-text-field__label" htmlFor={inputId}>
        {label}
      </label>
      <input
        id={inputId}
        className="ds-text-field__input"
        aria-invalid={error ? true : undefined}
        {...rest}
      />
      {error && <span className="ds-text-field__error">{error}</span>}
    </div>
  )
}
