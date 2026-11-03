import { useState } from 'react'

/** A token's or key's name, edited in place. Neither can say whose it is, so a
 *  person names it. */
export function CredentialName({ entryKey, name, disabled, onSave }: {
  entryKey: string
  name: string
  disabled: boolean
  onSave: (name: string) => Promise<boolean>
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(name)

  const commit = async () => {
    if (value.trim() === name) { setEditing(false); return }
    if (await onSave(value.trim())) setEditing(false)
  }

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => { setValue(name); setEditing(true) }}
        data-testid={`cred-name-${entryKey}`}
        title="Rename"
        className={`truncate rounded px-1 text-left hover:bg-muted ${
          name ? 'text-foreground-secondary' : 'text-[11px] italic text-foreground-subtle'
        }`}
      >
        {name || 'add a name'} <span className="text-foreground-subtle">✎</span>
      </button>
    )
  }
  return (
    <input
      autoFocus
      value={value}
      disabled={disabled}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => void commit()}
      onKeyDown={(e) => {
        if (e.key === 'Enter') void commit()
        if (e.key === 'Escape') setEditing(false)
      }}
      placeholder="e.g. ZDR org key"
      maxLength={200}
      data-testid={`cred-name-input-${entryKey}`}
      className="min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-0.5 text-[12px] text-foreground placeholder:text-muted-foreground"
    />
  )
}

export const inputClass =
  'min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground'

export const saveClass =
  'rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40'

/** Paste a new value over an API key — or its first value in this runner's vault, for
 *  an entry of a shared list that has none in this workspace tree. Hidden until asked for. */
export function ReplaceKey({ entryKey, disabled, isSet = true, onSave }: {
  entryKey: string
  disabled: boolean
  isSet?: boolean
  onSave: (value: string) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState('')
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} data-testid={`cred-replace-${entryKey}`}
        className="w-fit rounded-md border border-border px-2 py-1 text-xs hover:bg-muted">
        {isSet ? 'Replace key' : 'Add its key'}
      </button>
    )
  }
  return (
    <div className="flex gap-2">
      <input type="password" autoComplete="off" autoFocus value={value} onChange={(e) => setValue(e.target.value)}
        placeholder="paste the new key" aria-label="New API key" className={inputClass} />
      <button type="button" disabled={disabled || !value.trim()} className={saveClass}
        onClick={async () => {
          // Never keep a secret in component state after it lands.
          if (await onSave(value.trim())) { setValue(''); setOpen(false) }
        }}>
        Save
      </button>
      <button type="button" onClick={() => { setValue(''); setOpen(false) }} className={smallButton}>Cancel</button>
    </div>
  )
}

/** Paste a new API key, with an optional name, at the end of the list. */
export function NewApiKey({ disabled, onSave, onCancel }: {
  disabled: boolean
  onSave: (value: string, name: string) => Promise<boolean>
  onCancel: () => void
}) {
  const [value, setValue] = useState('')
  const [name, setName] = useState('')
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-dashed border-border p-2" data-testid="cred-new-api-key">
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="name (optional), e.g. ZDR org key"
        maxLength={200} aria-label="Name" className={inputClass.replace('font-mono ', '')} />
      <div className="flex gap-2">
        <input type="password" autoComplete="off" autoFocus value={value} onChange={(e) => setValue(e.target.value)}
          placeholder="paste the API key" aria-label="API key" className={inputClass} />
        <button type="button" disabled={disabled || !value.trim()} data-testid="cred-new-api-key-save" className={saveClass}
          onClick={async () => {
            if (await onSave(value.trim(), name.trim())) { setValue(''); setName('') }
          }}>
          Add
        </button>
        <button type="button" onClick={onCancel} className={smallButton}>Cancel</button>
      </div>
    </div>
  )
}

export const smallButton =
  'rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30'
