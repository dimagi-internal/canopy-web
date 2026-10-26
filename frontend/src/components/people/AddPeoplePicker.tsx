import { useId, useMemo, useState, type FormEvent, type JSX, type KeyboardEvent } from 'react'
import { Bot, X } from 'lucide-react'
import { Button, Input } from 'canopy-ui/ui'

import { RoleSelect, ROLE_TEXT_CLASS } from './RoleSelect'
import type { RoleOption } from './roles'

// Add people from a list the server says you may add — never a typed address.
// Type to narrow, pick one or several (Enter or click), choose a role, Add.
// The caller supplies the candidates and does the adding; this only picks.

export interface Candidate {
  user_id: number
  email: string
  display_name: string
  /** An agent's own login: its slug. */
  agent_slug?: string
}

function matches(c: Candidate, q: string): boolean {
  if (!q) return true
  const hay = `${c.display_name} ${c.email} ${c.agent_slug ?? ''}`.toLowerCase()
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w))
}

function errorText(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

export function AddPeoplePicker({
  candidates,
  options,
  defaultRole,
  onAdd,
  autoFocus = false,
  loading = false,
}: {
  candidates: readonly Candidate[]
  options: RoleOption[]
  defaultRole?: string
  /** Add one person; called once per pick, in order. Rejecting stops there and shows why. */
  onAdd: (email: string, role: string) => Promise<unknown>
  autoFocus?: boolean
  loading?: boolean
}): JSX.Element {
  const uid = useId()
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<Candidate[]>([])
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [role, setRole] = useState(defaultRole ?? options[0]?.value ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const current = options.some((o) => o.value === role) ? role : (options[0]?.value ?? '')

  const pickedIds = useMemo(() => new Set(picked.map((p) => p.user_id)), [picked])
  const shown = useMemo(
    () => candidates.filter((c) => !pickedIds.has(c.user_id) && matches(c, query.trim())).slice(0, 8),
    [candidates, pickedIds, query],
  )

  const pick = (c: Candidate) => {
    setPicked((prev) => [...prev, c])
    setQuery('')
    setActive(0)
  }
  const unpick = (id: number) => setPicked((prev) => prev.filter((p) => p.user_id !== id))

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setOpen(true)
      setActive((i) => Math.min(i + 1, Math.max(shown.length - 1, 0)))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      // Enter picks the highlighted person; it never submits what was typed.
      e.preventDefault()
      if (open && shown[active]) pick(shown[active])
    } else if (e.key === 'Escape') {
      setOpen(false)
    } else if (e.key === 'Backspace' && !query && picked.length) {
      unpick(picked[picked.length - 1].user_id)
    }
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!picked.length || busy) return
    setBusy(true)
    setError(null)
    try {
      for (const p of picked) {
        await onAdd(p.email, current)
        setPicked((prev) => prev.filter((x) => x.user_id !== p.user_id))
      }
    } catch (e2) {
      setError(errorText(e2, 'That did not work.'))
    } finally {
      setBusy(false)
    }
  }

  const listId = `${uid}-list`
  const empty = !loading && candidates.length === 0

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-1">
      <div className="flex flex-wrap items-end gap-2">
        <div className="relative min-w-[12rem] flex-1">
          <label htmlFor={`${uid}-q`} className="mb-1 block text-[11px] text-muted-foreground">
            People
          </label>
          {picked.length > 0 && (
            <ul className="mb-1 flex flex-wrap gap-1" aria-label="Picked">
              {picked.map((p) => (
                <li key={p.user_id}
                    className="inline-flex items-center gap-1 rounded-md border border-input px-1.5 py-0.5 text-[12px]">
                  {p.agent_slug && <Bot aria-hidden="true" className="size-3 text-primary" />}
                  <span>{p.display_name}</span>
                  <button type="button" aria-label={`Remove ${p.email}`} onClick={() => unpick(p.user_id)}
                          className="text-muted-foreground hover:text-foreground">
                    <X aria-hidden="true" className="size-3" />
                  </button>
                </li>
              ))}
            </ul>
          )}
          <Input
            id={`${uid}-q`}
            role="combobox"
            aria-expanded={open && shown.length > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={open && shown[active] ? `${uid}-opt-${shown[active].user_id}` : undefined}
            autoComplete="off"
            autoFocus={autoFocus}
            disabled={empty}
            value={query}
            placeholder={loading ? 'Loading…' : empty ? 'Everyone here is already in it' : 'Type a name or email'}
            onChange={(e) => {
              setQuery(e.target.value)
              setOpen(true)
              setActive(0)
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setOpen(false)}
            onKeyDown={onKey}
          />
          {open && shown.length > 0 && (
            <ul id={listId} role="listbox"
                className="absolute z-10 mt-1 max-h-60 w-full overflow-auto rounded-md border border-input bg-card py-1 shadow-md">
              {shown.map((c, i) => (
                <li
                  key={c.user_id}
                  id={`${uid}-opt-${c.user_id}`}
                  role="option"
                  aria-selected={i === active}
                  // mousedown, not click: the input's blur would close the list first.
                  onMouseDown={(e) => {
                    e.preventDefault()
                    pick(c)
                  }}
                  onMouseEnter={() => setActive(i)}
                  className={`flex cursor-pointer items-center gap-2 px-2 py-1 text-[13px] ${
                    i === active ? 'bg-muted' : ''}`}
                >
                  {c.agent_slug && <Bot aria-hidden="true" className="size-3.5 text-primary" />}
                  <span className="text-foreground">{c.display_name}</span>
                  {c.display_name !== c.email && <span className="text-muted-foreground">{c.email}</span>}
                </li>
              ))}
            </ul>
          )}
          {open && query.trim() && shown.length === 0 && !empty && (
            <p className="mt-1 text-[12px] text-muted-foreground">Nobody in this workspace matches.</p>
          )}
        </div>
        <div>
          <span className="mb-1 block text-[11px] text-muted-foreground">Role</span>
          {options.length > 1 ? (
            <RoleSelect value={current} options={options} onChange={setRole} ariaLabel="Role" />
          ) : (
            <span className={ROLE_TEXT_CLASS}>{options[0]?.label ?? ''}</span>
          )}
        </div>
        <Button type="submit" disabled={busy || picked.length === 0}>
          {busy ? 'Adding…' : picked.length > 1 ? `Add ${picked.length}` : 'Add'}
        </Button>
      </div>
      {error && (
        <p role="alert" className="m-0 text-[12px] text-destructive">
          {error}
        </p>
      )}
    </form>
  )
}
