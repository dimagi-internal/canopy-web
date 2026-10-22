import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  getCatalog,
  getCapability,
  type CapabilityCatalog,
  type CapabilityDetail,
  type CapabilityItem,
  type CapabilityKind,
} from '@/api/system'
import { WORKFLOWS, type WorkflowStep } from './systemWorkflows'

const KIND_LABEL: Record<CapabilityKind, string> = {
  skill: 'Skill',
  agent: 'Agent',
  command: 'Command',
}

// Each kind borrows one of the semantic accent tokens for its badge.
const KIND_BADGE: Record<CapabilityKind, string> = {
  skill: 'bg-primary/10 text-primary',
  agent: 'bg-info/10 text-info',
  command: 'bg-special/10 text-special',
}

type KindFilter = 'all' | CapabilityKind
type ViewMode = 'catalog' | 'workflows'

function KindBadge({ kind }: { kind: CapabilityKind }) {
  return (
    <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${KIND_BADGE[kind]}`}>
      {KIND_LABEL[kind]}
    </span>
  )
}

function groupLabel(kind: CapabilityKind, family: string): string {
  if (kind === 'agent') return 'Agents'
  if (kind === 'command') return 'Commands'
  return family === 'general' ? 'General' : family
}

/** Resolve a workflow step against the live catalog: exact (kind,name) → name → null. */
function resolveStep(items: readonly CapabilityItem[], step: WorkflowStep): CapabilityItem | null {
  return (
    items.find((i) => i.kind === step.kind && i.name === step.name) ||
    items.find((i) => i.name === step.name) ||
    null
  )
}

function WorkflowsView({
  items,
  selected,
  onSelect,
}: {
  items: readonly CapabilityItem[]
  selected: CapabilityItem | null
  onSelect: (i: CapabilityItem) => void
}) {
  return (
    <div className="space-y-5">
      {WORKFLOWS.map((wf) => (
        <section key={wf.id} className="rounded-lg border border-border bg-card p-4">
          <h2 className="text-sm font-semibold text-foreground">{wf.title}</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{wf.description}</p>
          <ol className="mt-3 space-y-1">
            {wf.steps.map((step, idx) => {
              const item = resolveStep(items, step)
              const active = !!item && selected?.kind === item.kind && selected?.name === item.name
              return (
                <li key={`${step.kind}:${step.name}:${idx}`} className="flex items-start gap-2">
                  <span className="mt-1 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-semibold text-muted-foreground">
                    {idx + 1}
                  </span>
                  {item ? (
                    <button
                      type="button"
                      onClick={() => onSelect(item)}
                      className={`min-w-0 flex-1 rounded-md px-2 py-1 text-left transition-colors hover:bg-muted/50 ${active ? 'bg-muted/60' : ''}`}
                    >
                      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span className="text-sm font-medium text-foreground">{item.display_name}</span>
                        <KindBadge kind={item.kind} />
                      </span>
                      {step.note && <span className="mt-0.5 block text-xs text-muted-foreground">{step.note}</span>}
                    </button>
                  ) : (
                    <span className="min-w-0 flex-1 px-2 py-1">
                      <span className="font-mono text-sm text-foreground-subtle line-through">{step.name}</span>
                      <span className="ml-2 text-[10px] text-muted-foreground">(not in this plugin version)</span>
                    </span>
                  )}
                </li>
              )
            })}
          </ol>
        </section>
      ))}
    </div>
  )
}

export function SystemPage() {
  const [catalog, setCatalog] = useState<CapabilityCatalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<KindFilter>('all')
  const [mode, setMode] = useState<ViewMode>('catalog')
  const [selected, setSelected] = useState<CapabilityItem | null>(null)
  const [detail, setDetail] = useState<CapabilityDetail | null>(null)
  const [detailErr, setDetailErr] = useState<string | null>(null)

  useEffect(() => {
    getCatalog().then(setCatalog).catch((e) => setError(String(e.message || e)))
  }, [])

  useEffect(() => {
    if (!selected) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetail(null)
    setDetailErr(null)
    getCapability(selected.kind, selected.name)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setDetailErr(String(e.message || e)))
    return () => {
      cancelled = true
    }
  }, [selected])

  const filtered = useMemo(() => {
    const items = catalog?.items ?? []
    const q = query.trim().toLowerCase()
    return items.filter((i) => {
      if (kind !== 'all' && i.kind !== kind) return false
      if (!q) return true
      return (
        i.display_name.toLowerCase().includes(q) ||
        i.name.toLowerCase().includes(q) ||
        i.description.toLowerCase().includes(q)
      )
    })
  }, [catalog, query, kind])

  // Group: skills by family, then Agents, then Commands. Stable, readable order.
  const groups = useMemo(() => {
    const map = new Map<string, CapabilityItem[]>()
    const order: string[] = []
    const push = (label: string, item: CapabilityItem) => {
      if (!map.has(label)) {
        map.set(label, [])
        order.push(label)
      }
      map.get(label)!.push(item)
    }
    for (const i of filtered.filter((i) => i.kind === 'skill').sort((a, b) => a.name.localeCompare(b.name))) {
      push(groupLabel('skill', i.family), i)
    }
    for (const i of filtered.filter((i) => i.kind === 'agent').sort((a, b) => a.name.localeCompare(b.name))) {
      push('Agents', i)
    }
    for (const i of filtered.filter((i) => i.kind === 'command').sort((a, b) => a.name.localeCompare(b.name))) {
      push('Commands', i)
    }
    // Skill families alphabetical, but General last within skills; Agents/Commands already appended last.
    return order
      .sort((a, b) => {
        const rank = (l: string) => (l === 'Agents' ? 2 : l === 'Commands' ? 3 : l === 'General' ? 1 : 0)
        const ra = rank(a)
        const rb = rank(b)
        return ra !== rb ? ra - rb : a.localeCompare(b)
      })
      .map((label) => ({ label, items: map.get(label)! }))
  }, [filtered])

  if (error) {
    return <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">Failed to load capabilities: {error}</div>
  }
  if (!catalog) {
    return <div className="text-sm text-muted-foreground">Loading capabilities…</div>
  }

  const counts = catalog.counts
  const total = filtered.length

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold text-foreground">Canopy System</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Everything the canopy plugin can do — read live from its skill, agent, and command definitions.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span><span className="text-foreground-secondary">{counts.skill ?? 0}</span> skills</span>
          <span>·</span>
          <span><span className="text-foreground-secondary">{counts.agent ?? 0}</span> agents</span>
          <span>·</span>
          <span><span className="text-foreground-secondary">{counts.command ?? 0}</span> commands</span>
          {catalog.plugin_version && (
            <>
              <span>·</span>
              <span className="font-mono">canopy v{catalog.plugin_version}</span>
            </>
          )}
        </div>
        {catalog.warning && (
          <div className="mt-3 rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning">{catalog.warning}</div>
        )}
      </header>

      {/* View toggle: Catalog vs Workflows */}
      <div className="mb-4 flex gap-1">
        {(['catalog', 'workflows'] as ViewMode[]).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => {
              setMode(m)
              setSelected(null)
            }}
            className={`rounded-md border px-3 py-1.5 text-sm capitalize transition-colors ${
              mode === m
                ? 'border-primary/30 bg-primary/10 text-primary font-medium'
                : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground-secondary'
            }`}
          >
            {m}
          </button>
        ))}
      </div>

      {/* Catalog controls (search + kind) — only in catalog mode */}
      {mode === 'catalog' && (
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search capabilities…"
          className="w-full rounded-lg border border-input bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none sm:max-w-xs"
        />
        <div className="flex gap-1 overflow-x-auto">
          {(['all', 'skill', 'agent', 'command'] as KindFilter[]).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setKind(k)}
              className={`shrink-0 whitespace-nowrap rounded-md border px-3 py-1.5 text-sm transition-colors ${
                kind === k
                  ? 'border-primary/30 bg-primary/10 text-primary font-medium'
                  : 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground-secondary'
              }`}
            >
              {k === 'all' ? 'All' : `${KIND_LABEL[k]}s`}
            </button>
          ))}
        </div>
      </div>
      )}

      {/* List + detail */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        {/* List — hidden on mobile when a detail is open */}
        <div className={selected ? 'hidden lg:block' : 'block'}>
          {mode === 'workflows' ? (
            <WorkflowsView items={catalog.items} selected={selected} onSelect={setSelected} />
          ) : total === 0 ? (
            <p className="text-sm text-muted-foreground">No capabilities match “{query}”.</p>
          ) : (
            <div className="space-y-5">
              {groups.map((g) => (
                <section key={g.label}>
                  <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {g.label} <span className="text-foreground-subtle">· {g.items.length}</span>
                  </h2>
                  <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
                    {g.items.map((item) => {
                      const active = selected?.kind === item.kind && selected?.name === item.name
                      return (
                        <li key={`${item.kind}:${item.name}`}>
                          <button
                            type="button"
                            onClick={() => setSelected(item)}
                            className={`flex w-full items-start gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted/50 ${active ? 'bg-muted/60' : ''}`}
                          >
                            <KindBadge kind={item.kind} />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm font-medium text-foreground">{item.display_name}</span>
                              <span className="mt-0.5 line-clamp-2 block text-xs text-muted-foreground">{item.description}</span>
                            </span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </div>

        {/* Detail */}
        <div className={selected ? 'block' : 'hidden lg:block'}>
          {!selected ? (
            <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
              Select a capability to see what it does.
            </div>
          ) : (
            <div className="rounded-lg border border-border bg-card lg:sticky lg:top-6">
              <div className="flex items-start justify-between gap-3 border-b border-border p-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <KindBadge kind={selected.kind} />
                    <span className="truncate font-mono text-xs text-muted-foreground">{selected.name}</span>
                  </div>
                  <h2 className="mt-1 text-lg font-semibold text-foreground">{selected.display_name}</h2>
                </div>
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  className="shrink-0 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground lg:hidden"
                >
                  ← Back
                </button>
              </div>
              <div className="p-4">
                <p className="text-sm leading-relaxed text-foreground-secondary">{selected.description}</p>
                {detailErr && <p className="mt-3 text-xs text-destructive">Couldn’t load details: {detailErr}</p>}
                {detail && detail.body && (
                  <div
                    className="
                      mt-4 border-t border-border pt-4 text-sm leading-relaxed text-foreground-secondary
                      [&_h1]:text-base [&_h1]:font-semibold [&_h1]:text-foreground [&_h1]:mt-4 [&_h1]:mb-1.5
                      [&_h2]:text-sm [&_h2]:font-semibold [&_h2]:text-foreground [&_h2]:mt-4 [&_h2]:mb-1.5
                      [&_h3]:text-sm [&_h3]:font-semibold [&_h3]:text-foreground-secondary [&_h3]:mt-3 [&_h3]:mb-1
                      [&_p]:my-2
                      [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:space-y-1
                      [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:space-y-1
                      [&_strong]:text-foreground [&_strong]:font-semibold
                      [&_a]:text-primary [&_a:hover]:underline
                      [&_code]:text-primary [&_code]:bg-background [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded [&_code]:text-[0.85em]
                      [&_pre]:bg-background [&_pre]:border [&_pre]:border-border [&_pre]:rounded-lg [&_pre]:p-3 [&_pre]:overflow-x-auto [&_pre]:my-3
                      [&_pre_code]:bg-transparent [&_pre_code]:p-0
                    "
                  >
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.body}</ReactMarkdown>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
