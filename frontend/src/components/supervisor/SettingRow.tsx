import type { JSX, ReactNode } from 'react'

// One line of a runner's Settings list: what it is and what it does now on the left,
// its control on the right. Settings that are a plain choice (on/off, or one of a
// few) are rows in that one list; anything that needs more than a click — Claude
// logins, administrators, workspaces — gets its own panel below it.
export function SettingRow({
  label,
  help,
  control,
  children,
  testId,
}: {
  label: string
  /** What the current value means, in a sentence. */
  help?: ReactNode
  control: ReactNode
  /** Extra lines under the row: a warning, an error, a follow-up field. */
  children?: ReactNode
  testId?: string
}): JSX.Element {
  return (
    <div className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0" data-testid={testId}>
      <div className="flex items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-[13px] font-medium text-foreground">{label}</span>
          {help && <span className="text-[12px] text-muted-foreground">{help}</span>}
        </div>
        <div className="shrink-0">{control}</div>
      </div>
      {children}
    </div>
  )
}

export function Switch({
  checked,
  onChange,
  disabled,
  label,
  testId,
}: {
  checked: boolean
  onChange: () => void
  disabled?: boolean
  /** The accessible name — the row's label. */
  label: string
  testId?: string
}): JSX.Element {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onChange}
      data-testid={testId}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors disabled:opacity-50 ${
        checked ? 'bg-primary' : 'bg-muted-foreground/40'
      }`}
    >
      <span
        className={`inline-block h-4 w-4 rounded-full bg-background shadow transition-transform ${
          checked ? 'translate-x-[18px]' : 'translate-x-0.5'
        }`}
      />
    </button>
  )
}

// One of a few named values, for a setting that is a choice rather than on/off.
export function Choice<T extends string>({
  value,
  options,
  onChange,
  saving,
  label,
  testIdPrefix,
}: {
  value: T
  options: Record<T, string>
  onChange: (v: T) => void
  saving: T | null
  label: string
  testIdPrefix: string
}): JSX.Element {
  return (
    <div className="flex flex-wrap justify-end gap-1" role="radiogroup" aria-label={label}>
      {(Object.keys(options) as T[]).map((o) => (
        <button
          key={o}
          type="button"
          role="radio"
          aria-checked={o === value}
          disabled={saving !== null}
          onClick={() => o !== value && onChange(o)}
          data-testid={`${testIdPrefix}-${o}`}
          className={`rounded-md px-2 py-0.5 text-[12px] font-medium disabled:opacity-50 ${
            o === value ? 'bg-primary text-primary-foreground' : 'border border-border text-foreground hover:bg-muted'
          }`}
        >
          {saving === o ? '…' : options[o]}
        </button>
      ))}
    </div>
  )
}
