import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { StatusBadge, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import type { StatusTone } from 'canopy-ui/lib'

import { listKbSources, listKbVersions, type KbSourceOut, type KbVersionOut } from '@/api/kb'
import { ApiError } from '@/api/problem'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'

// A KNOWLEDGE BASE — `/w/:workspace/projects/:handle/kb`: where its documents come
// from and its version log. Its steward agent publishes each version as a curator
// bundle; this page only shows what was published.

function size(bytes: number | null | undefined): string {
  if (bytes == null) return '—'
  if (bytes < 1e3) return `${bytes} B`
  if (bytes < 1e6) return `${(bytes / 1e3).toFixed(1)} KB`
  if (bytes < 1e9) return `${(bytes / 1e6).toFixed(1)} MB`
  return `${(bytes / 1e9).toFixed(1)} GB`
}

function errorText(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.detail || err.message || fallback
  return err instanceof Error ? err.message : fallback
}

export function KnowledgeBasePage(): JSX.Element {
  const { workspace = '', handle = '' } = useParams()
  const key = `${workspace}/${handle}`
  const [data, setData] = useState<{ key: string; sources: KbSourceOut[]; versions: KbVersionOut[] } | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    Promise.all([listKbSources(workspace, handle), listKbVersions(workspace, handle)])
      .then(([sources, versions]) => {
        if (!cancelled) setData({ key, sources, versions })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLoadError(
          err instanceof ApiError && err.status === 404
            ? `No knowledge base ${key} that you can see.`
            : errorText(err, 'Could not load the knowledge base'),
        )
      })
    return () => {
      cancelled = true
    }
  }, [workspace, handle, key])

  if (loadError) return <p className="p-6 text-sm text-destructive">{loadError}</p>
  if (!data || data.key !== key) return <p className="p-6 text-sm text-muted-foreground">Loading {key}…</p>

  const current = data.versions.find((v) => v.current)

  return (
    <div className="max-w-6xl" data-testid="kb-page">
      <h1 className="text-xl font-semibold text-foreground">Knowledge base · {key}</h1>
      <p className="mt-1 text-[12px] text-muted-foreground">
        {current ? `serving version ${current.number}` : 'not published yet'}
        {' · '}
        <Link to={scopedPath(workspace, `/projects/${handle}`)} className="text-primary hover:underline">
          project page
        </Link>
      </p>

      <Sources sources={data.sources} />
      <Versions versions={data.versions} />
    </div>
  )
}

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId?: string }) {
  return (
    <section className="mt-6" data-testid={testId}>
      <h2 className="mb-2 text-sm font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  )
}

function configSummary(config: Record<string, unknown>): string {
  const parts = Object.entries(config).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : String(v)}`)
  return parts.join(' · ') || '—'
}

function Sources({ sources }: { sources: KbSourceOut[] }): JSX.Element {
  return (
    <Section title="Sources" testId="kb-sources">
      {sources.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">No sources recorded.</p>
      ) : (
        <div className="rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Config</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sources.map((s) => (
                <TableRow key={s.id}>
                  <TableCell className="text-foreground">{s.name}</TableCell>
                  <TableCell>{s.kind}</TableCell>
                  <TableCell className="whitespace-normal text-[12px]">{configSummary(s.config)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </Section>
  )
}

const VERSION_TONE: Record<KbVersionOut['status'], StatusTone> = {
  pending: 'neutral',
  building: 'info',
  ready: 'success',
  failed: 'destructive',
}

function Versions({ versions }: { versions: KbVersionOut[] }): JSX.Element {
  return (
    <Section title="Versions" testId="kb-versions">
      {versions.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">No versions published yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>+ / ~ / −</TableHead>
                <TableHead>Uploaded</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>By</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Finished</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {versions.map((v) => (
                <TableRow key={v.number}>
                  <TableCell className="font-mono text-foreground">
                    {v.number}
                    {v.current && <span className="ml-1.5 text-[11px] text-success">current</span>}
                  </TableCell>
                  <TableCell className="text-[12px]">{v.kind}</TableCell>
                  <TableCell>
                    <StatusBadge tone={VERSION_TONE[v.status]} title={v.error || undefined}>
                      {v.status}
                    </StatusBadge>
                    {v.error && <div className="mt-0.5 max-w-xs whitespace-normal text-[11px] text-destructive">{v.error}</div>}
                  </TableCell>
                  <TableCell className="font-mono text-[12px]">
                    {v.added} / {v.changed} / {v.removed}
                  </TableCell>
                  <TableCell className="text-[12px]">{size(v.uploaded_bytes)}</TableCell>
                  <TableCell className="text-[12px]">{size(v.kb_bytes)}</TableCell>
                  <TableCell className="text-[12px]">{v.published_by}</TableCell>
                  <TableCell className="text-[12px]">{relativeAge(v.created_at, new Date(), { seconds: false })}</TableCell>
                  <TableCell className="text-[12px]">
                    {v.finished_at ? relativeAge(v.finished_at, new Date(), { seconds: false }) : '—'}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </Section>
  )
}
