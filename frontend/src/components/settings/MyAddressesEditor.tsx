import { useState, type FormEvent, type JSX } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import {
  addMyAddress,
  removeMyAddress,
  type AddressKind,
  type PersonAddress,
} from '@/api/people'

// More ways to reach a person (apps/contacts/models.py `PersonAddress`): a
// labelled email or phone beside the login that identifies them — Gina's work
// email, say — which agents read when they act (invite her work address to the
// family calendar). They belong to the person's ACCOUNT, not a workspace, and
// only the person changes them, in account settings.

function errorText(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

/** One person's addresses; a Remove on each when they are yours. */
export function AddressList({
  addresses,
  who,
  onRemove,
}: {
  addresses: PersonAddress[]
  who: string
  onRemove?: (id: number) => void
}): JSX.Element {
  if (addresses.length === 0) return <p className="mt-2 text-[12px] text-muted-foreground">No other addresses.</p>
  return (
    <ul className="mt-2 flex list-none flex-col gap-1 p-0">
      {addresses.map((a) => (
        <li key={a.id} className="flex items-center gap-2 text-[12px]">
          <span className="w-20 shrink-0 capitalize text-muted-foreground">{a.label}</span>
          <code className="min-w-0 truncate rounded bg-muted px-1.5 py-0.5 text-foreground">{a.value}</code>
          <span className="text-muted-foreground">{a.kind}</span>
          {onRemove && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="ml-auto"
              aria-label={`Remove ${a.label} ${a.kind} for ${who}`}
              onClick={() => onRemove(a.id)}
            >
              Remove
            </Button>
          )}
        </li>
      ))}
    </ul>
  )
}

/** Your own addresses, editable: the list, Remove, and the add form. */
export function MyAddressesEditor({
  addresses,
  who,
  onChange,
}: {
  addresses: PersonAddress[]
  who: string
  onChange: (next: PersonAddress[]) => void
}): JSX.Element {
  const [error, setError] = useState<string | null>(null)

  async function remove(id: number) {
    setError(null)
    try {
      await removeMyAddress(id)
      onChange(addresses.filter((a) => a.id !== id))
    } catch (e) {
      setError(errorText(e, 'Failed to remove that address'))
    }
  }

  return (
    <>
      {error && <div className="mt-2 text-[12px] text-destructive">{error}</div>}
      <AddressList addresses={addresses} who={who} onRemove={(id) => void remove(id)} />
      <AddAddressForm
        who={who}
        onAdd={async (kind, label, value) => {
          const row = await addMyAddress({ kind, label, value })
          onChange([...addresses.filter((a) => a.id !== row.id), row])
        }}
      />
    </>
  )
}

function AddAddressForm({
  who,
  onAdd,
}: {
  who: string
  onAdd: (kind: AddressKind, label: string, value: string) => Promise<void>
}): JSX.Element {
  const [kind, setKind] = useState<AddressKind>('email')
  const [label, setLabel] = useState('work')
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!value.trim() || !label.trim()) return
    setBusy(true)
    setError(null)
    try {
      await onAdd(kind, label.trim(), value.trim())
      setValue('')
    } catch (err) {
      setError(errorText(err, 'Failed to add that address'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="mt-3 flex flex-wrap items-center gap-2">
      <Input
        aria-label={`Label for ${who}`}
        className="h-8 w-28 text-[12px]"
        placeholder="work"
        value={label}
        onChange={(e) => setLabel(e.target.value)}
      />
      <select
        aria-label={`Kind for ${who}`}
        className="h-8 rounded-md border border-border bg-background px-2 text-[12px]"
        value={kind}
        onChange={(e) => setKind(e.target.value as AddressKind)}
      >
        <option value="email">email</option>
        <option value="phone">phone</option>
      </select>
      <Input
        aria-label={`Address for ${who}`}
        className="h-8 min-w-0 flex-1 text-[12px]"
        type={kind === 'email' ? 'email' : 'tel'}
        placeholder={kind === 'email' ? 'name@work.example' : '+1 555 0100'}
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <Button type="submit" size="sm" variant="outline" disabled={busy || !value.trim() || !label.trim()}>
        {busy ? 'Adding…' : 'Add'}
      </Button>
      {error && <div className="w-full text-[12px] text-destructive">{error}</div>}
    </form>
  )
}
