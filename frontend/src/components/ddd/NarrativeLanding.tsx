import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  deleteNarrative,
  deleteNarrativeVersion,
  deleteRun,
  getNarrative,
  setNarrativeVisibility,
  type DddNarrativeDetail,
  type DddNarrativeRun,
  type DddNarrativeVersion,
} from '@/api/ddd'
import { withBase } from '@/lib/basePath'
import { NarrativeDiff } from './NarrativeDiff'
import { pairNarrationScenes } from './narrativeScenePairing'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
  } catch {
    return iso
  }
}

/** Small ghost button that turns red on hover — the delete affordance. */
function DeleteButton({
  label,
  busy,
  onClick,
}: {
  label: string
  busy: boolean
  onClick: (e: React.MouseEvent) => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      title={label}
      className="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
    >
      {busy ? '…' : 'Delete'}
    </button>
  )
}

function RunCard({
  slug,
  run,
  latest,
  onDeleted,
}: {
  slug: string
  run: DddNarrativeRun
  latest: boolean
  onDeleted: () => void
}) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const href = `/ddd/${encodeURIComponent(slug)}/${encodeURIComponent(run.run_id)}`
  // The clean, shareable RELEASE (summary) page for this run. Offered only when
  // the run rendered something (video/deck) that the release page can show.
  const hasSummary = run.has_video || run.has_deck
  const summaryHref = withBase(
    `/ddd-release/${encodeURIComponent(slug)}/${encodeURIComponent(run.run_id)}`,
  )

  async function onDelete(e: React.MouseEvent) {
    e.stopPropagation()
    if (
      !window.confirm(
        `Delete run ${run.run_id}?\n\nIts rendered video/deck files will be removed from Drive. This cannot be undone.`,
      )
    )
      return
    setBusy(true)
    try {
      await deleteRun(run.run_id)
      onDeleted()
    } catch (err) {
      window.alert(`Delete failed: ${(err as Error).message || err}`)
      setBusy(false)
    }
  }

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={() => navigate(href)}
      onKeyDown={(e) => e.key === 'Enter' && navigate(href)}
      className="group cursor-pointer rounded-xl border border-border bg-card p-4 transition-colors hover:border-input"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-sm text-foreground-secondary">{run.run_id}</span>
        <div className="flex shrink-0 items-center gap-2">
          {latest && (
            <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[9px] text-primary">
              latest
            </span>
          )}
          {hasSummary && (
            <a
              href={summaryHref}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              title="Open the clean, shareable summary page for this run"
              className="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-primary transition-colors hover:bg-primary/10"
            >
              Summary ↗
            </a>
          )}
          <DeleteButton label="Delete run" busy={busy} onClick={onDelete} />
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        {run.gate && <span>{run.gate}</span>}
        {run.status && (
          <span className="rounded bg-muted px-1.5 py-0.5 text-foreground-secondary">{run.status}</span>
        )}
        <span>· {run.scene_count} scenes</span>
        {run.has_video && <span title="has video">🎬</span>}
        {run.has_deck && <span title="has deck">🖼️</span>}
        <span className="ml-auto">{fmtDate(run.latest_at)}</span>
      </div>
    </div>
  )
}

function VersionBlock({
  slug,
  version,
  previous,
  isCurrent,
  onChanged,
}: {
  slug: string
  version: DddNarrativeVersion
  previous?: DddNarrativeVersion
  isCurrent: boolean
  onChanged: () => void
}) {
  const [open, setOpen] = useState(isCurrent)
  const [busy, setBusy] = useState(false)
  const label = version.version != null ? `v${version.version}` : 'no narrative'
  // How many scenes this version changed vs the one before it — the thing that
  // actually distinguishes two versions whose story line (the title) is identical.
  const changedScenes =
    previous && previous.narration.length > 0 && version.narration.length > 0
      ? pairNarrationScenes(previous.narration, version.narration).filter(
          (p) => p.status !== 'unchanged',
        ).length
      : null

  async function onDelete(e: React.MouseEvent) {
    e.stopPropagation()
    if (version.version == null) return
    const n = version.runs.length
    if (
      !window.confirm(
        `Delete ${label}${version.title ? ` — "${version.title}"` : ''}?\n\n` +
          `This removes the version and its ${n} run${n === 1 ? '' : 's'} (rendered files included). This cannot be undone.`,
      )
    )
      return
    setBusy(true)
    try {
      await deleteNarrativeVersion(slug, version.version)
      onChanged()
    } catch (err) {
      window.alert(`Delete failed: ${(err as Error).message || err}`)
      setBusy(false)
    }
  }

  return (
    <section className="rounded-xl border border-border bg-background/30">
      <div className="flex items-center gap-2 px-4 py-3">
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <span aria-hidden className="text-muted-foreground">
            {open ? '▾' : '▸'}
          </span>
          <span className="font-mono text-xs text-primary">{label}</span>
          {isCurrent && (
            <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[9px] text-primary">
              current
            </span>
          )}
          <span className="truncate text-sm text-foreground-secondary">{version.title || ''}</span>
          {changedScenes != null && changedScenes > 0 && (
            <span className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-500">
              {changedScenes} scene{changedScenes === 1 ? '' : 's'} changed
            </span>
          )}
          <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
            {version.runs.length} run{version.runs.length === 1 ? '' : 's'} ·{' '}
            {fmtDate(version.created_at)}
          </span>
        </button>
        {version.version != null && (
          <DeleteButton label="Delete version" busy={busy} onClick={onDelete} />
        )}
      </div>

      {open && (
        <div className="border-t border-border px-4 py-3">
          {version.story && (
            <p className="mb-3 whitespace-pre-line text-sm leading-relaxed text-foreground-secondary">
              {version.story}
            </p>
          )}
          {previous && previous.narration.length > 0 && version.narration.length > 0 && (
            <NarrativeDiff
              before={previous.narration}
              after={version.narration}
              beforeLabel={previous.version != null ? `v${previous.version}` : 'previous'}
              afterLabel={label}
            />
          )}
          {version.video_url && (
            <video
              src={withBase(version.video_url)}
              controls
              preload="metadata"
              className="mb-3 w-full max-w-2xl rounded-lg border border-border bg-black"
            />
          )}
          {version.review_id && (
            <a
              href={withBase(`/review/${version.review_id}`)}
              className="mb-3 inline-flex items-center gap-1.5 rounded-md border border-primary/30 bg-primary/10 px-3 py-1 text-xs text-primary transition-colors hover:bg-primary/20"
            >
              Edit narrative <span aria-hidden>→</span>
            </a>
          )}
          {version.runs.length > 0 ? (
            <div className="grid gap-2 sm:grid-cols-2">
              {version.runs.map((r, i) => (
                <RunCard
                  key={r.run_id}
                  slug={slug}
                  run={r}
                  latest={isCurrent && i === 0}
                  onDeleted={onChanged}
                />
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-border px-4 py-4 text-center text-xs text-muted-foreground">
              No runs rendered from this version yet.
            </div>
          )}
        </div>
      )}
    </section>
  )
}

export function NarrativeLanding({ slug }: { slug: string }) {
  const navigate = useNavigate()
  const [detail, setDetail] = useState<DddNarrativeDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [deletingNarrative, setDeletingNarrative] = useState(false)
  const [vizBusy, setVizBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setError(null)
    getNarrative(slug)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(String(e.message || e)))
    return () => {
      cancelled = true
    }
  }, [slug, reloadKey])

  async function onDeleteNarrative() {
    const n = detail?.versions.length ?? 0
    if (
      !window.confirm(
        `Delete the ENTIRE narrative "${slug}"?\n\n` +
          `This removes all ${n} version${n === 1 ? '' : 's'} and every run + rendered file. This cannot be undone.`,
      )
    )
      return
    setDeletingNarrative(true)
    try {
      await deleteNarrative(slug)
      navigate('/ddd')
    } catch (err) {
      window.alert(`Delete failed: ${(err as Error).message || err}`)
      setDeletingNarrative(false)
    }
  }

  async function toggleVisibility() {
    if (!detail) return
    const makePublic = detail.visibility !== 'public'
    setVizBusy(true)
    try {
      const res = await setNarrativeVisibility(slug, makePublic)
      setDetail({ ...detail, visibility: res.visibility })
    } catch (err) {
      window.alert(`Could not change visibility: ${(err as Error).message || err}`)
    } finally {
      setVizBusy(false)
    }
  }

  if (error) return <div className="p-8 text-sm text-destructive/90">Error: {error}</div>
  if (!detail) return <div className="p-8 text-sm text-muted-foreground">Loading…</div>

  const currentVersion = detail.current_version?.version ?? null

  return (
    <div className="mx-auto max-w-4xl px-4 py-6 sm:px-8">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground">Narrative</div>
          <h1 className="text-2xl font-semibold text-foreground">{detail.title || detail.slug}</h1>
          <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
            <span className="font-mono">{detail.slug}</span>
            {detail.phase && (
              <span className="rounded border border-input bg-muted/60 px-2 py-0.5 text-foreground-secondary">
                {detail.phase}
              </span>
            )}
            {detail.project_slug && <span>· {detail.project_slug}</span>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={toggleVisibility}
            disabled={vizBusy}
            title="Toggle whether this whole narrative (video, deck, docs, reviews) is public"
            className={`rounded-lg border px-3 py-1 text-sm transition-colors disabled:opacity-50 ${
              detail.visibility === 'public'
                ? 'border-success/25 bg-success/10 text-success/90 hover:bg-success/20'
                : 'border-input bg-muted/60 text-foreground-secondary hover:bg-muted'
            }`}
          >
            {vizBusy
              ? '…'
              : detail.visibility === 'public'
                ? 'Public'
                : detail.visibility === 'mixed'
                  ? 'Mixed — make public'
                  : 'Private'}
          </button>
          <button
            type="button"
            onClick={onDeleteNarrative}
            disabled={deletingNarrative}
            className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-destructive/40 hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
          >
            {deletingNarrative ? 'Deleting…' : 'Delete narrative'}
          </button>
        </div>
      </header>

      <div className="mt-6 flex items-baseline gap-2">
        <h2 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Versions &amp; runs
        </h2>
        <span className="text-[11px] text-muted-foreground">{detail.versions.length}</span>
        <span className="h-px flex-1 bg-muted" />
      </div>

      <div className="mt-3 flex flex-col gap-3">
        {detail.versions.map((v, i) => (
          <VersionBlock
            key={v.review_id ?? 'unversioned'}
            slug={slug}
            version={v}
            previous={detail.versions[i + 1]}
            isCurrent={v.version != null && v.version === currentVersion}
            onChanged={() => setReloadKey((k) => k + 1)}
          />
        ))}
        {detail.versions.length === 0 && (
          <div className="rounded-xl border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
            No narrative versions yet.
          </div>
        )}
      </div>
    </div>
  )
}
