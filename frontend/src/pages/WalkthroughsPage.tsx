import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  listWalkthroughs,
  type WalkthroughListItem,
  type WalkthroughKind,
} from '../api/walkthroughs'
import { useScopedPath } from '../lib/scopedLinks'
import { Td, Th } from '@/components/DataTable'

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function VisibilityChip({ v }: { v: 'private' | 'link' }) {
  const cls =
    v === 'link'
      ? 'bg-success/10 text-success'
      : 'bg-muted text-foreground-secondary'
  return (
    <span className={`px-2 py-0.5 text-xs rounded ${cls}`}>
      {v === 'link' ? 'Link' : 'Private'}
    </span>
  )
}

export function WalkthroughsPage() {
  const scope = useScopedPath()
  const [params, setParams] = useSearchParams()
  const project = params.get('project') ?? ''
  const kind = (params.get('kind') as WalkthroughKind | null) ?? null
  const mine = params.get('mine') === 'true'

  const [items, setItems] = useState<WalkthroughListItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setItems(null)
    setError(null)
    listWalkthroughs({
      project: project || undefined,
      kind: kind ?? undefined,
      mine: mine || undefined,
    })
      .then((data) => {
        if (!cancelled) setItems(data)
      })
      .catch((e) => {
        if (!cancelled) setError(String(e.message || e))
      })
    return () => {
      cancelled = true
    }
  }, [project, kind, mine])

  const distinctProjects = useMemo(() => {
    if (!items) return []
    const s = new Set<string>()
    for (const w of items) if (w.project_slug) s.add(w.project_slug)
    return [...s].sort()
  }, [items])

  function update(key: string, value: string | null) {
    const next = new URLSearchParams(params)
    if (value == null || value === '') next.delete(key)
    else next.set(key, value)
    setParams(next, { replace: true })
  }

  return (
    <div className="max-w-6xl mx-auto p-6">
      <header className="flex items-baseline justify-between mb-6">
        <h1 className="text-2xl font-semibold">Walkthroughs</h1>
        <p className="text-sm text-muted-foreground">
          Sharable demos uploaded from <code>/canopy:walkthrough</code>
        </p>
      </header>

      <div className="flex gap-3 mb-4 text-sm">
        <select
          className="border rounded px-2 py-1"
          value={project}
          onChange={(e) => update('project', e.target.value)}
        >
          <option value="">All projects</option>
          {distinctProjects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <select
          className="border rounded px-2 py-1"
          value={kind ?? ''}
          onChange={(e) => update('kind', e.target.value || null)}
        >
          <option value="">All kinds</option>
          <option value="html">HTML</option>
          <option value="video">Video</option>
        </select>
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={mine}
            onChange={(e) => update('mine', e.target.checked ? 'true' : null)}
          />
          Mine only
        </label>
      </div>

      {error && (
        <div className="text-destructive text-sm mb-3">Failed: {error}</div>
      )}
      {items === null && !error && (
        <div className="text-muted-foreground text-sm">Loading…</div>
      )}
      {items && items.length === 0 && (
        <div className="text-muted-foreground text-sm">No walkthroughs match.</div>
      )}
      {items && items.length > 0 && (
        <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead className="text-left text-xs text-muted-foreground border-b">
            <tr>
              <Th density="roomy-gap" weight="inherit">Title</Th>
              <Th density="roomy-gap" weight="inherit">Project</Th>
              <Th density="roomy-gap" weight="inherit">Kind</Th>
              <Th density="roomy-gap" weight="inherit">Owner</Th>
              <Th density="roomy-gap" weight="inherit">Visibility</Th>
              <Th density="roomy-gap" weight="inherit">Size</Th>
              <Th density="roomy-gap" weight="inherit">Created</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((w) => (
              <tr key={w.id} className="border-b hover:bg-muted/50">
                <Td density="roomy-gap">
                  <Link to={scope(`/walkthrough/${encodeURIComponent(w.id)}`)} className="text-primary hover:underline">
                    {w.title}
                  </Link>
                </Td>
                <Td density="roomy-gap">
                  {w.project_slug ? (
                    <Link to={`/?project=${w.project_slug}`} className="text-foreground-secondary hover:underline">
                      {w.project_slug}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </Td>
                <Td density="roomy-gap" className="capitalize">{w.kind}</Td>
                <Td density="roomy-gap">{w.owner_email}</Td>
                <Td density="roomy-gap">
                  <VisibilityChip v={w.visibility} />
                </Td>
                <Td density="roomy-gap">{formatBytes(w.size_bytes)}</Td>
                <Td density="roomy-gap" tone="muted">
                  {new Date(w.created_at).toLocaleDateString()}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  )
}
