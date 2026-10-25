import { useEffect, useState, type FormEvent, type JSX } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import {
  addPersonAddress,
  listWorkspaceAddresses,
  removePersonAddress,
  type AddressKind,
  type MemberAddresses,
} from '@/api/people'

// More ways to reach each member (apps/contacts/models.py `PersonAddress`): a
// labelled email or phone beside the login that identifies them — Gina's work
// email, say — which this workspace's agents read when they act (invite her
// work address to the family calendar). The person or a workspace admin adds
// them; an agent never does.

function errorText(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

export function ContactDetailsSection({ slug }: { slug: string }): JSX.Element {
  const [people, setPeople] = useState<MemberAddresses[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPeople(null)
    listWorkspaceAddresses(slug)
      .then((p) => {
        if (!cancelled) setPeople(p)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setPeople([])
          setError(errorText(e, 'Failed to load contact details'))
        }
      })
    return () => {
      cancelled = true
    }
  }, [slug])

  function replace(next: MemberAddresses) {
    setPeople((prev) => (prev ?? []).map((p) => (p.person_id === next.person_id ? next : p)))
  }

  async function remove(p: MemberAddresses, addressId: number) {
    setError(null)
    try {
      await removePersonAddress(p.person_id, addressId)
      replace({ ...p, addresses: p.addresses.filter((a) => a.id !== addressId) })
    } catch (e) {
      setError(errorText(e, 'Failed to remove that address'))
    }
  }

  return (
    <div className="mb-8" data-testid="contact-details">
      <WorkbenchSubHeader title="Contact details" count={people?.length} />
      <p className="mb-3 text-[12px] text-muted-foreground">
        Other ways to reach each person: a work email, a phone. This workspace’s agents use them when they act
        (inviting someone’s work address to a calendar event). Each person, or an admin, adds their own. Agents
        only read them, and every read shows on the person’s own page.
      </p>
      {error && <div className="mb-3 text-sm text-destructive">{error}</div>}
      {people === null ? (
        <div className="h-16 animate-pulse rounded-lg bg-muted" />
      ) : (
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {people.map((p) => (
            <li key={p.person_id} className="rounded-lg border border-border bg-card p-4" data-testid={`contact-${p.user_id}`}>
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-medium text-foreground">{p.display_name || p.email}</span>
                {p.display_name && p.display_name !== p.email && (
                  <span className="text-[12px] text-muted-foreground">{p.email}</span>
                )}
              </div>
              {p.addresses.length === 0 ? (
                <p className="mt-2 text-[12px] text-muted-foreground">No other addresses.</p>
              ) : (
                <ul className="mt-2 flex list-none flex-col gap-1 p-0">
                  {p.addresses.map((a) => (
                    <li key={a.id} className="flex items-center gap-2 text-[12px]">
                      <span className="w-20 shrink-0 capitalize text-muted-foreground">{a.label}</span>
                      <code className="min-w-0 truncate rounded bg-muted px-1.5 py-0.5 text-foreground">{a.value}</code>
                      <span className="text-muted-foreground">{a.kind}</span>
                      {p.may_edit && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="ml-auto"
                          aria-label={`Remove ${a.label} ${a.kind} for ${p.email}`}
                          onClick={() => void remove(p, a.id)}
                        >
                          Remove
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {p.may_edit && (
                <AddAddressForm
                  who={p.email}
                  onAdd={async (kind, label, value) => {
                    const row = await addPersonAddress(p.person_id, { workspace: slug, kind, label, value })
                    replace({ ...p, addresses: [...p.addresses.filter((a) => a.id !== row.id), row] })
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
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
