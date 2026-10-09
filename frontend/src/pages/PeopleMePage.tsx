import { useCallback, useEffect, useState, type FormEvent } from 'react'

import {
  MY_CATEGORIES,
  addMyEntry,
  correctMyEntry,
  exportMine,
  getMyPerson,
  listMyAudit,
  listMyGrants,
  retractFact,
  revokeMyGrant,
  setMyAgentMemory,
} from '../api/people'
import type { HcpAuditEvent, HcpGrant, PersonFactDetail, PersonMe } from '../api/people'

/** The person's two agent-memory features, each with a canopy-level "available"
 *  and "on by default in new sessions"; a session can override the default but
 *  never turn on what is not available. One plain line for each state. */
const MEMORY_SWITCHES = [
  {
    key: 'record' as const,
    label: 'Agents may learn about me',
    on: 'Available: agents you talk to may record what they learn about your work.',
    off: 'Not available: no agent records anything new about you, in any session.',
  },
  {
    key: 'use' as const,
    label: "Agents may use what they've learned",
    on: 'Available: agents you talk to may be told what is relevant about you.',
    off: 'Not available: no agent is told anything about you, in any session. Nothing below is deleted.',
  },
]

/** "What agents know about me" — every live fact canopy holds about you, by
 *  workspace; which clients (an agent, over a channel, on a host) may read it,
 *  each revocable; and your audit log of every read and change. You can
 *  retract any fact. Fleet brain v1 (canopy#804), on HCP v1 (hcp_api.py). */
export function PeopleMePage() {
  const [me, setMe] = useState<PersonMe | null>(null)
  const [grants, setGrants] = useState<HcpGrant[]>([])
  const [audit, setAudit] = useState<HcpAuditEvent[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [busyGrant, setBusyGrant] = useState<string | null>(null)
  const [busyMemory, setBusyMemory] = useState<string | null>(null)
  const [newCategory, setNewCategory] = useState<string>(MY_CATEGORIES[0].value)
  const [newText, setNewText] = useState('')
  const [adding, setAdding] = useState(false)

  const load = useCallback(() => {
    getMyPerson()
      .then(setMe)
      .catch((e: Error) => setError(e.message))
    listMyGrants()
      .then(setGrants)
      .catch((e: Error) => setError(e.message))
    listMyAudit()
      .then((page) => {
        setAudit(page.events)
        setNextCursor(page.nextCursor)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  const moreAudit = async () => {
    if (!nextCursor) return
    try {
      const page = await listMyAudit(nextCursor)
      setAudit((prev) => [...prev, ...page.events])
      setNextCursor(page.nextCursor)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const onRevoke = async (g: HcpGrant) => {
    if (!confirm(`Revoke ${g.client.name}? It will stop being told anything about you, and canopy will not grant it again unless you do.`)) return
    setBusyGrant(g.grantId)
    try {
      await revokeMyGrant(g.grantId)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyGrant(null)
    }
  }

  useEffect(load, [load])

  const onToggleAvailable = async (which: 'record' | 'use') => {
    if (!me) return
    const next = !me.agent_memory[which].available
    const offWarning =
      which === 'record'
        ? 'Stop agents learning about you, in every session? Nothing already recorded is deleted.'
        : 'Stop agents using what they have learned, in every session? Nothing is deleted.'
    if (!next && !confirm(offWarning)) return
    setBusyMemory(`${which}:available`)
    try {
      await setMyAgentMemory({ [which]: { available: next } })
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyMemory(null)
    }
  }

  const onToggleDefault = async (which: 'record' | 'use') => {
    if (!me) return
    setBusyMemory(`${which}:default`)
    try {
      await setMyAgentMemory({ [which]: { default: !me.agent_memory[which].default } })
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyMemory(null)
    }
  }

  const onRetract = async (fact: PersonFactDetail) => {
    if (!me) return
    if (!confirm(`Retract "${fact.statement}"? Agents will stop being told it.`)) return
    setBusy(fact.id)
    try {
      await retractFact(me.id, fact.id)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const onCorrect = async (fact: PersonFactDetail) => {
    if (!fact.entry_id) return
    const text = prompt('Correct this. Agents will be told the corrected version.', fact.statement)
    if (text === null || !text.trim() || text.trim() === fact.statement) return
    setBusy(fact.id)
    try {
      await correctMyEntry(fact.entry_id, text.trim())
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const onAdd = async (e: FormEvent) => {
    e.preventDefault()
    if (!newText.trim()) return
    setAdding(true)
    try {
      await addMyEntry(newCategory, newText.trim())
      setNewText('')
      load()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setAdding(false)
    }
  }

  const onExport = async () => {
    try {
      const blob = await exportMine()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'what-canopy-knows-about-me.jsonld'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (error) return <div className="p-6 text-destructive">{error}</div>
  if (me === null) return <div className="p-6 text-muted-foreground">Loading…</div>

  const allFacts = me.facts ?? []

  // A personal entry (no workspace) is grouped first, under "Personal".
  const PERSONAL = ''
  const keyOf = (f: PersonFactDetail) => f.workspace ?? PERSONAL
  const workspaces = Array.from(new Set(allFacts.map(keyOf))).sort()

  return (
    <div className="mx-auto max-w-4xl px-4 py-8 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">What agents know about me</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {me.display_name}
          {me.email ? ` · ${me.email}` : ''}. When you talk to an agent, it is handed the few facts
          relevant to what you asked — only within the workspace they were written in — and can
          look up more while it works. Retract anything that is wrong. A fact marked{' '}
          <em>conflicted</em> is something an agent guessed that contradicts what you said; agents
          are not told it until you correct or retract it.
        </p>
      </div>

      <section aria-label="Agent memory" className="divide-y divide-border rounded-lg border border-border">
        {MEMORY_SWITCHES.map((sw) => {
          const { available, default: byDefault } = me.agent_memory[sw.key]
          return (
            <div
              key={sw.key}
              className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <div className="text-sm font-semibold text-foreground">{sw.label}</div>
                <p className="mt-0.5 text-sm text-muted-foreground">
                  {available ? sw.on : sw.off}
                  {available
                    ? byDefault
                      ? ' On in new sessions; you can turn it off in any one.'
                      : ' Off in new sessions; you can turn it on in any one.'
                    : ''}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  role="switch"
                  aria-checked={available}
                  aria-label={`${sw.label}: available`}
                  onClick={() => onToggleAvailable(sw.key)}
                  disabled={busyMemory !== null}
                  className={`rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${
                    available
                      ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                      : 'border border-border text-foreground hover:bg-muted'
                  }`}
                >
                  {busyMemory === `${sw.key}:available` ? 'Saving…' : available ? 'Available' : 'Not available'}
                </button>
                {available && (
                  <button
                    type="button"
                    role="switch"
                    aria-checked={byDefault}
                    aria-label={`${sw.label}: on by default in new sessions`}
                    onClick={() => onToggleDefault(sw.key)}
                    disabled={busyMemory !== null}
                    className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted disabled:opacity-50"
                  >
                    {busyMemory === `${sw.key}:default` ? 'Saving…' : byDefault ? 'On by default' : 'Off by default'}
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </section>

      <form onSubmit={onAdd} aria-label="Add something about yourself" className="rounded-lg border border-border px-4 py-3">
        <div className="text-sm font-semibold text-foreground">Add something about yourself</div>
        <p className="mt-0.5 text-sm text-muted-foreground">
          Saved as a personal entry: yours, in no workspace. Agents in your workspaces are not told it;
          you, and any app you allow below, are.
        </p>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <select
            aria-label="Category"
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
          >
            {MY_CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
          <input
            aria-label="What to remember"
            value={newText}
            onChange={(e) => setNewText(e.target.value)}
            maxLength={500}
            placeholder="One sentence, e.g. I prefer a short answer with links."
            className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
          />
          <button
            type="submit"
            disabled={adding || !newText.trim()}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {adding ? 'Saving…' : 'Save'}
          </button>
        </div>
      </form>

      {workspaces.length === 0 && (
        <p className="text-muted-foreground">Nothing yet — nothing has been recorded about you.</p>
      )}

      {workspaces.map((ws) => {
        const facts = allFacts.filter((f) => keyOf(f) === ws)
        return (
          <section key={ws} className="rounded-lg border border-border">
            <h2 className="border-b border-border px-4 py-2 text-sm font-semibold text-foreground">
              {ws === PERSONAL ? 'Personal — yours, in no workspace' : ws}
            </h2>
            {facts.length === 0 ? (
              <p className="px-4 py-3 text-sm text-muted-foreground">No facts.</p>
            ) : (
              <ul className="divide-y divide-border">
                {facts.map((f) => (
                  <li key={f.id} className="flex items-start gap-3 px-4 py-3 text-sm">
                    <span className="mt-0.5 shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                      {f.kind}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-foreground">{f.statement}</div>
                      <div className="mt-0.5 text-xs text-muted-foreground">
                        {f.status && f.status !== 'active' ? (
                          <span className="mr-1 rounded bg-destructive/10 px-1 text-destructive">
                            {f.status}
                          </span>
                        ) : null}
                        {f.basis}
                        {f.confidence ? ` (${f.confidence} confidence)` : ''}
                        {f.asserted_by ? ` · by ${f.asserted_by}` : ''}
                        {f.project ? ` · ${f.project.title}` : ''}
                        {f.instance_ref ? ` · ${f.instance_ref}` : ''}
                        {f.created_at ? ` · ${new Date(f.created_at).toLocaleDateString()}` : ''}
                      </div>
                    </div>
                    <div className="flex shrink-0 gap-3">
                      {f.entry_id && (
                        <button
                          className="text-primary hover:underline disabled:opacity-50"
                          disabled={busy === f.id}
                          onClick={() => onCorrect(f)}
                        >
                          Correct
                        </button>
                      )}
                      <button
                        className="text-destructive hover:text-destructive/80 disabled:opacity-50"
                        disabled={busy === f.id}
                        onClick={() => onRetract(f)}
                      >
                        Retract
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )
      })}

      <section>
        <h2 className="text-sm font-semibold text-foreground">Who can read it</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Each agent is allowed separately for each way you reach it — ACE over Slack and ACE over
          email are two entries. canopy allows an agent the first time it serves you; revoking stops
          it immediately and for good, unless you allow it again.
        </p>
        {grants.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">No agent has served you yet.</p>
        ) : (
          <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-sm">
            {grants.map((g) => (
              <li key={g.grantId} className="flex items-start gap-3 px-4 py-2">
                <div className="min-w-0 flex-1">
                  <div className="text-foreground">{g.client.name}</div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {g.status}
                    {g.canopy.workspace ? ` · ${g.canopy.workspace}` : ''}
                    {` · since ${new Date(g.issuedAt).toLocaleDateString()}`}
                    {g.expiresAt ? ` · until ${new Date(g.expiresAt).toLocaleString()}` : ''}
                    {` · ${Array.from(new Set(g.scopes.map((s) => s.split(':').slice(1, -1).join(':')))).join(', ')}`}
                  </div>
                </div>
                {g.status === 'active' && (
                  <button
                    className="shrink-0 text-destructive hover:text-destructive/80 disabled:opacity-50"
                    disabled={busyGrant === g.grantId}
                    onClick={() => onRevoke(g)}
                  >
                    Revoke
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-foreground">Audit log</h2>
          <button className="text-sm text-primary hover:underline" onClick={onExport}>
            Download everything (JSON-LD)
          </button>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          Every read and change of what canopy holds about you, newest first. Only you can see it.
        </p>
        {audit.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">Nothing yet.</p>
        ) : (
          <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-sm">
            {audit.map((e) => (
              <li key={e.eventId} className="flex flex-wrap gap-x-3 px-4 py-2">
                <span className="text-muted-foreground">{new Date(e.timestamp).toLocaleString()}</span>
                <span className="text-foreground">{e.actorId}</span>
                <span className="text-muted-foreground">
                  {e.eventType}
                  {e.category ? ` · ${e.category}` : ''}
                  {e.purpose ? ` · “${e.purpose}”` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
        {nextCursor && (
          <button className="mt-2 text-sm text-primary hover:underline" onClick={moreAudit}>
            Show older
          </button>
        )}
      </section>
    </div>
  )
}
