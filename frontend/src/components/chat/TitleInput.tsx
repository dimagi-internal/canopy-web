import { useEffect, useRef, useState } from 'react'
import { clsx } from 'clsx'

/**
 * A session title being renamed in place. Enter or leaving the field saves,
 * Escape cancels; saving an unchanged title does nothing. Blank is allowed —
 * the server then names the session the way it names an untitled one.
 */
export function TitleInput({
  initial,
  onSave,
  onCancel,
  className,
}: {
  initial: string
  onSave: (title: string) => Promise<void>
  onCancel: () => void
  className?: string
}) {
  const [value, setValue] = useState(initial)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLInputElement>(null)
  // Enter saves and the blur that follows must not save a second time.
  const done = useRef(false)

  useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])

  const save = async () => {
    if (done.current) return
    const next = value.trim()
    if (next === initial.trim()) {
      done.current = true
      onCancel()
      return
    }
    done.current = true
    setSaving(true)
    try {
      await onSave(next)
    } catch (e) {
      done.current = false
      setSaving(false)
      setError(e instanceof Error ? e.message : 'Could not rename')
      ref.current?.focus()
    }
  }

  return (
    <input
      ref={ref}
      value={value}
      maxLength={200}
      disabled={saving}
      aria-label="Session title"
      placeholder="Name this session"
      title={error ?? 'Enter to save · Esc to cancel'}
      data-testid="session-title-input"
      onChange={(e) => {
        setValue(e.target.value)
        setError(null)
      }}
      onKeyDown={(e) => {
        // Kept to this field: Escape here cancels the rename, it must not also
        // reach a page-level handler and stop the agent.
        e.stopPropagation()
        if (e.key === 'Enter') {
          e.preventDefault()
          void save()
        } else if (e.key === 'Escape') {
          e.preventDefault()
          done.current = true
          onCancel()
        }
      }}
      onBlur={() => void save()}
      onClick={(e) => e.preventDefault()}
      className={clsx(
        'min-w-0 rounded border bg-background px-1 text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring',
        error ? 'border-destructive' : 'border-input',
        className,
      )}
    />
  )
}
