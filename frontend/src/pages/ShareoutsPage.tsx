import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  type Shareout,
  type ShareoutPeriod,
  groupByPeriod,
  shareoutsApi,
} from '@/api/shareouts'

// Shareout windows are tz-aware UTC timestamps. A window built from calendar
// dates is "day-aligned" (00:00:00 → 23:59:59 UTC) and renders as plain dates;
// a precise mid-day run renders with local times (the user's wall clock).
const pad = (n: number) => String(n).padStart(2, '0')
const utcYmd = (d: Date) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
const sameUtcDate = (a: Date, b: Date) => utcYmd(a) === utcYmd(b)

function isDayAligned(s: Date, e: Date): boolean {
  const start0 = s.getUTCHours() === 0 && s.getUTCMinutes() === 0 && s.getUTCSeconds() === 0
  const end1 = e.getUTCHours() === 23 && e.getUTCMinutes() === 59 && e.getUTCSeconds() === 59
  return start0 && end1
}

function formatPeriod(start: string, end: string): string {
  const s = new Date(start)
  const e = new Date(end)
  if (isDayAligned(s, e)) {
    const full: Intl.DateTimeFormatOptions = { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }
    const sd = s.toLocaleDateString(undefined, full)
    if (sameUtcDate(s, e)) return sd
    const ed = e.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
    return `${sd} – ${ed}`
  }
  const dOpt: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', year: 'numeric' }
  const tOpt: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' }
  if (s.toDateString() === e.toDateString()) {
    return `${s.toLocaleDateString(undefined, dOpt)}, ${s.toLocaleTimeString(undefined, tOpt)} – ${e.toLocaleTimeString(undefined, tOpt)}`
  }
  return `${s.toLocaleDateString(undefined, dOpt)}, ${s.toLocaleTimeString(undefined, tOpt)} – ${e.toLocaleDateString(undefined, dOpt)}, ${e.toLocaleTimeString(undefined, tOpt)}`
}

// Each `## Heading` in a briefing (What shipped / Why it matters / How to
// leverage it) renders as a labeled section: an orange uppercase label with a
// divider above it, so the three concepts are visually distinct blocks rather
// than one run of prose. Bullets get clear markers + breathing room.
//
// NOTE: this styles markdown with arbitrary `[&_el]:` descendant variants, NOT
// `@tailwindcss/typography`'s `prose`/`prose-*` modifiers — that plugin is not
// registered in index.css, so `prose-*` classes emit NO css in the production
// build (verified: the live stylesheet had zero prose rules). Arbitrary
// variants compile to plain utilities that always ship. Tailwind preflight
// strips list styling, so `list-disc` + padding are required for bullets.
function Markdown({ children }: { children: string }) {
  return (
    <div
      className="
        text-sm leading-relaxed text-foreground-secondary max-w-none
        [&_h2]:text-[11px] [&_h2]:font-bold [&_h2]:uppercase [&_h2]:tracking-[0.08em]
        [&_h2]:text-primary [&_h2]:mt-5 [&_h2]:mb-2 [&_h2]:pt-4
        [&_h2]:border-t [&_h2]:border-border
        [&_h2:first-child]:border-t-0 [&_h2:first-child]:pt-0 [&_h2:first-child]:mt-0
        [&_h3]:text-foreground-secondary [&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:mt-3 [&_h3]:mb-1
        [&_p]:text-foreground-secondary [&_p]:my-2
        [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:space-y-1.5
        [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:space-y-1.5
        [&_li]:text-foreground-secondary [&_li]:pl-1 [&_li]:marker:text-primary/70
        [&_strong]:text-foreground [&_strong]:font-semibold
        [&_a]:text-primary [&_a]:no-underline [&_a:hover]:underline
        [&_code]:text-primary [&_code]:bg-background [&_code]:px-1 [&_code]:py-0.5
        [&_code]:rounded [&_code]:text-[0.85em]
      "
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  )
}

function LinkChips({ links }: { links: Shareout['links'] }) {
  if (!links || links.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1.5 mt-3">
      {links.map((l, i) => (
        <a
          key={i}
          href={l.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-[11px] font-medium text-foreground-secondary hover:text-primary bg-background/80 border border-input/60 hover:border-primary/50 px-2 py-1 rounded-md transition-colors"
        >
          <span className="text-primary/70">↗</span>
          {l.label}
        </a>
      ))}
    </div>
  )
}

const PR_STATE_STYLE: Record<string, string> = {
  MERGED: 'text-special bg-special/10 border-special/30',
  OPEN: 'text-success bg-success/10 border-success/30',
  CLOSED: 'text-foreground-secondary bg-input/20 border-input/40',
}

function AllPRs({ prs }: { prs: Shareout['all_prs'] }) {
  if (!prs || prs.length === 0) return null
  return (
    <details className="mt-3 group">
      <summary className="cursor-pointer select-none text-[11px] font-medium text-foreground-secondary hover:text-foreground-secondary list-none flex items-center gap-1.5">
        <span className="transition-transform group-open:rotate-90 text-muted-foreground">▶</span>
        All {prs.length} PR{prs.length === 1 ? '' : 's'} this period
      </summary>
      <ul className="mt-2 ml-1 space-y-1 border-l border-border pl-3">
        {prs.map((pr, i) => (
          <li key={i} className="flex items-start gap-2 text-[12px]">
            <span
              className={`shrink-0 mt-0.5 text-[9px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border ${PR_STATE_STYLE[pr.state ?? ''] ?? 'text-foreground-secondary bg-muted/40 border-input/50'}`}
            >
              {pr.state || '—'}
            </span>
            <a
              href={pr.url}
              target="_blank"
              rel="noreferrer"
              className="text-foreground-secondary hover:text-primary leading-snug"
            >
              {pr.number != null && <span className="text-muted-foreground">#{pr.number} </span>}
              {pr.title}
            </a>
          </li>
        ))}
      </ul>
    </details>
  )
}

function RollupCard({ shareout }: { shareout: Shareout }) {
  return (
    <div className="bg-gradient-to-b from-card to-card/60 border border-primary/30 rounded-xl p-5 sm:p-6 shadow-lg shadow-black/20">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-primary bg-primary/10 border border-primary/20 px-2 py-0.5 rounded">
          Roll-up
        </span>
      </div>
      <h3 className="text-lg font-semibold text-foreground mb-2 leading-snug">{shareout.title}</h3>
      {shareout.summary && (
        <p className="text-sm text-foreground-secondary mb-3 leading-relaxed">{shareout.summary}</p>
      )}
      <Markdown>{shareout.content}</Markdown>
      <LinkChips links={shareout.links} />
    </div>
  )
}

function ProjectCard({ shareout }: { shareout: Shareout }) {
  return (
    <details className="group bg-card/70 border border-border rounded-xl overflow-hidden open:border-input open:bg-card transition-colors">
      <summary className="cursor-pointer select-none list-none p-4 sm:p-5 hover:bg-muted/30 transition-colors">
        <div className="flex items-start gap-3">
          <span className="mt-1 shrink-0 text-muted-foreground text-xs transition-transform group-open:rotate-90">▶</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              {shareout.project_slug && (
                <span className="text-[10px] font-semibold uppercase tracking-wide text-foreground-secondary bg-muted px-1.5 py-0.5 rounded">
                  {shareout.project_slug}
                </span>
              )}
              {shareout.all_prs && shareout.all_prs.length > 0 && (
                <span className="text-[10px] text-muted-foreground">
                  {shareout.all_prs.length} PR{shareout.all_prs.length === 1 ? '' : 's'}
                </span>
              )}
            </div>
            <h3 className="text-[15px] font-semibold text-foreground mt-1.5 leading-snug">{shareout.title}</h3>
            {shareout.summary && (
              <p className="text-[13px] text-foreground-secondary mt-1 leading-relaxed">{shareout.summary}</p>
            )}
          </div>
        </div>
      </summary>
      <div className="px-4 sm:px-5 pb-5 pl-10 sm:pl-11">
        <Markdown>{shareout.content}</Markdown>
        <LinkChips links={shareout.links} />
        <AllPRs prs={shareout.all_prs} />
      </div>
    </details>
  )
}

// Compact rail label: "Tue, Jun 3" (day), "May 30 – Jun 1" (span), or
// "Jun 4, 2:30 PM" (a precise mid-day run).
function formatRail(start: string, end: string): string {
  const s = new Date(start)
  const e = new Date(end)
  if (isDayAligned(s, e)) {
    const o: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', timeZone: 'UTC' }
    if (sameUtcDate(s, e)) {
      return s.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' })
    }
    return `${s.toLocaleDateString(undefined, o)} – ${e.toLocaleDateString(undefined, o)}`
  }
  return `${s.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${s.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`
}

function prTotal(period: ShareoutPeriod): number {
  return period.projects.reduce((n, p) => n + (p.all_prs?.length ?? 0), 0)
}

// Shareable URL slug, built from UTC so it's identical for every viewer:
// a date (single day), `start_end` (day span), or `YYYY-MM-DD-HHMM` (a precise
// mid-day run). Routed at /shareouts/:period.
function periodSlug(p: ShareoutPeriod): string {
  const s = new Date(p.periodStart)
  const e = new Date(p.periodEnd)
  if (isDayAligned(s, e)) {
    return sameUtcDate(s, e) ? utcYmd(s) : `${utcYmd(s)}_${utcYmd(e)}`
  }
  return `${utcYmd(s)}-${pad(s.getUTCHours())}${pad(s.getUTCMinutes())}`
}

function PeriodRail({
  periods,
  activeKey,
}: {
  periods: ShareoutPeriod[]
  activeKey: string
}) {
  return (
    <nav
      aria-label="Shareouts by date"
      className="min-w-0 md:w-56 md:shrink-0 md:sticky md:top-6 md:self-start"
    >
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-2 px-2">
        By date
      </div>
      <ul className="flex md:flex-col gap-1 overflow-x-auto md:overflow-visible pb-1">
        {periods.map((p) => {
          const active = p.key === activeKey
          // Real link → URL updates, browser back works, and the user can
          // right-click → copy link to share this exact shareout.
          return (
            <li key={p.key} className="shrink-0">
              <Link
                to={`/shareouts/${periodSlug(p)}`}
                aria-current={active ? 'true' : undefined}
                className={`block rounded-lg border px-3 py-2 transition-colors ${
                  active
                    ? 'bg-muted/70 border-primary/40 text-foreground'
                    : 'bg-transparent border-transparent text-foreground-secondary hover:bg-card hover:text-foreground-secondary'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`h-3 w-[3px] rounded-full ${active ? 'bg-primary' : 'bg-transparent'}`}
                  />
                  <span className="text-[13px] font-medium whitespace-nowrap">
                    {formatRail(p.periodStart, p.periodEnd)}
                  </span>
                </div>
                <div className="text-[10px] text-muted-foreground mt-0.5 ml-[11px] whitespace-nowrap">
                  {p.projects.length} project{p.projects.length === 1 ? '' : 's'}
                  {prTotal(p) > 0 && ` · ${prTotal(p)} PRs`}
                </div>
              </Link>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

function CopyLinkButton({ period }: { period: ShareoutPeriod }) {
  const [copied, setCopied] = useState(false)
  const url = `${window.location.origin}/shareouts/${periodSlug(period)}`
  async function copy() {
    try {
      await navigator.clipboard.writeText(url)
    } catch {
      // clipboard blocked (e.g. insecure context) — fall back to selecting nothing
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }
  return (
    <button
      onClick={copy}
      title={`Copy link to this shareout — ${url}`}
      className="shrink-0 inline-flex items-center gap-1.5 text-[11px] font-medium text-foreground-secondary hover:text-primary border border-border hover:border-primary/40 bg-card px-2.5 py-1 rounded-md transition-colors"
    >
      <span className="text-primary/70">🔗</span>
      {copied ? 'Copied!' : 'Copy link'}
    </button>
  )
}

// The producer (if any) is uniform across a period — every card in a run
// carries the same slug — so read it once from the roll-up, else the first
// project. Empty/absent = a human ran it (render nothing).
function periodProducer(period: ShareoutPeriod): string {
  return (period.rollup?.produced_by_agent || period.projects[0]?.produced_by_agent || '')
}

function PeriodMain({ period }: { period: ShareoutPeriod }) {
  const producer = periodProducer(period)
  return (
    <section className="min-w-0 flex-1 max-w-3xl">
      <div className="mb-5 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-foreground">
            {formatPeriod(period.periodStart, period.periodEnd)}
          </h2>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            {period.projects.length} project{period.projects.length === 1 ? '' : 's'}
            {prTotal(period) > 0 && ` · ${prTotal(period)} PRs`}
            {producer && (
              <>
                {' · produced by '}
                <Link
                  to={`/agents/${producer}`}
                  className="text-muted-foreground hover:text-primary transition-colors"
                >
                  {producer}
                </Link>
              </>
            )}
          </p>
        </div>
        <CopyLinkButton period={period} />
      </div>
      <div className="space-y-3">
        {period.rollup && <RollupCard shareout={period.rollup} />}
        {period.projects.map((s) => (
          <ProjectCard key={s.id} shareout={s} />
        ))}
      </div>
    </section>
  )
}

export function ShareoutsPage() {
  const { period: periodParam } = useParams()
  const navigate = useNavigate()
  const [periods, setPeriods] = useState<ShareoutPeriod[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void (async () => {
      try {
        const data = await shareoutsApi.list({ limit: 200 })
        if (!cancelled) setPeriods(groupByPeriod(data)) // newest period first
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load shareouts')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Active period is URL-driven: /shareouts/:period selects that one (so it's a
  // shareable permalink); bare /shareouts defaults to the most recent. An
  // unknown/stale slug falls back to the most recent and rewrites the URL.
  const matched = periodParam ? periods.find((p) => periodSlug(p) === periodParam) : undefined
  const active = matched ?? periods[0]
  useEffect(() => {
    if (!loading && periodParam && periods.length > 0 && !matched) {
      navigate('/shareouts', { replace: true })
    }
  }, [loading, periodParam, periods, matched, navigate])

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-foreground">Shareouts</h1>
        <p className="text-[13px] text-muted-foreground mt-1">
          What shipped, why it matters, and how to leverage it. Pick a date on the left; tap a project to read the briefing.
        </p>
      </div>

      {error && (
        <div className="flex items-center justify-center h-48 text-destructive text-sm">{error}</div>
      )}

      {loading && (
        <div className="space-y-3 max-w-3xl">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="bg-card border border-border rounded-xl p-5 animate-pulse"
              style={{ animationDelay: `${i * 80}ms` }}
            >
              <div className="h-3 bg-muted rounded w-24 mb-3" />
              <div className="h-4 bg-muted rounded w-2/3 mb-2" />
              <div className="h-3 bg-muted/70 rounded w-full" />
            </div>
          ))}
        </div>
      )}

      {!loading && !error && periods.length > 0 && active && (
        <div className="flex flex-col md:flex-row gap-6 md:gap-10">
          <PeriodRail periods={periods} activeKey={active.key} />
          <PeriodMain period={active} />
        </div>
      )}

      {!loading && !error && periods.length === 0 && (
        <div className="flex flex-col items-center justify-center h-48 text-center">
          <p className="text-sm text-muted-foreground mb-1">No shareouts yet.</p>
          <p className="text-xs text-foreground-subtle">
            Run{' '}
            <code className="text-primary/70 bg-card px-1.5 py-0.5 rounded">
              canopy:shareout
            </code>{' '}
            to publish a briefing.
          </p>
        </div>
      )}
    </div>
  )
}
