import { useState, type FormEvent } from 'react'

import type { AccessLineIn, AccessRoleOut } from '@/api/agents'
import {
  ADDABLE_ROLES,
  ADDRESS_ONLY_KINDS,
  AT_LEAST_OPTIONS,
  KIND_OPTIONS,
  canText,
  type AddableRole,
  type LineKind,
} from './accessWords'

// ADD ONE LINE to an access list — an agent's own, or a workspace's defaults.
//
// A line says who has which role: a person, everyone in a workspace, anyone at an email
// domain, or a known visitor or sender. The server decides what it will take; a refusal
// (422) is shown here, next to the form, in the server's own words. `canGiveEditor` is only
// a hint (a workspace's default editor line is its owner's call; the server still refuses).
const FIELD = 'rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground'

export function AccessLineForm({
  roles,
  workspaces,
  canGiveEditor = true,
  submitLabel = 'Add',
  onAdd,
}: {
  roles: readonly AccessRoleOut[]
  /** The workspaces "everyone in a workspace" can name: [slug, display name]. */
  workspaces: readonly [string, string][]
  canGiveEditor?: boolean
  submitLabel?: string
  /** Resolves when the line is added; rejects with the server's reason. */
  onAdd: (line: AccessLineIn) => Promise<unknown>
}) {
  const [kind, setKind] = useState<LineKind>('person')
  const [role, setRole] = useState<AddableRole>('user')
  const [email, setEmail] = useState('')
  const [workspace, setWorkspace] = useState(workspaces[0]?.[0] ?? '')
  const [atLeast, setAtLeast] = useState('')
  const [domain, setDomain] = useState('')
  const [manualOnly, setManualOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const addressOnly = ADDRESS_ONLY_KINDS.includes(kind)
  const editorOff = addressOnly || !canGiveEditor
  const effectiveWorkspace = workspace || workspaces[0]?.[0] || ''

  const pickKind = (next: LineKind) => {
    setKind(next)
    // Editor is never offered for the address-only kinds: step down rather than leave a refused choice.
    if (ADDRESS_ONLY_KINDS.includes(next) && role === 'editor') setRole('user')
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    const line: AccessLineIn = {
      kind,
      role,
      manual_only: manualOnly,
      verified: true,
      note: '',
      email: kind === 'person' ? email.trim() : undefined,
      workspace: kind === 'workspace' ? effectiveWorkspace : '',
      at_least: kind === 'workspace' ? atLeast : '',
      domain: kind === 'person' ? '' : domain.trim(),
    }
    try {
      await onAdd(line)
      setEmail('')
      setDomain('')
      setAtLeast('')
      setManualOnly(false)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not add that')
    } finally {
      setBusy(false)
    }
  }

  const ready =
    kind === 'person' ? email.trim() !== '' : kind === 'workspace' ? effectiveWorkspace !== '' : kind === 'domain' ? domain.trim() !== '' : true

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-2" data-testid="access-line-form" aria-label="Add access">
      <div className="flex flex-wrap items-center gap-2">
        <select value={kind} onChange={(e) => pickKind(e.target.value as LineKind)} aria-label="Who" className={FIELD}>
          {KIND_OPTIONS.map((k) => (
            <option key={k.kind} value={k.kind}>
              {k.label}
            </option>
          ))}
        </select>

        {kind === 'person' && (
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@example.com"
            aria-label="Their email"
            className={`${FIELD} w-56`}
          />
        )}
        {kind === 'workspace' && (
          <>
            <select value={effectiveWorkspace} onChange={(e) => setWorkspace(e.target.value)} aria-label="Workspace" className={FIELD}>
              {workspaces.map(([slug, name]) => (
                <option key={slug} value={slug}>
                  {name}
                </option>
              ))}
            </select>
            <select value={atLeast} onChange={(e) => setAtLeast(e.target.value)} aria-label="Which people" className={FIELD}>
              {AT_LEAST_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </>
        )}
        {kind !== 'person' && (
          <input
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            placeholder={kind === 'domain' ? 'example.org' : 'only addresses at… (optional)'}
            aria-label={kind === 'domain' ? 'Email domain' : 'Only addresses at this domain'}
            className={`${FIELD} w-48`}
          />
        )}
      </div>

      <fieldset className="m-0 flex flex-col gap-0.5 border-0 p-0">
        <legend className="mb-0.5 p-0 text-[11px] text-muted-foreground">Role</legend>
        {ADDABLE_ROLES.map((r) => {
          const disabled = r === 'editor' && editorOff
          return (
            <label key={r} className={`flex items-baseline gap-2 text-[12px] ${disabled ? 'text-muted-foreground' : 'text-foreground'}`}>
              <input type="radio" name="access-role" value={r} checked={role === r} disabled={disabled} onChange={() => setRole(r)} />
              <span className="capitalize">{r}</span>
              <span className="text-muted-foreground">
                {canText(r, roles)}
                {disabled && r === 'editor' && addressOnly ? ' (not for people known only by address)' : ''}
              </span>
            </label>
          )
        })}
      </fieldset>

      <label className="flex items-center gap-2 text-[12px] text-foreground">
        <input type="checkbox" checked={manualOnly} onChange={(e) => setManualOnly(e.target.checked)} />
        Needs approval before anything goes out
      </label>

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={busy || !ready}
          className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
        >
          {busy ? 'Adding…' : submitLabel}
        </button>
        {error && (
          <p role="alert" className="m-0 text-[12px] text-destructive">
            {error}
          </p>
        )}
      </div>
    </form>
  )
}
