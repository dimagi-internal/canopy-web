import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { AlertTriangle, BookOpen, Check, ChevronDown, Download, Eye, Pencil, Trash2, X } from 'lucide-react'

import { useParams } from 'react-router-dom'

import {
  MY_CATEGORIES,
  addMyEntry,
  correctMyEntry,
  exportMine,
  getMyPerson,
  getPersonPage,
  listMyAudit,
  listMyCollections,
  listMyGrants,
  listPersonPageAudit,
  listPersonPageSearches,
  retractFact,
  revokeMyGrant,
  setMyAgentMemory,
} from '../api/people'
import type { HcpAuditEvent, HcpGrant, MemoryCollections, MemoryViewers, PersonFactDetail, PersonMe } from '../api/people'
import { listWorkspaces } from '../api/workspaces'
import type { WorkspaceOut } from '../api/workspaces'
import { AgentReadsPicker, CollectionsPanel } from './memory/Collections'
import { PageViewers } from './memory/PageViewers'
import { SearchHistory, SearchPreview } from './memory/SearchHistory'
import {
  activeAccess,
  agentName,
  byProject,
  dayLabel,
  durationOf,
  endedOf,
  eventSentence,
  groupByPlace,
  placeOf,
  reads,
  relTime,
  type AgentAccess,
  type Place,
} from './memory/words'

/** The two canopy-level switches. They are POLICY: they grant no agent anything.
 *  An agent is granted only when you allow it in a conversation (and, separately,
 *  choose to keep allowing it), within what is switched on here. */
const MEMORY_SWITCHES = [
  {
    key: 'record' as const,
    label: 'Agents can learn from me',
    on: 'Agents you allow can remember things about you as you work with them.',
    off: 'No agent remembers anything new about you, in any conversation.',
  },
  {
    key: 'use' as const,
    label: 'Agents can use what they know',
    on: 'Agents you allow are given the entries below that are within their reach.',
    off: 'No agent is told anything below, in any conversation. Nothing is deleted.',
  },
]

type Source = 'all' | 'you' | 'agents'

/** Where an entry came from, as a short chip: "You" or "ACE · Oct 7". */
function sourceOf(f: PersonFactDetail): { mine: boolean; text: string } {
  const when = f.created_at ? new Date(f.created_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) : ''
  const site = (f.provenance_site as { name?: string } | undefined)?.name
  if (f.learned_by) return { mine: false, text: `${f.learned_by}${site ? ` on ${site}` : ''}${when ? ` · ${when}` : ''}` }
  if (!f.workspace || f.basis === 'declared') return { mine: true, text: `You${when ? ` · ${when}` : ''}` }
  return { mine: false, text: `${f.asserted_by || 'an agent'}${when ? ` · ${when}` : ''}` }
}

/** "Someone's page, as they let you see it" — /people/:personId/page, the href
 *  canopy hands an agent a person let in (apps/contacts/viewers.py). The same page,
 *  read-only, fed by the viewer routes. */
export function PersonPageView() {
  const { personId } = useParams()
  const id = Number(personId)
  if (!Number.isInteger(id) || id <= 0) return <div className="p-6 text-muted-foreground">There’s nothing at this address.</div>
  return <PeopleMePage viewing={id} />
}

/** "What agents know about me" — in the order a person asks: what is remembered
 *  and how much it was used, which conversations used it, who can read it, and
 *  what changed. Every entry can be corrected or removed in place; every agent's
 *  access can be taken back; settings and the ranking tools stay below, out of the
 *  way. With `viewing`, the page of that person as they let this agent see it:
 *  read-only. Fleet brain v1 (canopy#804), on HCP v1 (hcp_api.py). */
export function PeopleMePage({ viewing }: { viewing?: number } = {}) {
  const readOnly = viewing !== undefined
  const [me, setMe] = useState<PersonMe | null>(null)
  const [viewers, setViewers] = useState<MemoryViewers | null>(null)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [grants, setGrants] = useState<HcpGrant[]>([])
  const [audit, setAudit] = useState<HcpAuditEvent[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  // `fatal`: your own record didn't load, so there is no page. `error`: anything
  // else failed (a section, or an action) and shows as a banner above the page.
  const [fatal, setFatal] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [busyGrant, setBusyGrant] = useState<string | null>(null)
  const [busyMemory, setBusyMemory] = useState<string | null>(null)
  const [source, setSource] = useState<Source>('all')
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null)
  const [showPast, setShowPast] = useState(false)
  const [activityShown, setActivityShown] = useState(12)
  // The agent whose view you are looking through: hovering its card (or pinning it
  // with "See what it reads") fades every entry it can't read.
  const [hovered, setHovered] = useState<AgentAccess | null>(null)
  const [pinned, setPinned] = useState<AgentAccess | null>(null)
  const lens = hovered ?? pinned
  const [collections, setCollections] = useState<MemoryCollections | null>(null)
  const [workspaces, setWorkspaces] = useState<WorkspaceOut[]>([])
  const [busyCollections, setBusyCollections] = useState(false)

  const load = useCallback(() => {
    if (viewing !== undefined) {
      getPersonPage(viewing)
        .then((page) => {
          setMe(page.me)
          setGrants(page.grants as unknown as HcpGrant[])
          setCollections(page.collections)
        })
        .catch((e: Error) => setFatal(e.message))
      listPersonPageAudit(viewing)
        .then((page) => {
          setAudit(page.events)
          setNextCursor(page.nextCursor)
        })
        .catch((e: Error) => setError(`Couldn't load what happened: ${e.message}`))
      return
    }
    // Collections and your workspaces only feed controls; if either fails the
    // rest of the page still stands.
    listMyCollections()
      .then(setCollections)
      .catch(() => setCollections(null))
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]))
    getMyPerson()
      .then(setMe)
      .catch((e: Error) => setFatal(e.message))
    listMyGrants()
      .then(setGrants)
      .catch((e: Error) => setError(`Couldn't load which agents can read your memory: ${e.message}`))
    listMyAudit()
      .then((page) => {
        setAudit(page.events)
        setNextCursor(page.nextCursor)
      })
      .catch((e: Error) => setError(`Couldn't load what happened: ${e.message}`))
  }, [viewing])

  useEffect(load, [load])

  // agent slug → the name you know it by, from your grants ("ace" → "ACE").
  const names = useMemo(() => {
    const m = new Map<string, string>()
    for (const g of grants) if (g.canopy.agent) m.set(g.canopy.agent, agentName(g))
    return m
  }, [grants])
  const access = useMemo(() => activeAccess(grants), [grants])
  // Reads are shown per conversation above; this log keeps what changed and who looked.
  const changes = useMemo(() => audit.filter((e) => e.eventType !== 'preference.read'), [audit])
  const past = useMemo(() => grants.filter((g) => g.status !== 'active'), [grants])

  const run = async (key: (v: string | number | null) => void, id: string | number, work: () => Promise<unknown>) => {
    key(id)
    try {
      await work()
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      key(null)
    }
  }

  const onRevoke = (a: AgentAccess) => {
    if (!confirm(`Take back ${a.name}'s access? It stops being told anything about you right away, and won't be again unless you allow it.`)) return
    void run(
      (v) => setBusyGrant(v as string | null),
      a.key,
      () => Promise.all(a.grants.map((g) => revokeMyGrant(g.grantId))),
    )
  }

  const onToggleAvailable = (which: 'record' | 'use') => {
    if (!me) return
    const next = !me.agent_memory[which].available
    const offWarning =
      which === 'record'
        ? 'Stop agents learning from you, in every conversation? Nothing already remembered is deleted.'
        : 'Stop agents using your memory, in every conversation? Nothing is deleted.'
    if (!next && !confirm(offWarning)) return
    void run((v) => setBusyMemory(v as string | null), `${which}:available`, () =>
      setMyAgentMemory({ [which]: { available: next } }),
    )
  }

  const onToggleDefault = (which: 'record' | 'use') => {
    if (!me) return
    void run((v) => setBusyMemory(v as string | null), `${which}:default`, () =>
      setMyAgentMemory({ [which]: { default: !me.agent_memory[which].default } }),
    )
  }

  const onRetract = (fact: PersonFactDetail) => {
    if (!me) return
    if (!confirm(`Forget "${fact.statement}"? Agents stop being told it.`)) return
    void run((v) => setBusy(v as number | null), fact.id, () => retractFact(me.id, fact.id))
  }

  const onSaveEdit = (fact: PersonFactDetail) => {
    if (!editing || !fact.entry_id) return
    const text = editing.text.trim()
    setEditing(null)
    if (!text || text === fact.statement) return
    void run((v) => setBusy(v as number | null), fact.id, () => correctMyEntry(fact.entry_id as string, text))
  }

  // A collection change re-reads grants too: a grant's reach follows its collection.
  const actOnCollections = (work: () => Promise<unknown>) => {
    setBusyCollections(true)
    work()
      .then(load)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusyCollections(false))
  }

  const onAdd = async (category: string, text: string, workspace: string | null) => {
    try {
      await addMyEntry(category, text, workspace)
      load()
      return true
    } catch (err) {
      setError((err as Error).message)
      return false
    }
  }

  const moreActivity = async () => {
    if (activityShown < changes.length) {
      setActivityShown((n) => n + 20)
      return
    }
    if (!nextCursor) return
    try {
      const page = viewing !== undefined ? await listPersonPageAudit(viewing, nextCursor) : await listMyAudit(nextCursor)
      setAudit((prev) => [...prev, ...page.events])
      setNextCursor(page.nextCursor)
      setActivityShown((n) => n + 20)
    } catch (e) {
      setError((e as Error).message)
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

  if (fatal) return <div className="p-6 text-destructive">{fatal}</div>
  if (me === null) return <div className="p-6 text-muted-foreground">Loading…</div>

  // Memory is on only for the people settings.HCP_PEOPLE names, for now. Outside
  // that: nothing here at all — unless canopy already holds some of yours, which
  // stays yours to see, download and delete ("paused").
  // Read-only for a viewer: every control that changes something is hidden.
  const enabled = me.memory_enabled !== false
  const canEdit = enabled && !readOnly
  if (!enabled && !me.memory_paused) {
    return (
      <div className="mx-auto max-w-4xl px-4 py-8">
        <h1 className="text-2xl font-semibold text-foreground">Your memory</h1>
        <p className="mt-1 text-sm text-muted-foreground">Memory isn&apos;t available for you yet.</p>
      </div>
    )
  }

  const allFacts = me.facts ?? []
  const conflicted = allFacts.filter((f) => f.status === 'conflicted')
  const settled = allFacts.filter((f) => f.status !== 'conflicted')
  const shown = settled.filter((f) => source === 'all' || sourceOf(f).mine === (source === 'you'))
  const { orgs, unplaced } = groupByPlace(shown)
  const activity = changes.slice(0, activityShown)
  const usedIn = Math.max(0, ...settled.map((f) => f.used?.conversations ?? 0))
  const lastUsed = settled.map((f) => f.used?.last_at).filter(Boolean).sort().pop() ?? null
  const firstName = me.display_name.split(' ')[0] || me.display_name
  const lookers = viewers?.viewers.map((v) => v.name) ?? []

  const factRow = (f: PersonFactDetail) => {
    const src = sourceOf(f)
    const u = f.used
    const isEditing = editing?.id === f.id
    const hidden = lens !== null && !reads(lens, f.workspace ?? null)
    return (
      <li key={f.id} className={`group relative py-2.5 transition-opacity ${hidden ? 'opacity-25' : ''}`}>
        {isEditing ? (
          <form
            className="flex items-start gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              onSaveEdit(f)
            }}
          >
            <textarea
              autoFocus
              aria-label="Correct this"
              value={editing.text}
              onChange={(e) => setEditing({ id: f.id, text: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === 'Escape') setEditing(null)
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  onSaveEdit(f)
                }
              }}
              rows={2}
              maxLength={500}
              className="min-w-0 flex-1 resize-none rounded-md border border-primary/50 bg-background px-2 py-1 text-sm text-foreground outline-none"
            />
            <IconButton label="Save" onClick={() => onSaveEdit(f)}>
              <Check className="h-4 w-4" />
            </IconButton>
            <IconButton label="Cancel" onClick={() => setEditing(null)}>
              <X className="h-4 w-4" />
            </IconButton>
          </form>
        ) : (
          <div className="flex items-start gap-2">
            <p className={`min-w-0 flex-1 text-sm text-foreground ${busy === f.id ? 'opacity-50' : ''}`}>{f.statement}</p>
            {!readOnly && (
              <div className="flex shrink-0 gap-0.5 opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-within:opacity-100">
                {enabled && f.entry_id && (
                  <IconButton label="Correct" onClick={() => setEditing({ id: f.id, text: f.statement })} disabled={busy === f.id}>
                    <Pencil className="h-3.5 w-3.5" />
                  </IconButton>
                )}
                <IconButton label="Forget" danger onClick={() => onRetract(f)} disabled={busy === f.id}>
                  <Trash2 className="h-3.5 w-3.5" />
                </IconButton>
              </div>
            )}
          </div>
        )}
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span
            className={`rounded-full px-1.5 py-px ${src.mine ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}
            title={src.mine ? 'You said this' : f.confidence ? `An agent concluded this (${f.confidence} confidence)` : 'An agent recorded this'}
          >
            {src.mine ? '' : 'learned by '}
            {src.text}
          </span>
          {u && u.conversations > 0 && u.last_at ? (
            <span className="inline-flex items-center gap-1" title={`Last given to an agent ${new Date(u.last_at).toLocaleString()}`}>
              <Eye className="h-3 w-3" />
              Given to {(u.by ?? []).map((s) => names.get(s) ?? s.toUpperCase()).join(', ')} in{' '}
              {u.conversations === 1 ? '1 conversation' : `${u.conversations} conversations`} · last {relTime(u.last_at)}
            </span>
          ) : (
            <span>Not used yet</span>
          )}
        </div>
      </li>
    )
  }

  return (
    <div className="mx-auto max-w-4xl space-y-10 px-4 py-8">
      {error && (
        <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <p className="min-w-0 flex-1">{error}</p>
          <IconButton label="Dismiss" onClick={() => setError(null)}>
            <X className="h-4 w-4" />
          </IconButton>
        </div>
      )}
      {/* --- the answer, before the detail ------------------------------------------ */}
      <header className="space-y-3">
        <h1 className="text-2xl font-semibold text-foreground">
          {readOnly ? `What agents know about ${firstName}` : 'What agents know about you'}
          {!enabled && <span className="ml-2 text-base font-normal text-muted-foreground">(paused)</span>}
        </h1>
        {/* The answer in one sentence, before any detail. */}
        <p className="text-base text-foreground">
          {!enabled
            ? "Memory isn't on for you yet, so no agent learns from you or uses any of this."
            : settled.length === 0
              ? 'Agents haven’t remembered anything yet.'
              : `${settled.length === 1 ? 'One thing' : `${settled.length} things`} remembered. ${
                  access.length === 0
                    ? 'No agent can read them right now.'
                    : `${access.map((a) => a.name).join(' and ')} can read ${settled.length === 1 ? 'it' : 'them'}`
                }${access.length > 0 && usedIn > 0 && lastUsed ? `, and ${usedIn === 1 ? 'was' : 'has been'} given ${settled.length === 1 ? 'it' : 'them'} in ${usedIn === 1 ? '1 conversation' : `${usedIn} conversations`}, most recently ${relTime(lastUsed)}.` : access.length > 0 ? '.' : ''}`}
        </p>
        <p className="text-xs text-muted-foreground">
          {readOnly
            ? `Read-only — ${firstName} lets this agent see this page, and each look is logged where they can see it.`
            : lookers.length
              ? `You see this page, and so can ${lookers.join(', ')} (read-only).`
              : 'Only you see this page.'}
        </p>
      </header>

      {conflicted.length > 0 && (
        <section aria-label="Needs your eye" className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <AlertTriangle className="h-4 w-4 text-amber-500" />
            {conflicted.length === 1 ? 'One thing needs your eye' : `${conflicted.length} things need your eye`}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            An agent guessed these, and they contradict something you said. No agent is told them until you correct or forget them.
          </p>
          <ul className="mt-1 divide-y divide-border">{conflicted.map(factRow)}</ul>
        </section>
      )}

      {/* --- what is remembered -------------------------------------------------- */}
      <section id="remembered" aria-label="What agents know" className="scroll-mt-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-foreground">What’s remembered</h2>
            <p className="text-sm text-muted-foreground">
              {readOnly ? 'Each entry, who wrote it, and how often agents were given it.' : 'Fix anything that’s wrong — agents are told the corrected version.'}
            </p>
          </div>
          <Segmented
            value={source}
            onChange={setSource}
            options={[
              ['all', 'Everything'],
              ['you', 'You said'],
              ['agents', 'Agents learned'],
            ]}
          />
        </div>
        {lens && (
          <div className="sticky top-2 z-10 mt-3 flex items-center justify-between gap-2 rounded-lg bg-primary/15 px-3 py-2 text-xs text-primary backdrop-blur">
            <span>Showing what {lens.name} can read. Everything faded is out of its reach.</span>
            {pinned && (
              <button className="font-medium hover:underline" onClick={() => setPinned(null)}>
                Show everything
              </button>
            )}
          </div>
        )}
        <div className="mt-4 space-y-4">
          {orgs.map((o) => (
            <div key={o.org} className="rounded-xl border border-border bg-card px-4 pt-3 pb-2">
              <h3 className="text-sm font-semibold text-foreground">
                {o.name}
                {o.places.length > 1 ? (
                  <span className="ml-2 font-normal text-muted-foreground">{o.places.length} workspaces</span>
                ) : null}
              </h3>
              {o.places.map((p) => (
                <div key={p.workspace} className="mt-3">
                  <WorkspaceHeading name={p.name} readers={access.filter((a) => reads(a, p.workspace))} />
                  <ProjectGroups facts={p.facts} row={factRow} />
                </div>
              ))}
            </div>
          ))}
          {unplaced.facts.length > 0 && (
            <UnplacedCard place={unplaced} readers={access.filter((a) => reads(a, null))} row={factRow} />
          )}
          {orgs.length === 0 && unplaced.facts.length === 0 && (
            <p className="text-sm text-muted-foreground">Nothing is remembered about you.</p>
          )}
        </div>
        {canEdit && <Composer onAdd={onAdd} workspaces={workspaces} preferred={orgs[0]?.places[0]?.workspace ?? null} />}
      </section>

      {/* --- which conversations used it ------------------------------------------ */}
      {enabled && allFacts.length > 0 && (
        <SearchHistory
          names={names}
          canPreview={!readOnly}
          fetchPage={viewing !== undefined ? (before) => listPersonPageSearches(viewing, before) : undefined}
        />
      )}

      {/* --- who can read it ----------------------------------------------------- */}
      <section id="who" aria-label="Who can read it" className="scroll-mt-6">
        <h2 className="text-lg font-semibold text-foreground">Who can read it</h2>
        <p className="text-sm text-muted-foreground">
          Only agents you allowed, in a conversation. Allowing one allows no other. Taking access back works immediately.
        </p>
        {access.length === 0 ? (
          <p className="mt-3 rounded-xl border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
            No agent can read your memory right now. An agent can read it only after you allow it in a conversation.
          </p>
        ) : (
          <ul className="mt-3 grid gap-3 sm:grid-cols-2">
            {access.map((a) => {
              const lastByThem = audit.find(
                (e) => e.eventType === 'preference.read' && eventSentence(e, names).who === a.name,
              )
              const reach = a.grants.map((g) => g.canopy.reach).find(Boolean)
              const now = Array.from(new Set(a.grants.flatMap((g) => g.canopy.reach_names ?? [])))
              return (
                <li
                  key={a.key}
                  onMouseEnter={() => setHovered(a)}
                  onMouseLeave={() => setHovered(null)}
                  className={`flex flex-col gap-3 rounded-xl border bg-card p-4 transition-colors ${lens?.key === a.key ? 'border-primary/50' : 'border-border'}`}
                >
                  <div className="flex items-start gap-3">
                    <Avatar name={a.name} />
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-foreground">{a.name}</div>
                      <div className="text-xs text-muted-foreground">
                        {a.kept ? 'Until you take it back' : durationOf(a.grants[0])} · in {a.places.join(', ')}
                      </div>
                    </div>
                    {!readOnly && (
                      <button
                        className="shrink-0 rounded-md px-2 py-1 text-xs font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50"
                        disabled={busyGrant === a.key}
                        onClick={() => onRevoke(a)}
                      >
                        {busyGrant === a.key ? 'Taking back…' : 'Take back'}
                      </button>
                    )}
                  </div>
                  {reach && (
                    <div className="text-xs">
                      <div className="text-foreground">Reads {reach}</div>
                      {now.length > 0 && <div className="mt-0.5 text-muted-foreground">Right now: {now.join(', ')}</div>}
                    </div>
                  )}
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                    <span>{lastByThem ? `Last read ${relTime(lastByThem.timestamp)}.` : 'Hasn’t read anything yet.'}</span>
                    <a
                      href="#remembered"
                      onClick={() => {
                        setPinned(a)
                        setHovered(null)
                      }}
                      className="text-primary hover:underline"
                    >
                      See what it reads
                    </a>
                  </div>
                  {(() => {
                    // Only an agent you keep allowing can be pointed at a collection;
                    // a one-conversation grant reads its organisation and ends.
                    const kept = a.grants.find((g) => g.grantType === 'persistent' && g.canopy.agent)
                    return kept && !readOnly ? (
                      <AgentReadsPicker grant={kept} data={collections} disabled={busyCollections} act={actOnCollections} />
                    ) : null
                  })()}
                </li>
              )
            })}
          </ul>
        )}
        {past.length > 0 && (
          <div className="mt-3">
            <button
              className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
              aria-expanded={showPast}
              onClick={() => setShowPast((v) => !v)}
            >
              <ChevronDown className={`h-4 w-4 transition-transform ${showPast ? 'rotate-180' : ''}`} />
              {showPast ? 'Hide' : 'Show'} {past.length} past {past.length === 1 ? 'access' : 'accesses'}
            </button>
            {showPast && (
              <ul className="mt-2 divide-y divide-border rounded-xl border border-border text-sm">
                {past.map((g) => (
                  <li key={g.grantId} className="flex flex-wrap items-baseline justify-between gap-x-4 px-4 py-2">
                    <span className="text-foreground">
                      {agentName(g)} <span className="text-muted-foreground">in {placeOf(g)}</span>
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {endedOf(g)} · allowed {new Date(g.issuedAt).toLocaleDateString([], { month: 'short', day: 'numeric' })}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>

      {/* --- what changed -------------------------------------------------------- */}
      <section id="activity" aria-label="Changes and access" className="scroll-mt-6">
        <h2 className="text-lg font-semibold text-foreground">Changes and access</h2>
        <p className="text-sm text-muted-foreground">What was added, corrected or forgotten, and who was let in — newest first.</p>
        {activity.length === 0 ? (
          <p className="mt-3 text-sm text-muted-foreground">Nothing yet.</p>
        ) : (
          <ol className="mt-3 space-y-4">
            {activity.map((e, i) => {
              const day = dayLabel(e.timestamp)
              const s = eventSentence(e, names)
              return (
                <li key={e.eventId}>
                  {(i === 0 || dayLabel(activity[i - 1].timestamp) !== day) && (
                    <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">{day}</div>
                  )}
                  <div className="flex gap-3 border-l-2 border-border pl-3 text-sm">
                    <span className="w-16 shrink-0 text-xs leading-5 text-muted-foreground">
                      {new Date(e.timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                    </span>
                    <span className="min-w-0">
                      <span className="font-medium text-foreground">{s.who}</span>{' '}
                      <span className="text-foreground">{s.did}</span>
                      {s.why ? <span className="block text-xs text-muted-foreground">to {s.why.replace(/^to\s+/i, '')}</span> : null}
                    </span>
                  </div>
                </li>
              )
            })}
          </ol>
        )}
        {(activityShown < changes.length || nextCursor) && (
          <button className="mt-3 text-sm text-primary hover:underline" onClick={moreActivity}>
            Show more
          </button>
        )}
      </section>

      {/* --- the controls you rarely need ---------------------------------------- */}
      {readOnly ? (
        <section aria-label="Settings" className="space-y-1 text-sm text-muted-foreground">
          <h2 className="text-lg font-semibold text-foreground">Settings</h2>
          {MEMORY_SWITCHES.map((sw) => (
            <p key={sw.key}>
              <span className="text-foreground">{sw.label}:</span> {me.agent_memory[sw.key].available ? 'on' : 'off'}
            </p>
          ))}
        </section>
      ) : (
      <section aria-label="Settings and sharing" className="space-y-3">
        <h2 className="text-lg font-semibold text-foreground">Settings and sharing</h2>
        {enabled && (
          <div className="divide-y divide-border rounded-xl border border-border">
            {MEMORY_SWITCHES.map((sw) => {
              const { available, default: byDefault } = me.agent_memory[sw.key]
              return (
                <div key={sw.key} className="flex items-start gap-4 px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-foreground">{sw.label}</div>
                    <p className="mt-0.5 text-xs text-muted-foreground">{available ? sw.on : sw.off}</p>
                    {available && (
                      <label className="mt-2 inline-flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={byDefault}
                          disabled={busyMemory !== null}
                          onChange={() => onToggleDefault(sw.key)}
                          aria-label={`${sw.label}: on by default in new conversations`}
                        />
                        On in new conversations (each agent still asks you once first)
                      </label>
                    )}
                  </div>
                  <Switch
                    on={available}
                    label={sw.label}
                    busy={busyMemory === `${sw.key}:available`}
                    disabled={busyMemory !== null}
                    onClick={() => onToggleAvailable(sw.key)}
                  />
                </div>
              )
            })}
          </div>
        )}
        <PageViewers onChange={setViewers} />
        <button
          className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline"
          onClick={onExport}
        >
          <Download className="h-4 w-4" />
          Download everything canopy holds about you
        </button>
        {enabled && (
          <div>
            <button
              className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
              aria-expanded={showAdvanced}
              onClick={() => setShowAdvanced((v) => !v)}
            >
              <ChevronDown className={`h-4 w-4 transition-transform ${showAdvanced ? 'rotate-180' : ''}`} />
              Advanced: limit what an agent reads, and test how agents search
            </button>
            {showAdvanced && (
              <div className="mt-2 space-y-4">
                <CollectionsPanel data={collections} busy={busyCollections} act={actOnCollections} />
                <SearchPreview />
              </div>
            )}
          </div>
        )}
      </section>
      )}
    </div>
  )
}

function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T
  onChange: (v: T) => void
  options: [T, string][]
}) {
  return (
    <div role="radiogroup" className="inline-flex rounded-lg border border-border p-0.5 text-xs">
      {options.map(([v, label]) => (
        <button
          key={v}
          role="radio"
          aria-checked={value === v}
          onClick={() => onChange(v)}
          className={`rounded-md px-2.5 py-1 ${value === v ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

function Switch({ on, label, busy, disabled, onClick }: { on: boolean; label: string; busy: boolean; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={`relative mt-0.5 h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-60 ${on ? 'bg-primary' : 'bg-muted-foreground/30'} ${busy ? 'animate-pulse' : ''}`}
    >
      <span
        className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-background shadow transition-transform ${on ? 'translate-x-5' : ''}`}
      />
    </button>
  )
}

function IconButton({
  label,
  onClick,
  disabled,
  danger,
  children,
}: {
  label: string
  onClick: () => void
  disabled?: boolean
  danger?: boolean
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={`rounded p-1 text-muted-foreground disabled:opacity-50 ${danger ? 'hover:bg-destructive/10 hover:text-destructive' : 'hover:bg-muted hover:text-foreground'}`}
    >
      {children}
    </button>
  )
}

function Avatar({ name }: { name: string }) {
  return (
    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-sm font-semibold text-primary">
      {name.charAt(0).toUpperCase() || <BookOpen className="h-4 w-4" />}
    </span>
  )
}

/** A place's heading, with who can read what is in it — the line that answers
 *  "who sees this?" right where the entries are. */
function PlaceHeading({ title, note, readers, empty }: { title: string; note: string; readers: AgentAccess[]; empty?: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h3 className="text-sm font-semibold text-foreground">
        {title}
        {note ? <span className="ml-2 font-normal text-muted-foreground">{note}</span> : null}
      </h3>
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Eye className="h-3 w-3" />
        {readers.length ? `Read by ${readers.map((a) => a.name).join(', ')}` : (empty ?? 'No agent reads this right now')}
      </span>
    </div>
  )
}

/** A workspace's name with who reads what is in it, right now — per workspace,
 *  because agents' reach differs workspace by workspace (a collection can hold one). */
function WorkspaceHeading({ name, readers }: { name: string; readers: AgentAccess[] }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4">
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{name}</span>
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Eye className="h-3 w-3" />
        {readers.length ? `Read by ${readers.map((a) => a.name).join(', ')}` : 'No agent reads this right now'}
      </span>
    </div>
  )
}

/** A workspace's entries, under the board project each was learned on when any
 *  was. An agent working on a project is told that project's entries first. */
function ProjectGroups({ facts, row }: { facts: PersonFactDetail[]; row: (f: PersonFactDetail) => ReactNode }) {
  const groups = byProject(facts)
  if (groups.every((g) => g.project === null)) return <ul className="divide-y divide-border">{facts.map(row)}</ul>
  return (
    <div className="space-y-2">
      {groups.map((g) => (
        <div key={g.project?.id ?? 'none'} className="border-l-2 border-border pl-3">
          <p className="pt-2 text-xs text-muted-foreground">
            {g.project ? (
              <>
                Project <span className="font-medium text-foreground">{g.project.title}</span>
              </>
            ) : (
              'Not tied to a project'
            )}
          </p>
          <ul className="divide-y divide-border">{g.facts.map(row)}</ul>
        </div>
      ))}
    </div>
  )
}

type OnAdd =(category: string, text: string, workspace: string | null) => Promise<boolean>

/** Entries in no workspace: written by you before a workspace was required, or by
 *  an outside app (canopy files an app's writes in none, so they reach no
 *  workspace's agents). Shown only when there are some; each entry says who wrote
 *  it. An agent reads them only through a collection holding "No workspace". */
function UnplacedCard({ place, readers, row }: { place: Place; readers: AgentAccess[]; row: (f: PersonFactDetail) => ReactNode }) {
  return (
    <div className="flex flex-col rounded-xl border border-dashed border-border px-4 pt-3 pb-2">
      <PlaceHeading
        title="Not in any workspace"
        note="written by you without a workspace, or by an outside app"
        readers={readers}
        empty="No agent reads these. One can only through a collection that includes “No workspace”."
      />
      <ul className="divide-y divide-border">{place.facts.map(row)}</ul>
    </div>
  )
}

function CategorySelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <select
      aria-label="What kind of thing"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground"
    >
      {MY_CATEGORIES.map((c) => (
        <option key={c.value} value={c.value}>
          {c.label}
        </option>
      ))}
    </select>
  )
}

/** "Tell agents something about you" — one line at the top of your memory, with
 *  WHERE it goes as part of the sentence: a workspace you belong to, grouped under
 *  its organisation. Someone in no workspace saves without one (the API's default). */
function Composer({ onAdd, workspaces, preferred }: { onAdd: OnAdd; workspaces: WorkspaceOut[]; preferred: string | null }) {
  const [text, setText] = useState('')
  const [category, setCategory] = useState<string>(MY_CATEGORIES[0].value)
  const [where, setWhere] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const named = (slug: string) => workspaces.find((w) => w.slug === slug)?.display_name ?? slug
  const orgs = Array.from(new Set(workspaces.map((w) => w.root)))
  const target = where ?? (preferred && workspaces.some((w) => w.slug === preferred) ? preferred : workspaces[0]?.slug ?? '')
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    setSaving(true)
    if (await onAdd(category, text.trim(), target || null)) setText('')
    setSaving(false)
  }
  return (
    <form
      onSubmit={submit}
      aria-label="Tell agents something about you"
      className="mt-4 flex flex-col gap-2 rounded-xl border border-border bg-card p-3 sm:flex-row sm:items-center"
    >
      <input
        aria-label="Tell agents something about you"
        value={text}
        onChange={(e) => setText(e.target.value)}
        maxLength={500}
        placeholder="Tell agents something about you…"
        className="min-w-0 flex-1 bg-transparent px-1 py-1 text-sm text-foreground outline-none placeholder:text-muted-foreground"
      />
      <div className="flex flex-wrap items-center gap-2">
        <CategorySelect value={category} onChange={setCategory} />
        {workspaces.length > 0 && (
          <label className="flex items-center gap-1 text-xs text-muted-foreground">
            in
            <select
              aria-label="Where it goes"
              value={target}
              onChange={(e) => setWhere(e.target.value)}
              className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground"
            >
              {orgs.map((root) => (
                <optgroup key={root} label={named(root)}>
                  {workspaces
                    .filter((w) => w.root === root)
                    .map((w) => (
                      <option key={w.slug} value={w.slug}>
                        {w.display_name}
                      </option>
                    ))}
                </optgroup>
              ))}
            </select>
          </label>
        )}
        <button
          type="submit"
          disabled={saving || !text.trim()}
          className="rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Remember'}
        </button>
      </div>
    </form>
  )
}
