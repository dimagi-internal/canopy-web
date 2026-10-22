import { useCallback, useEffect, useMemo, useState, type FormEvent, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Button, Input, StatusBadge, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import type { StatusTone } from 'canopy-ui/lib'

import {
  decideKbFiles,
  getKbReview,
  listKbVersions,
  setKbReaders,
  type KbApprovalIn,
  type KbDriveFileOut,
  type KbReviewOut,
  type KbReviewSourceOut,
  type KbVersionOut,
} from '@/api/kb'
import { ApiError } from '@/api/problem'
import { Switch } from '@/components/supervisor/SettingRow'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '@/lib/scopedLinks'

// A KNOWLEDGE BASE'S SOURCES AND ITS OWNER'S REVIEW — `/w/:workspace/projects/:handle/kb`.
// A Drive source is read only as far as its project's owner switches it on here:
// nothing under its folders reaches the steward until then. Everyone who may see the
// KB (owner, steward, named readers) sees this page; only the owner gets controls.
// Every decision re-fetches the review, because a folder's decision cascades on the
// server and the page must show what the server now holds, not a local guess.

const GOOGLE = 'application/vnd.google-apps.'

/** A short type label from a Drive mime type. */
export function kbTypeLabel(f: Pick<KbDriveFileOut, 'is_folder' | 'mime_type'>): string {
  if (f.is_folder || f.mime_type === `${GOOGLE}folder`) return 'Folder'
  switch (f.mime_type) {
    case `${GOOGLE}document`:
      return 'Doc'
    case `${GOOGLE}spreadsheet`:
      return 'Sheet'
    case `${GOOGLE}presentation`:
      return 'Slides'
    case 'application/pdf':
      return 'PDF'
    default:
      return 'file'
  }
}

/** The one status a row shows, most telling first. */
export function kbStatus(f: KbDriveFileOut): { label: string; tone: StatusTone } {
  if (!f.present) return { label: 'Gone', tone: 'neutral' }
  if (f.skip_reason) return { label: 'Skipped', tone: 'neutral' }
  if (f.held_headers.length && !f.hold_released) return { label: `Held: ${f.held_headers.join(', ')}`, tone: 'warning' }
  if (f.queued) return { label: 'New since approval', tone: 'info' }
  if (f.decision === 'approved') return { label: 'Approved', tone: 'success' }
  if (f.decision === 'excluded') return { label: 'Excluded', tone: 'destructive' }
  return { label: 'Pending', tone: 'neutral' }
}

/** Depth-first rows: each folder's children under it, folders first, then by name. */
export function kbTree(files: readonly KbDriveFileOut[]): { file: KbDriveFileOut; depth: number }[] {
  const ids = new Set(files.map((f) => f.file_id))
  const children = new Map<string, KbDriveFileOut[]>()
  const roots: KbDriveFileOut[] = []
  for (const f of files) {
    if (f.parent_id && ids.has(f.parent_id) && f.parent_id !== f.file_id) {
      const list = children.get(f.parent_id) ?? []
      list.push(f)
      children.set(f.parent_id, list)
    } else {
      roots.push(f)
    }
  }
  const order = (a: KbDriveFileOut, b: KbDriveFileOut) =>
    Number(b.is_folder) - Number(a.is_folder) || a.name.localeCompare(b.name)
  const out: { file: KbDriveFileOut; depth: number }[] = []
  const seen = new Set<string>()
  const walk = (list: KbDriveFileOut[], depth: number) => {
    for (const f of [...list].sort(order)) {
      if (seen.has(f.file_id)) continue // a cycle in Drive's parents never loops the page
      seen.add(f.file_id)
      out.push({ file: f, depth })
      walk(children.get(f.file_id) ?? [], depth + 1)
    }
  }
  walk(roots, 0)
  return out
}

function shortDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

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

type Decide = (source: number, files: KbApprovalIn[], key: string) => void

export function KnowledgeBasePage(): JSX.Element {
  const { workspace = '', handle = '' } = useParams()
  const key = `${workspace}/${handle}`
  const [review, setReview] = useState<{ key: string; data: KbReviewOut } | null>(null)
  const [versions, setVersions] = useState<KbVersionOut[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const reload = useCallback(
    () =>
      Promise.all([getKbReview(workspace, handle), listKbVersions(workspace, handle)]).then(([r, v]) => {
        setReview({ key, data: r })
        setVersions(v)
      }),
    [workspace, handle, key],
  )

  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    Promise.all([getKbReview(workspace, handle), listKbVersions(workspace, handle)])
      .then(([r, v]) => {
        if (cancelled) return
        setReview({ key, data: r })
        setVersions(v)
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

  const run = useCallback(
    (busyKey: string, action: () => Promise<unknown>) => {
      setBusy(busyKey)
      setActionError(null)
      action()
        .then(() => reload())
        .catch((err: unknown) => setActionError(errorText(err, 'That change did not go through')))
        .finally(() => setBusy(null))
    },
    [reload],
  )

  const decide: Decide = useCallback(
    (source, files, busyKey) => run(busyKey, () => decideKbFiles(workspace, handle, source, files)),
    [run, workspace, handle],
  )

  const data = review?.key === key ? review.data : null
  if (loadError) return <p className="p-6 text-sm text-destructive">{loadError}</p>
  if (!data) return <p className="p-6 text-sm text-muted-foreground">Loading {key}…</p>

  const drive = data.sources.filter((s) => s.kind === 'drive_folders')
  const web = data.sources.filter((s) => s.kind !== 'drive_folders')

  return (
    <div className="max-w-6xl" data-testid="kb-page">
      <h1 className="text-xl font-semibold text-foreground">Knowledge base · {data.name}</h1>
      <p className="mt-1 text-[12px] text-muted-foreground">
        <span className="font-mono">{data.address}</span>
        {' · '}owner {data.owner_email ?? 'none'}
        {' · '}
        {data.version != null ? `serving version ${data.version}` : 'not published yet'}
        {' · '}
        <Link to={scopedPath(workspace, `/projects/${handle}`)} className="text-primary hover:underline">
          project page
        </Link>
      </p>
      <p className="mt-2 text-[13px] text-foreground-secondary">
        Nothing in these folders is read until the owner switches it on. Flagged files look like they may hold personal
        data — check them before approving.
      </p>
      {!data.may_decide && (
        <p className="mt-2 text-[12px] text-muted-foreground" data-testid="kb-not-owner">
          Only the project&apos;s owner ({data.owner_email ?? 'not set'}) can change what is read.
        </p>
      )}
      {actionError && (
        <p className="mt-2 text-[12px] text-destructive" role="alert">
          {actionError}
        </p>
      )}

      {data.sources.length === 0 && (
        <p className="mt-4 rounded border border-border bg-card p-4 text-[13px] text-foreground-secondary">
          This knowledge base has no sources yet. Its steward adds one (POST …/kb/sources).
        </p>
      )}

      {web.length > 0 && <WebSources sources={web} />}

      {drive.map((s) => (
        <DriveSource key={s.id} source={s} mayDecide={data.may_decide} busy={busy} decide={decide} />
      ))}

      <Readers
        review={data}
        steward={data.steward_slug}
        busy={busy}
        onChange={(change, busyKey) =>
          run(busyKey, () => setKbReaders(workspace, handle, change))
        }
      />

      <Versions versions={versions ?? []} />
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

function WebSources({ sources }: { sources: KbReviewSourceOut[] }): JSX.Element {
  return (
    <Section title="Web sources" testId="kb-web-sources">
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
    </Section>
  )
}

function DriveSource({
  source,
  mayDecide,
  busy,
  decide,
}: {
  source: KbReviewSourceOut
  mayDecide: boolean
  busy: string | null
  decide: Decide
}): JSX.Element {
  const files = source.files ?? []
  const rows = useMemo(() => kbTree(files), [files])
  const queued = files.filter((f) => f.queued && f.present)
  const skipped = files.filter((f) => f.skip_reason)
  const inventoried = source.inventoried_at
    ? `inventoried ${relativeAge(source.inventoried_at, new Date(), { seconds: false })}`
    : 'not inventoried yet'

  return (
    <Section title={`Drive · ${source.name}`} testId={`kb-drive-source-${source.id}`}>
      <p className="-mt-1 mb-2 text-[12px] text-muted-foreground">
        {files.length} {files.length === 1 ? 'item' : 'items'} · {inventoried}
      </p>
      {files.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">
          Nothing listed yet: the steward lists the folders&apos; names and types (never their contents) first.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Owner</TableHead>
                <TableHead>Modified</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Flags</TableHead>
                {mayDecide && <TableHead>Read it</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map(({ file, depth }) => (
                <FileRow
                  key={file.file_id}
                  file={file}
                  depth={depth}
                  sourceId={source.id}
                  mayDecide={mayDecide}
                  busy={busy}
                  decide={decide}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {queued.length > 0 && (
        <div className="mt-4" data-testid={`kb-queued-${source.id}`}>
          <h3 className="mb-1 text-[13px] font-semibold text-foreground">New since approval · {queued.length}</h3>
          <p className="mb-2 text-[12px] text-muted-foreground">
            These appeared in an approved folder after it was approved. They wait here until the owner decides.
          </p>
          <div className="rounded-lg border border-border">
            <Table>
              <TableBody>
                {queued.map((f) => (
                  <TableRow key={f.file_id}>
                    <TableCell className="text-foreground">
                      <FileName file={f} />
                    </TableCell>
                    <TableCell className="text-[12px]">{f.path}</TableCell>
                    <TableCell className="text-[12px]">{kbTypeLabel(f)}</TableCell>
                    <TableCell>
                      <Flags flags={f.flags} />
                    </TableCell>
                    {mayDecide && (
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy !== null}
                          onClick={() => decide(source.id, [{ file_id: f.file_id, decision: 'approved' }], `q:${f.file_id}`)}
                        >
                          Approve
                        </Button>{' '}
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy !== null}
                          onClick={() => decide(source.id, [{ file_id: f.file_id, decision: 'excluded' }], `q:${f.file_id}`)}
                        >
                          Exclude
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}

      {skipped.length > 0 && (
        <div className="mt-4" data-testid={`kb-skipped-${source.id}`}>
          <h3 className="mb-1 text-[13px] font-semibold text-foreground">Skipped · {skipped.length}</h3>
          <p className="mb-2 text-[12px] text-muted-foreground">Never read, whatever is decided, for the reason given.</p>
          <div className="rounded-lg border border-border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Size</TableHead>
                  <TableHead>Why</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {skipped.map((f) => (
                  <TableRow key={f.file_id}>
                    <TableCell className="text-foreground">
                      <FileName file={f} />
                    </TableCell>
                    <TableCell className="text-[12px]">{kbTypeLabel(f)}</TableCell>
                    <TableCell className="text-[12px]">{size(f.size)}</TableCell>
                    <TableCell className="whitespace-normal text-[12px]">{f.skip_reason}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </Section>
  )
}

function FileName({ file }: { file: KbDriveFileOut }): JSX.Element {
  const cls = file.present ? 'text-foreground hover:text-primary hover:underline' : 'text-muted-foreground line-through'
  return file.web_url ? (
    <a href={file.web_url} target="_blank" rel="noreferrer" className={cls} title={file.path}>
      {file.name || file.file_id}
    </a>
  ) : (
    <span className={cls} title={file.path}>
      {file.name || file.file_id}
    </span>
  )
}

function Flags({ flags }: { flags: readonly string[] }): JSX.Element | null {
  if (!flags.length) return null
  return (
    <div className="space-y-0.5">
      {flags.map((f) => (
        <div key={f} className="whitespace-normal text-[11px] text-warning">
          {f}
        </div>
      ))}
    </div>
  )
}

function FileRow({
  file,
  depth,
  sourceId,
  mayDecide,
  busy,
  decide,
}: {
  file: KbDriveFileOut
  depth: number
  sourceId: number
  mayDecide: boolean
  busy: string | null
  decide: Decide
}): JSX.Element {
  const status = kbStatus(file)
  const on = file.decision === 'approved'
  const held = file.held_headers.length > 0 && !file.hold_released
  const rowBusy = busy === `f:${file.file_id}`
  return (
    <TableRow data-testid={`kb-file-${file.file_id}`}>
      <TableCell className="max-w-[28rem] truncate">
        <span style={{ paddingLeft: `${depth * 16}px` }} className="inline-block">
          {file.is_folder && <span className="mr-1 text-muted-foreground">▸</span>}
          <FileName file={file} />
        </span>
      </TableCell>
      <TableCell className="text-[12px]">{kbTypeLabel(file)}</TableCell>
      <TableCell className="text-[12px]">{file.owner}</TableCell>
      <TableCell className="text-[12px]">{shortDate(file.modified_time)}</TableCell>
      <TableCell>
        <StatusBadge tone={status.tone} className="max-w-[16rem] truncate" title={status.label}>
          {status.label}
        </StatusBadge>
      </TableCell>
      <TableCell>
        <Flags flags={file.flags} />
      </TableCell>
      {mayDecide && (
        <TableCell>
          {file.present && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
              <span className="inline-flex items-center gap-1.5">
                <Switch
                  checked={on}
                  disabled={busy !== null}
                  label={`Read ${file.name}`}
                  testId={`kb-switch-${file.file_id}`}
                  onChange={() =>
                    decide(sourceId, [{ file_id: file.file_id, decision: on ? 'excluded' : 'approved' }], `f:${file.file_id}`)
                  }
                />
                {rowBusy ? (
                  <span className="text-muted-foreground">…</span>
                ) : (
                  file.decision === 'pending' && <span className="text-muted-foreground">pending</span>
                )}
              </span>
              {file.is_folder && (
                <label className="inline-flex items-center gap-1 text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={file.auto_approve}
                    disabled={busy !== null}
                    onChange={(e) =>
                      decide(sourceId, [{ file_id: file.file_id, auto_approve: e.target.checked }], `f:${file.file_id}`)
                    }
                    className="accent-primary"
                  />
                  Auto-approve new files
                </label>
              )}
              {held && (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy !== null}
                  onClick={() => decide(sourceId, [{ file_id: file.file_id, release_hold: true }], `f:${file.file_id}`)}
                >
                  Release hold
                </Button>
              )}
            </div>
          )}
        </TableCell>
      )}
    </TableRow>
  )
}

function Readers({
  review,
  steward,
  busy,
  onChange,
}: {
  review: KbReviewOut
  steward: string
  busy: string | null
  onChange: (change: { add?: string[]; remove?: string[] }, busyKey: string) => void
}): JSX.Element {
  const [email, setEmail] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const value = email.trim()
    if (!value) return
    onChange({ add: [value] }, 'readers:add')
    setEmail('')
  }
  return (
    <Section title="Who can query this knowledge base" testId="kb-readers">
      <div className="rounded-lg border border-border">
        <Table>
          <TableBody>
            <TableRow>
              <TableCell className="text-foreground">{review.owner_email ?? '—'}</TableCell>
              <TableCell className="text-[12px] text-muted-foreground">owner · always</TableCell>
              {review.may_decide && <TableCell />}
            </TableRow>
            <TableRow>
              <TableCell className="font-mono text-[12px] text-foreground">{steward || 'its steward agent'}</TableCell>
              <TableCell className="text-[12px] text-muted-foreground">steward · always</TableCell>
              {review.may_decide && <TableCell />}
            </TableRow>
            {review.readers.map((r) => (
              <TableRow key={r}>
                <TableCell className="text-foreground">{r}</TableCell>
                <TableCell className="text-[12px] text-muted-foreground">reader</TableCell>
                {review.may_decide && (
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy !== null}
                      onClick={() => onChange({ remove: [r] }, `readers:${r}`)}
                    >
                      Remove
                    </Button>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {review.may_decide && (
        <form onSubmit={submit} className="mt-2 flex max-w-md items-center gap-2">
          <Input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@example.org"
            aria-label="Add a reader by email"
            className="h-8 text-[13px]"
          />
          <Button type="submit" size="sm" disabled={busy !== null || !email.trim()}>
            Add reader
          </Button>
        </form>
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
