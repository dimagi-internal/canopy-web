import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { AlertTriangle, BookOpen, Check, ChevronDown, Download, Eye, Pencil, Plus, Trash2, X } from 'lucide-react'

import {
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
import {
  activeAccess,
  agentName,
  dayLabel,
  durationOf,
  endedOf,
  eventSentence,
  grantCategories,
  groupFacts,
  placeOf,
  readsByEntry,
  relTime,
  type AgentAccess,
  type EntryReads,
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
    on: 'Agents you allow are told what is below, so you don’t have to repeat yourself.',
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

/** "What agents know about me" — three questions, in the order a person asks them:
 *  what is remembered, who can read it, and what happened to it. Every entry can
 *  be corrected or removed in place; every agent's access can be taken back; past
 *  access and settings stay one click away rather than in the way. Fleet brain v1
 *  (canopy#804), on HCP v1 (hcp_api.py). */
export function PeopleMePage() {
  const [me, setMe] = useState<PersonMe | null>(null)
  const [grants, setGrants] = useState<HcpGrant[]>([])
  const [audit, setAudit] = useState<HcpAuditEvent[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [busyGrant, setBusyGrant] = useState<string | null>(null)
  const [busyMemory, setBusyMemory] = useState<string | null>(null)
  const [source, setSource] = useState<Source>('all')
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null)
  const [showPast, setShowPast] = useState(false)
  const [activityShown, setActivityShown] = useState(12)

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

  useEffect(load, [load])

  // agent slug → the name you know it by, from your grants ("ace" → "ACE").
  const names = useMemo(() => {
    const m = new Map<string, string>()
    for (const g of grants) if (g.canopy.agent) m.set(g.canopy.agent, agentName(g))
    return m
  }, [grants])
  const reads = useMemo(() => readsByEntry(audit, names), [audit, names])
  const access = useMemo(() => activeAccess(grants), [grants])
  const past = useMemo(() => grants.filter((g) => g.status !== 'active'), [grants])
  const lastRead = useMemo(
    () => audit.find((e) => e.eventType === 'preference.read' && e.actorType !== 'user'),
    [audit],
  )

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

  const onAdd = async (category: string, text: string) => {
    try {
      await addMyEntry(category, text)
      load()
      return true
    } catch (err) {
      setError((err as Error).message)
      return false
    }
  }

  const moreActivity = async () => {
    if (activityShown < audit.length) {
      setActivityShown((n) => n + 20)
      return
    }
    if (!nextCursor) return
    try {
      const page = await listMyAudit(nextCursor)
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

  if (error) return <div className="p-6 text-destructive">{error}</div>
  if (me === null) return <div className="p-6 text-muted-foreground">Loading…</div>

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

  const allFacts = me.facts ?? []
  const conflicted = allFacts.filter((f) => f.status === 'conflicted')
  const settled = allFacts.filter((f) => f.status !== 'conflicted')
  const shown = settled.filter((f) => source === 'all' || sourceOf(f).mine === (source === 'you'))
  const groups = groupFacts(shown).filter((g) => enabled || g.facts.length > 0)
  const activity = audit.slice(0, activityShown)

  const factRow = (f: PersonFactDetail) => {
    const src = sourceOf(f)
    const r: EntryReads | undefined = f.entry_id ? reads.get(f.entry_id) : undefined
    const isEditing = editing?.id === f.id
    return (
      <li key={f.id} className="group relative py-2.5">
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
          {f.project ? <span>{f.project.title}</span> : null}
          {r ? (
            <span className="inline-flex items-center gap-1" title={`Read ${r.count}× · last ${new Date(r.last).toLocaleString()}`}>
              <Eye className="h-3 w-3" />
              {r.by.join(', ')} used this {r.count === 1 ? 'once' : `${r.count}×`}
            </span>
          ) : null}
        </div>
      </li>
    )
  }

  return (
    <div className="mx-auto max-w-4xl space-y-10 px-4 py-8">
      {/* --- the answer, before the detail ------------------------------------------ */}
      <header className="space-y-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">{enabled ? 'Your memory' : 'Your memory (paused)'}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {enabled
              ? 'What agents remember about you, who can read it, and what they did with it. Only you see this page.'
              : "Memory isn't on for you yet, so no agent learns from you or uses any of this. You can still download it, remove anything, or take back access."}
          </p>
        </div>
        <div className="grid grid-cols-3 gap-2 sm:gap-3">
          <Stat href="#remembered" value={String(settled.length)} label={settled.length === 1 ? 'thing remembered' : 'things remembered'} />
          <Stat
            href="#who"
            value={String(access.length)}
            label={access.length === 1 ? 'agent can read it' : 'agents can read it'}
            hint={access.map((a) => a.name).join(', ') || 'none right now'}
          />
          <Stat
            href="#activity"
            value={lastRead ? relTime(lastRead.timestamp) : '—'}
            label="last read"
            hint={lastRead ? `by ${eventSentence(lastRead, names).who}` : 'never'}
          />
        </div>
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
            <h2 className="text-lg font-semibold text-foreground">What agents know about you</h2>
            <p className="text-sm text-muted-foreground">Fix anything that's wrong — agents are told the corrected version.</p>
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
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {groups.map((g) => (
            <div key={g.category} className="flex flex-col rounded-xl border border-border bg-card px-4 pt-3 pb-2">
              <div className="flex items-baseline justify-between">
                <h3 className="text-sm font-semibold text-foreground">{g.label}</h3>
                <span className="text-xs text-muted-foreground">{g.facts.length || ''}</span>
              </div>
              {g.facts.length === 0 ? (
                <p className="py-2.5 text-sm text-muted-foreground/70">Nothing yet.</p>
              ) : (
                <ul className="divide-y divide-border">{g.facts.map(factRow)}</ul>
              )}
              {enabled && g.category !== 'other' && <AddLine category={g.category} label={g.label} onAdd={onAdd} />}
            </div>
          ))}
        </div>
        {enabled && (
          <p className="mt-3 text-xs text-muted-foreground">
            What you add here is personal: agents read it only if you point them at your personal memory (Settings, below).
          </p>
        )}
      </section>

      {/* --- who can read it ----------------------------------------------------- */}
      <section id="who" aria-label="Who can read it" className="scroll-mt-6">
        <h2 className="text-lg font-semibold text-foreground">Who can read it</h2>
        <p className="text-sm text-muted-foreground">
          Only agents you allowed, in a conversation. Allowing one allows no other. Taking access back works immediately.
        </p>
        {access.length === 0 ? (
          <p className="mt-3 rounded-xl border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
            No agent can read your memory right now. An agent will ask you in a conversation if it needs to.
          </p>
        ) : (
          <ul className="mt-3 grid gap-3 sm:grid-cols-2">
            {access.map((a) => {
              const lastByThem = audit.find(
                (e) => e.eventType === 'preference.read' && eventSentence(e, names).who === a.name,
              )
              const cats = Array.from(new Set(a.grants.flatMap(grantCategories)))
              const reach = a.grants.map((g) => g.canopy.reach).find(Boolean)
              return (
                <li key={a.key} className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
                  <div className="flex items-start gap-3">
                    <Avatar name={a.name} />
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-foreground">{a.name}</div>
                      <div className="text-xs text-muted-foreground">
                        {a.kept ? 'Until you take it back' : durationOf(a.grants[0])} · in {a.places.join(', ')}
                      </div>
                    </div>
                    <button
                      className="shrink-0 rounded-md px-2 py-1 text-xs font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50"
                      disabled={busyGrant === a.key}
                      onClick={() => onRevoke(a)}
                    >
                      {busyGrant === a.key ? 'Taking back…' : 'Take back'}
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {cats.map((c) => (
                      <span key={c} className="rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">
                        {c}
                      </span>
                    ))}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {reach ? `Reaches ${reach}. ` : ''}
                    {lastByThem ? `Last read ${relTime(lastByThem.timestamp)}.` : 'Hasn’t read anything yet.'}
                  </div>
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

      {/* --- what happened ------------------------------------------------------- */}
      <section id="activity" aria-label="What happened" className="scroll-mt-6">
        <h2 className="text-lg font-semibold text-foreground">What happened</h2>
        <p className="text-sm text-muted-foreground">Every time your memory was read or changed, newest first.</p>
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
        {(activityShown < audit.length || nextCursor) && (
          <button className="mt-3 text-sm text-primary hover:underline" onClick={moreActivity}>
            Show more
          </button>
        )}
      </section>

      {/* --- the controls you rarely need ---------------------------------------- */}
      <section aria-label="Settings" className="space-y-3">
        <h2 className="text-lg font-semibold text-foreground">Settings</h2>
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
        {enabled && <MemorySettings grants={grants} onChanged={load} />}
        <button
          className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline"
          onClick={onExport}
        >
          <Download className="h-4 w-4" />
          Download everything canopy holds about you
        </button>
      </section>
    </div>
  )
}

function Stat({ href, value, label, hint }: { href: string; value: string; label: string; hint?: string }) {
  return (
    <a href={href} className="block min-w-0 rounded-xl border border-border bg-card px-3 py-2.5 transition-colors hover:border-primary/40 sm:px-4 sm:py-3">
      <div className="text-lg font-semibold text-foreground sm:text-2xl">{value}</div>
      <div className="text-xs text-muted-foreground sm:text-sm">{label}</div>
      {hint ? <div className="mt-0.5 truncate text-xs text-muted-foreground/80">{hint}</div> : null}
    </a>
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

/** "+ Add" at the foot of a category: opens into one line, saves on Enter. */
function AddLine({ category, label, onAdd }: { category: string; label: string; onAdd: (c: string, t: string) => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  if (!open) {
    return (
      <button
        className="mt-auto inline-flex items-center gap-1 self-start rounded py-2 text-xs text-muted-foreground hover:text-primary"
        onClick={() => setOpen(true)}
      >
        <Plus className="h-3.5 w-3.5" /> Add
      </button>
    )
  }
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    setSaving(true)
    if (await onAdd(category, text.trim())) {
      setText('')
      setOpen(false)
    }
    setSaving(false)
  }
  return (
    <form onSubmit={submit} className="mt-auto flex items-center gap-2 py-2">
      <input
        autoFocus
        aria-label={`Add to ${label}`}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === 'Escape' && setOpen(false)}
        maxLength={500}
        placeholder="One sentence, e.g. I prefer a short answer with links."
        className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground"
      />
      <button
        type="submit"
        disabled={saving || !text.trim()}
        className="rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
      >
        {saving ? 'Saving…' : 'Save'}
      </button>
    </form>
  )
}
