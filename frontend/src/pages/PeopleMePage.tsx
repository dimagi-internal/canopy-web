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
import { MemorySettings } from './MemorySettings'

/** The person's two agent-memory features, each with a canopy-level "available"
 *  and "on by default in new sessions". These are POLICY: they grant no agent
 *  anything. An agent is granted only when you allow it in a session (and,
 *  separately, choose to keep allowing it), within what is available here. */
const MEMORY_SWITCHES = [
  {
    key: 'record' as const,
    label: 'Learn from me',
    on: 'Available: agents you allow can add what they learn to your memory.',
    off: 'Not available: no agent adds anything to your memory, in any conversation.',
  },
  {
    key: 'use' as const,
    label: 'Use my memory',
    on: 'Available: agents you allow can read your memory to help you.',
    off: 'Not available: no agent reads your memory, in any conversation. Nothing below is deleted.',
  },
]

/** 4.1.5.1 Rule 3: show a narrowed grant as narrowed — which of your entries an
 *  app's grant reaches (your personal ones, plus any workspace you ticked). */
function reachOf(g: HcpGrant): string {
  // An agent's grant: canopy names its reach in words (e.g. "your Dimagi memory (all divisions)").
  if (g.canopy.reach) return g.canopy.reach
  const sources = new Set<string>()
  for (const r of g.restrictions as { narrowedTo?: { sources?: string[] } }[]) {
    for (const src of r.narrowedTo?.sources ?? []) sources.add(src)
  }
  if (sources.size === 0) return ''
  return Array.from(sources)
    .map((src) => (src === 'personal' ? 'your personal entries' : `what ${src.replace('workspace:', '')} learned`))
    .join(' and ')
}

/** Where an entry came from, in a person's words: "learned by Hal on connect-labs ·
 *  Oct 9" (the site is canopy's provenance extension, `record.provenance.site`). */
function learnedLine(f: PersonFactDetail): string {
  const when = f.created_at ? new Date(f.created_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) : ''
  const site = (f.provenance_site as { name?: string } | undefined)?.name
  if (f.learned_by) return `learned by ${f.learned_by}${site ? ` on ${site}` : ''}${when ? ` · ${when}` : ''}`
  if (!f.workspace) return `added by you${when ? ` · ${when}` : ''}`
  return `${f.asserted_by ? `by ${f.asserted_by}` : f.basis}${when ? ` · ${when}` : ''}`
}

/** "What agents know about me" — every live fact canopy holds about you, in one
 *  list; which clients (an agent, over a channel, on a host) may read it,
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
        ? 'Stop agents learning from you, in every conversation? Nothing already in your memory is deleted.'
        : 'Stop agents using your memory, in every conversation? Nothing is deleted.'
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
      a.download = 'my-memory.jsonld'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (error) return <div className="p-6 text-destructive">{error}</div>
  if (me === null) return <div className="p-6 text-muted-foreground">Loading…</div>

  const allFacts = me.facts ?? []
  // Memory is on only for the people settings.HCP_PEOPLE names, for now. Outside
  // that: nothing here at all — unless canopy already holds some of yours, which
  // stays yours to see, download and delete ("paused").
  const enabled = me.memory_enabled !== false
  if (!enabled && !me.memory_paused) {
    return (
      <div className="mx-auto max-w-4xl px-4 py-8">
        <h1 className="text-2xl font-semibold text-foreground">Your memory</h1>
        <p className="mt-1 text-sm text-muted-foreground">Memory isn&apos;t available for you yet.</p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-4xl px-4 py-8 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">
          {enabled ? 'Your memory' : 'Your memory (paused)'}
        </h1>
        {!enabled && (
          <p className="mt-1 text-sm text-muted-foreground">
            Memory isn&apos;t on for you yet, so no agent learns from you or uses any of this. What
            canopy already holds is below: you can download it, remove anything, or take back access.
          </p>
        )}
        <p className="mt-1 text-sm text-muted-foreground" hidden={!enabled}>
          {me.display_name}
          {me.email ? ` · ${me.email}` : ''}. When you talk to an agent you allowed, it is handed the
          few facts relevant to what you asked from its organisation&apos;s memory, and can look up
          more while it works. Retract anything that is wrong. A fact marked{' '}
          <em>conflicted</em> is something an agent guessed that contradicts what you said; agents
          are not told it until you correct or retract it.
        </p>
      </div>

      {enabled && (<>
      <section aria-label="Learn from me and use my memory" className="divide-y divide-border rounded-lg border border-border">
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
                      ? ' On in new conversations: each agent asks you once, in the conversation, before it starts.'
                      : ' Off in new conversations; turn it on in a conversation and that agent asks you there.'
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
          Saved to your personal memory. Agents are not told it unless you let them use your personal
          memory (More settings); you, and any app you allow below, are.
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
            aria-label="What to add to your memory"
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
      </>)}

      {allFacts.length === 0 ? (
        <p className="text-muted-foreground">Nothing yet — nothing has been recorded about you.</p>
      ) : (
        <section aria-label="Your memory" className="rounded-lg border border-border">
          <ul className="divide-y divide-border">
            {allFacts.map((f) => (
              <li key={f.id} className="flex items-start gap-3 px-4 py-3 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="text-foreground">{f.statement}</div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {f.status && f.status !== 'active' ? (
                      <span className="mr-1 rounded bg-destructive/10 px-1 text-destructive">{f.status}</span>
                    ) : null}
                    {learnedLine(f)}
                    {f.confidence ? ` · ${f.confidence} confidence` : ''}
                    {f.project ? ` · ${f.project.title}` : ''}
                  </div>
                </div>
                <div className="flex shrink-0 gap-3">
                  {enabled && f.entry_id && (
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
        </section>
      )}

      <section>
        <h2 className="text-sm font-semibold text-foreground">Who can read it</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          An agent is here only if you allowed it in a session — for that session, or kept for that
          agent. Allowing one agent allows no other. An app outside canopy is here only if you
          allowed it on its consent screen, and reaches only what it says below. Revoking stops it
          immediately and for good, unless you allow it again.
        </p>
        {grants.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">You have not allowed any agent or app.</p>
        ) : (
          <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-sm">
            {grants.map((g) => (
              <li key={g.grantId} className="flex items-start gap-3 px-4 py-2">
                <div className="min-w-0 flex-1">
                  <div className="text-foreground">
                    {g.client.name}
                    {g.canopy.client ? (
                      <span className="text-muted-foreground"> · app run by {g.canopy.client.operator}</span>
                    ) : null}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {g.status}
                    {g.grantType === 'persistent'
                      ? ' · kept until you revoke it'
                      : g.canopy.client
                        ? ' · temporary'
                        : ` · this conversation only${g.expiresAt ? `, until ${new Date(g.expiresAt).toLocaleString()}` : ''}`}
                    {g.canopy.workspace ? ` · ${g.canopy.workspace}` : ''}
                    {reachOf(g) ? ` · reaches ${reachOf(g)}` : ''}
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

      {enabled && <MemorySettings grants={grants} onChanged={load} />}

      <section>
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-foreground">Audit log</h2>
          <button className="text-sm text-primary hover:underline" onClick={onExport}>
            Download everything (JSON-LD)
          </button>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          Every read and change of your memory, newest first. Only you can see it.
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
