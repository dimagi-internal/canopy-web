import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import {
  getNarrative,
  listNarratives,
  type DddNarrativeDetail,
  type DddNarrativeListItem,
} from '@/api/ddd'
import { listReviews, type ReviewListItem } from '@/api/reviews'
import { WorkbenchRail, WorkbenchNavItem } from 'canopy-ui'
import { useRunSectionNav } from './runSectionNav'

/**
 * Run-child product-findings review entry, nested under its run. These are NOT
 * narrative-version rows (the backend forces narrative_slug=None / version=0);
 * we surface them here so a run with an open findings review reads as "needs
 * input" right in the rail. Distinguished purely by gate === 'product_findings'.
 */
function FindingsReviewEntry({
  review,
  active,
}: {
  review: ReviewListItem
  active: boolean
}) {
  const pending = review.status !== 'resolved'
  return (
    <Link
      to={`/review/${encodeURIComponent(review.id)}`}
      className={clsx(
        'flex items-center gap-2 rounded-md px-3 py-0.5 text-[11px] transition-colors',
        active
          ? 'text-primary'
          : pending
            ? 'text-warning/90 hover:text-warning'
            : 'text-muted-foreground hover:text-foreground',
      )}
    >
      <span
        aria-hidden
        className={clsx(
          'h-1 w-1 shrink-0 rounded-full',
          pending ? 'bg-warning' : 'bg-muted-foreground',
        )}
      />
      <span className="truncate">
        Findings review · {pending ? 'needs input' : 'resolved'}
      </span>
    </Link>
  )
}

function runStamp(runId: string): string {
  // "microplans-2026-06-02-001" -> "2026-06-02-001"
  const m = runId.match(/(\d{4}-\d{2}-\d{2}-\d+)$/)
  return m ? m[1] : runId
}

/**
 * Jump-list of the active run's sections, nested under its run entry. The run
 * package registers these and reports the active one via scroll-spy (see
 * runSectionNav); clicking scrolls the package to that section so the rail
 * navigates a run instead of forcing one long scroll. Renders nothing until the
 * package has mounted and published its sections.
 */
function RunSectionList() {
  const nav = useRunSectionNav()
  if (!nav || nav.sections.length === 0) return null
  return (
    <div className="ml-6 mt-0.5 flex flex-col gap-0.5 border-l border-border/70 pl-1">
      {nav.sections.map((s) => (
        <button
          key={s.id}
          type="button"
          onClick={() => nav.jump(s.id)}
          className={clsx(
            'flex items-center gap-2 rounded-md px-3 py-0.5 text-left text-[11px] transition-colors',
            s.id === nav.activeId
              ? 'text-primary'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          <span
            aria-hidden
            className={clsx(
              'h-1 w-1 shrink-0 rounded-full',
              s.id === nav.activeId ? 'bg-primary' : 'bg-muted-foreground',
            )}
          />
          <span className="truncate">{s.label}</span>
        </button>
      ))}
    </div>
  )
}

/**
 * The active narrative's versions, each with its runs nested beneath — mirrors
 * the "Versions & runs" structure on the narrative page. Newest version first;
 * the current version carries a badge.
 */
function NarrativeRuns({
  slug,
  activeRunId,
}: {
  slug: string
  activeRunId?: string
}) {
  const [detail, setDetail] = useState<DddNarrativeDetail | null>(null)
  // Run-child product-findings reviews, grouped by run_id. Sourced from the reviews
  // list (the narrative API doesn't carry run-children), filtered to
  // gate === 'product_findings' so they never show as version rows.
  //
  // Scoping to THIS narrative is structural, not a filter: the render only ever looks
  // up findingsByRun.get(run_id) for runs already in this narrative's version tree, so
  // a review belonging to another narrative is simply never consulted. A run-child
  // review carries no narrative_slug to compare against anyway — it attaches by the
  // run it names, which is the only honest link it has.
  const [findingsByRun, setFindingsByRun] = useState<Map<string, ReviewListItem[]>>(new Map())

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    getNarrative(slug)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null))
    return () => {
      cancelled = true
    }
  }, [slug])

  useEffect(() => {
    let cancelled = false
    listReviews({ q: slug })
      .then((reviews) => {
        if (cancelled) return
        const byRun = new Map<string, ReviewListItem[]>()
        for (const r of reviews) {
          if (r.gate !== 'product_findings') continue
          const list = byRun.get(r.run_id) ?? []
          list.push(r)
          byRun.set(r.run_id, list)
        }
        setFindingsByRun(byRun)
      })
      .catch(() => !cancelled && setFindingsByRun(new Map()))
    return () => {
      cancelled = true
    }
  }, [slug])

  if (!detail) {
    return <div className="px-3 py-1 text-[11px] text-muted-foreground">Loading runs…</div>
  }

  // Highlight a findings-review entry when its /review/:id page is open.
  const reviewMatch = window.location.pathname.match(/^\/review\/([^/]+)/)
  const activeReviewId = reviewMatch ? decodeURIComponent(reviewMatch[1]) : undefined

  const currentVersion = detail.current_version?.version ?? null
  // Newest version first, matching the narrative page ordering.
  const versions = [...detail.versions].sort(
    (a, b) => (b.version ?? 0) - (a.version ?? 0),
  )
  if (versions.length === 0) {
    return <div className="px-3 py-1 text-[11px] text-muted-foreground">No versions yet</div>
  }

  return (
    <div className="ml-2 flex flex-col gap-1 border-l border-border pl-1">
      {versions.map((v) => {
        const isCurrent = v.version != null && v.version === currentVersion
        const label = v.version != null ? `v${v.version}` : 'no narrative'
        // The version's edit/review page — same destination as the "Edit
        // narrative" button on the narrative landing page. The null/"no
        // narrative" bucket has no review_id and stays non-clickable.
        const isVersionActive = v.review_id != null && activeReviewId === v.review_id
        const runs = [...v.runs].sort((a, b) =>
          (b.latest_at || '').localeCompare(a.latest_at || ''),
        )
        const versionRow = (
          <>
            <span className="font-mono text-[11px] font-medium">{label}</span>
            {isCurrent && (
              <span className="rounded bg-primary/10 px-1 py-px text-[9px] font-medium uppercase tracking-wide text-primary">
                current
              </span>
            )}
          </>
        )
        return (
          <div key={v.review_id ?? `v${v.version ?? 'none'}`} className="flex flex-col gap-0.5">
            {v.review_id != null ? (
              <Link
                to={`/review/${encodeURIComponent(v.review_id)}`}
                className={clsx(
                  'flex items-center gap-1.5 rounded-md px-3 py-0.5 transition-colors',
                  isVersionActive
                    ? 'bg-primary/10 text-primary border border-primary/30'
                    : 'text-foreground hover:bg-accent border border-transparent',
                )}
              >
                {versionRow}
              </Link>
            ) : (
              <div className="flex items-center gap-1.5 px-3 py-0.5 text-foreground">
                {versionRow}
              </div>
            )}
            {runs.length === 0 ? (
              <div className="px-3 py-0.5 pl-5 text-[11px] text-muted-foreground">No runs</div>
            ) : (
              <div className="ml-3 flex flex-col gap-0.5 border-l border-border/70 pl-1">
                {runs.map((r) => (
                  <div key={r.run_id} className="flex flex-col gap-0.5">
                    <Link
                      to={`/ddd/${encodeURIComponent(slug)}/${encodeURIComponent(r.run_id)}`}
                      className={clsx(
                        'flex items-center gap-2 rounded-md px-3 py-1 text-xs transition-colors',
                        r.run_id === activeRunId
                          ? 'bg-primary/10 text-primary border border-primary/30'
                          : 'text-muted-foreground hover:bg-accent hover:text-foreground border border-transparent',
                      )}
                    >
                      <span aria-hidden className="text-muted-foreground">
                        {r.run_id === activeRunId ? '●' : '○'}
                      </span>
                      <span className="truncate font-mono">{runStamp(r.run_id)}</span>
                    </Link>
                    {r.run_id === activeRunId && <RunSectionList />}
                    {(findingsByRun.get(r.run_id) ?? []).length > 0 && (
                      <div className="ml-6 mt-0.5 flex flex-col gap-0.5 border-l border-border/70 pl-1">
                        {(findingsByRun.get(r.run_id) ?? []).map((fr) => (
                          <FindingsReviewEntry
                            key={fr.id}
                            review={fr}
                            active={activeReviewId === fr.id}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function DddLeftNav({
  activeSlug,
  activeRunId,
}: {
  activeSlug?: string
  activeRunId?: string
}) {
  const [narratives, setNarratives] = useState<DddNarrativeListItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [mine, setMine] = useState(false)
  const [project, setProject] = useState('')

  useEffect(() => {
    let cancelled = false
    setNarratives(null)
    listNarratives({ project: project || undefined, mine })
      .then((d) => !cancelled && setNarratives(d))
      .catch((e) => !cancelled && setError(String(e.message || e)))
    return () => {
      cancelled = true
    }
  }, [mine, project])

  const projects = Array.from(
    new Set((narratives ?? []).map((n) => n.project_slug).filter(Boolean) as string[]),
  ).sort()

  const header = (
    <>
      <div className="px-4 py-3">
        <Link to="/ddd" className="text-sm font-semibold text-foreground hover:text-foreground/80">
          Narratives
        </Link>
        <p className="text-[11px] text-muted-foreground">DDD runs, grouped by narrative</p>
      </div>
      <div className="flex flex-col gap-2 border-t border-border px-3 py-2">
        <select
          value={project}
          onChange={(e) => setProject(e.target.value)}
          className="rounded-md border border-border bg-card px-2 py-1 text-xs text-foreground"
        >
          <option value="">All projects</option>
          {projects.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />
          Mine only
        </label>
      </div>
    </>
  )

  return (
    <WorkbenchRail width="md:w-72" header={header}>
      <nav className="px-2 py-2">
        {error && <div className="px-3 py-2 text-xs text-destructive">{error}</div>}
        {!narratives && !error && (
          <div className="px-3 py-2 text-xs text-muted-foreground">Loading…</div>
        )}
        {narratives && narratives.length === 0 && (
          <div className="px-3 py-2 text-xs text-muted-foreground">No narratives yet</div>
        )}
        <div className="flex flex-col gap-1">
          {(narratives ?? []).map((n) => {
            const isActive = n.slug === activeSlug
            return (
              <div key={n.slug}>
                <Link to={`/ddd/${encodeURIComponent(n.slug)}`} title={n.title ?? undefined}>
                  <WorkbenchNavItem active={isActive} count={n.run_count} variant="neutral">
                    {n.slug}
                  </WorkbenchNavItem>
                </Link>
                {isActive && <NarrativeRuns slug={n.slug} activeRunId={activeRunId} />}
              </div>
            )
          })}
        </div>
      </nav>
    </WorkbenchRail>
  )
}
