import { useState, type FormEvent } from 'react'

import type { AccessLineIn, AccessRoleOut } from '@/api/agents'
import {
  ADDRESS_ONLY_KINDS,
  AT_LEAST_OPTIONS,
  KIND_OPTIONS,
  assignable,
  canText,
  drives,
  roleWord,
  type LineKind,
} from './accessWords'

// ADD ONE LINE to an access list — an agent's own, or a workspace's defaults.
//
// A line says who has what: a person, everyone in a workspace (optionally also any workspace below
// it), anyone at an email domain, or a known visitor or sender. What they get is ONE role or,
// rarely, ONE connected site (picked from the agent's own list). No role is
// pre-selected: it is a choice, not a default. The server decides what it will take; a refusal (422) is shown here,
// next to the form, in the server's own words. `canGiveWrite` is only a hint (a workspace's default
// line that drives the whole agent is its owner's call; the server still refuses).
const FIELD = 'rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground'

export function AccessLineForm({
  roles,
  workspaces,
  canGiveWrite = true,
  submitLabel = 'Add',
  doors,
  doorSites,
  onAdd,
}: {
  roles: readonly AccessRoleOut[]
  /** The workspaces "everyone in a workspace" can name: [slug, display name]. */
  workspaces: readonly [string, string][]
  canGiveWrite?: boolean
  submitLabel?: string
  /** The agent's published doors, when this form is for one agent: a door is picked from this
   *  list. Left out (a workspace's defaults, which span agents) the name is typed. */
  doors?: readonly string[]
  /** Each of `doors`' connected site, so a person picks a site and not an internal name. */
  doorSites?: Readonly<Record<string, string>>
  /** Resolves when the line is added; rejects with the server's reason. */
  onAdd: (line: AccessLineIn) => Promise<unknown>
}) {
  const [kind, setKind] = useState<LineKind>('person')
  // '' = nothing chosen yet; 'capability' = one named capability instead of a role.
  const [choice, setChoice] = useState<string>('')
  const [capability, setCapability] = useState('')
  const [includeBelow, setIncludeBelow] = useState(false)
  const [email, setEmail] = useState('')
  const [workspace, setWorkspace] = useState(workspaces[0]?.[0] ?? '')
  const [atLeast, setAtLeast] = useState('')
  const [domain, setDomain] = useState('')
  const [manualOnly, setManualOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const addressOnly = ADDRESS_ONLY_KINDS.includes(kind)
  const writeOff = addressOnly || !canGiveWrite
  const effectiveWorkspace = workspace || workspaces[0]?.[0] || ''

  const pickKind = (next: LineKind) => {
    setKind(next)
    // A role that drives the whole agent is never offered for the address-only kinds: clear it
    // rather than leave a refused choice.
    if (ADDRESS_ONLY_KINDS.includes(next) && choice !== 'capability' && drives(choice, roles)) {
      setChoice('')
    }
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    const line: AccessLineIn = {
      kind,
      role: choice === 'capability' || choice === '' ? undefined : choice,
      capability: choice === 'capability' ? capability.trim() : '',
      include_below: kind === 'workspace' && includeBelow,
      manual_only: manualOnly && choice !== 'capability',
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
      setChoice('')
      setCapability('')
      setIncludeBelow(false)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not add that')
    } finally {
      setBusy(false)
    }
  }

  const whoReady =
    kind === 'person' ? email.trim() !== '' : kind === 'workspace' ? effectiveWorkspace !== '' : kind === 'domain' ? domain.trim() !== '' : true
  const ready = whoReady && (choice === 'capability' ? capability.trim() !== '' : choice !== '')

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
            <label className="flex items-center gap-1.5 text-[12px] text-foreground">
              <input type="checkbox" checked={includeBelow} onChange={(e) => setIncludeBelow(e.target.checked)} />
              or any workspace below it
            </label>
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
        <legend className="mb-0.5 p-0 text-[11px] text-muted-foreground">What they get</legend>
        {assignable(roles).map((role) => {
          const r = role.role
          const disabled = role.contains_write && writeOff
          return (
            <label key={r} className={`flex items-baseline gap-2 text-[12px] ${disabled ? 'text-muted-foreground' : 'text-foreground'}`}>
              <input type="radio" name="access-role" value={r} checked={choice === r} disabled={disabled} onChange={() => setChoice(r)} />
              <span>{roleWord(r, roles)}</span>
              <span className="text-muted-foreground">
                {canText(r, roles)}
                {role.description ? ` · ${role.description}` : ''}
                {disabled && addressOnly ? ' (not for people known only by address)' : ''}
              </span>
            </label>
          )
        })}
        <label className="flex items-baseline gap-2 text-[12px] text-foreground">
          <input type="radio" name="access-role" value="capability" checked={choice === 'capability'} onChange={() => setChoice('capability')} />
          <span>One connected site</span>
          <span className="text-muted-foreground">(rare) they may use it only from that site, and nothing else</span>
        </label>
        {choice === 'capability' && doors !== undefined && (
          doors.length > 0 ? (
            <select value={capability} onChange={(e) => setCapability(e.target.value)} aria-label="Connected site" className={`${FIELD} ml-6 w-56`}>
              <option value="">Pick a site…</option>
              {doors.map((d) => (
                <option key={d} value={d}>
                  {doorSites?.[d] ?? d}
                </option>
              ))}
            </select>
          ) : (
            <p className="m-0 ml-6 text-[12px] text-muted-foreground">This agent has no connected sites yet.</p>
          )
        )}
        {choice === 'capability' && doors === undefined && (
          <input
            value={capability}
            onChange={(e) => setCapability(e.target.value)}
            placeholder="internal name, e.g. connect"
            aria-label="Internal name of the site"
            className={`${FIELD} ml-6 w-56`}
          />
        )}
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
