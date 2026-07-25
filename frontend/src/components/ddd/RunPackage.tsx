import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  deleteRun,
  getRun,
  type DddLink,
  type DddRunNarrative,
  type DddRunPackage,
} from '@/api/ddd'
import { sceneHashFragment, withSceneHash } from '@/lib/sceneHash'
import { withBase } from '@/lib/basePath'
import {
  runSectionDomId,
  useRunSectionNav,
  type RunSection,
} from './runSectionNav'

/**
 * Which sections a loaded run actually renders, in display order. Drives both
 * the anchored DOM ids here and the jump list in the left rail — empty
 * sections (no links, no previous runs) are omitted so the rail never points
 * at something that isn't on the page.
 */
function presentSections(): RunSection[] {
  // The run's first-class objects, in display order. All are always listed —
  // each section shows its own empty state when absent — so the rail mirrors
  // the object model and never points at something missing.
  const out: RunSection[] = [
    { id: 'video', label: 'Video' },
    { id: 'slides', label: 'Walkthrough slides' },
    { id: 'documentation', label: 'Documentation' },
    { id: 'narrative', label: 'Narrative' },
    { id: 'external', label: 'External systems' },
    { id: 'outputs', label: 'Outputs' },
  ]
  return out
}

/** Human label for an artifact role, so Outputs reads in product terms rather
 *  than raw role/kind codes. */
function roleLabel(role: string | null, kind: string): string {
  switch (role) {
    case 'hero_video':
      return 'Video'
    case 'deck':
      return 'Walkthrough slides'
    case 'docs':
      return 'Documentation'
    case 'clip':
      return 'Clip'
    default:
      return kind === 'video' ? 'Video' : 'Document'
  }
}

/** The display form of an artifact URL: its path, without the query string —
 *  which carries the ?t=<share_token>. The full URL stays the link's href, so
 *  the token is never rendered as visible text (nor shoulder-surfed from a demo). */
function displayUrl(url: string): string {
  const q = url.indexOf('?')
  return q === -1 ? url : url.slice(0, q)
}

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function Section({
  id,
  title,
  subtitle,
  children,
}: {
  id: string
  title: string
  subtitle?: string
  children: React.ReactNode
}) {
  return (
    <section id={runSectionDomId(id)} className="mt-6 scroll-mt-4">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </h2>
        {subtitle && <span className="text-[11px] text-muted-foreground">{subtitle}</span>}
        <span className="h-px flex-1 bg-muted" />
      </div>
      {children}
    </section>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground">
      {children}
    </div>
  )
}

/** A clear, visitable URL — opens the artifact's own page in a new tab. */
function OpenLink({ href, label = 'Open' }: { href: string; label?: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={href}
      className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 text-[11px] text-foreground-secondary transition-colors hover:border-input hover:text-primary"
    >
      <span className="truncate font-mono">{displayUrl(href)}</span>
      <span aria-hidden>↗</span>
      <span className="sr-only">{label}</span>
    </a>
  )
}

/** Embed a self-contained HTML artifact (slideshow or docs page) in a sandboxed
 *  iframe, with a clear open-in-new-tab URL above it. When `sceneHash` is a
 *  `#scene-N` deep-link, it's forwarded into both the embedded iframe and the
 *  open-in-new-tab URL so the deck opens on that scene (the deck's own JS reads
 *  its hash). Non-scene hashes are ignored by withSceneHash. */
function HtmlEmbed({
  contentUrl,
  viewerUrl,
  title,
  sceneHash = '',
}: {
  contentUrl: string
  viewerUrl: string
  title: string
  sceneHash?: string
}) {
  return (
    <div className="flex flex-col gap-2">
      <OpenLink href={withSceneHash(viewerUrl, sceneHash)} />
      <div className="overflow-hidden rounded-xl border border-border bg-white">
        <iframe
          src={withSceneHash(withBase(contentUrl), sceneHash)}
          title={title}
          sandbox="allow-scripts allow-same-origin"
          className="h-[70vh] w-full bg-white"
        />
      </div>
    </div>
  )
}

function NarrativeBlock({ narrative }: { narrative: DddRunNarrative }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      {narrative.story && (
        <p className="mb-4 whitespace-pre-line text-sm leading-relaxed text-foreground-secondary">
          {narrative.story}
        </p>
      )}
      {narrative.narration.length > 0 && (
        <ol className="flex flex-col gap-2">
          {narrative.narration.map((n, i) => {
            const persona = n.persona ? narrative.personas?.[n.persona] : undefined
            return (
              <li
                key={n.id ?? i}
                className="rounded-lg border border-border bg-background/40 px-3 py-2"
              >
                <div className="mb-0.5 flex items-center gap-2">
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {n.scene ?? i + 1}
                  </span>
                  {n.title && (
                    <span className="text-xs font-medium text-foreground-secondary">{n.title}</span>
                  )}
                  {persona?.name && (
                    <span
                      className="text-[10px] text-primary/80"
                      title={persona.role || ''}
                    >
                      {persona.name}
                    </span>
                  )}
                </div>
                <p className="text-xs leading-relaxed text-foreground-secondary">{n.text}</p>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

/** The external systems the run used or created — the live app pages, created
 *  entities, and any related docs/companions — each as a clean, visitable URL.
 *  Sourced from the run's links (reference = systems we touched; narrative /
 *  companion = related material). */
function ExternalSystemsBlock({ links }: { links: DddLink[] }) {
  const systems = links.filter((l) => l.kind === 'reference')
  const related = links.filter((l) => l.kind === 'narrative' || l.kind === 'companion')
  const row = (l: DddLink) => (
    <a
      key={`${l.kind}:${l.url}`}
      href={l.url}
      target="_blank"
      rel="noopener noreferrer"
      className="group flex items-center gap-3 rounded-lg border border-border bg-background/40 px-3 py-2 transition-colors hover:border-input hover:bg-background/70"
    >
      {(l.kind === 'narrative' || l.kind === 'companion') && (
        <span aria-hidden className="shrink-0 text-sm">
          {l.kind === 'narrative' ? '📖' : '🎞️'}
        </span>
      )}
      <span className="shrink-0 text-xs text-foreground-secondary">{l.label}</span>
      <span className="flex-1 truncate text-right font-mono text-[11px] text-muted-foreground transition-colors group-hover:text-primary">
        {l.url}
      </span>
      <span
        aria-hidden
        className="shrink-0 text-[11px] text-muted-foreground transition-colors group-hover:text-primary"
      >
        ↗
      </span>
    </a>
  )
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">{systems.map(row)}</div>
      {related.length > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className="text-[9px] uppercase tracking-wider text-muted-foreground">Related</h3>
          {related.map(row)}
        </div>
      )}
    </div>
  )
}

export function RunPackage({ runId }: { runId: string }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [run, setRun] = useState<DddRunPackage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  // A `#scene-N` deep-link on the run page (e.g. surfaced findings linking to
  // /ddd/<slug>/<runId>#scene-3) is forwarded into the embedded slides deck.
  // Non-scene hashes (the rail's own section anchors) normalize to '' here.
  const sceneHash = sceneHashFragment(location.hash)

  const nav = useRunSectionNav()
  // useState setters and the memoized jump are referentially stable, so pulling
  // them out keeps the scroll-spy effect from re-running on every active change.
  const setSections = nav?.setSections
  const setActiveId = nav?.setActiveId

  useEffect(() => {
    let cancelled = false
    setRun(null)
    setError(null)
    getRun(runId)
      .then((d) => !cancelled && setRun(d))
      .catch((e) => !cancelled && setError(String(e.message || e)))
    return () => {
      cancelled = true
    }
  }, [runId])

  // Publish the run's sections to the rail and scroll-spy the active one. A
  // section is "active" once it crosses into the top third of the scroll
  // container; the topmost such section in display order wins.
  useEffect(() => {
    if (!run || !setSections || !setActiveId) return
    const sections = presentSections()
    setSections(sections)

    const root = document.querySelector('[data-ddd-scroll]')
    const els = sections
      .map((s) => document.getElementById(runSectionDomId(s.id)))
      .filter((el): el is HTMLElement => el != null)

    const visible = new Set<string>()
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const id = e.target.id.replace('run-section-', '')
          if (e.isIntersecting) visible.add(id)
          else visible.delete(id)
        }
        const top = sections.find((s) => visible.has(s.id))
        if (top) setActiveId(top.id)
      },
      { root, rootMargin: '0px 0px -66% 0px', threshold: 0 },
    )
    els.forEach((el) => observer.observe(el))

    return () => {
      observer.disconnect()
      setSections([])
      setActiveId(null)
    }
  }, [run, setSections, setActiveId])

  // On a `#scene-N` deep-link, bring the slides deck into view once the run has
  // rendered — otherwise the reviewer lands at the top (video) with the deck's
  // forwarded scene below the fold. Runs once per (run, sceneHash) pair.
  useEffect(() => {
    if (!run || !sceneHash) return
    const el = document.getElementById(runSectionDomId('slides'))
    el?.scrollIntoView({ block: 'start' })
  }, [run, sceneHash])

  async function onDeleteRun() {
    if (!run) return
    if (
      !window.confirm(
        `Delete run ${run.run_id}?\n\nIts rendered video/deck files will be removed from Drive. This cannot be undone.`,
      )
    )
      return
    setDeleting(true)
    try {
      await deleteRun(run.run_id)
      navigate(`/ddd/${encodeURIComponent(run.narrative_slug)}`)
    } catch (err) {
      window.alert(`Delete failed: ${(err as Error).message || err}`)
      setDeleting(false)
    }
  }

  if (error)
    return <div className="p-8 text-sm text-destructive/90">Error: {error}</div>
  if (!run) return <div className="p-8 text-sm text-muted-foreground">Loading run…</div>

  // The clean, shareable RELEASE (summary) page for this run. It resolves for a
  // member whenever the run has at least one rendered artifact (build_release
  // picks a primary walkthrough from video/deck/docs) — so only offer the link
  // when one exists.
  const hasSummary = Boolean(
    run.video || run.slides || run.documentation || run.all_artifacts.length > 0,
  )
  const summaryHref = withBase(
    `/ddd-release/${encodeURIComponent(run.narrative_slug)}/${encodeURIComponent(run.run_id)}`,
  )

  return (
    <div className="mx-auto max-w-4xl px-8 py-6">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <Link
            to={`/ddd/${encodeURIComponent(run.narrative_slug)}`}
            className="text-[11px] uppercase tracking-wider text-muted-foreground hover:text-foreground-secondary"
          >
            {run.narrative_slug}
          </Link>
          <h1 className="font-mono text-xl font-semibold text-foreground">
            {run.run_id}
          </h1>
        </div>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          {hasSummary && (
            <a
              href={summaryHref}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the clean, shareable summary page for this run"
              className="inline-flex items-center gap-1.5 rounded-md border border-primary/30 bg-primary/10 px-3 py-1 text-primary transition-colors hover:bg-primary/20"
            >
              View summary page <span aria-hidden>↗</span>
            </a>
          )}
          {run.phase && (
            <span className="rounded border border-input bg-muted/60 px-2 py-0.5 text-foreground-secondary">
              {run.phase}
            </span>
          )}
          <span>{fmtDate(run.latest_at)}</span>
          <button
            type="button"
            onClick={onDeleteRun}
            disabled={deleting}
            className="rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-destructive/40 hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
          >
            {deleting ? 'Deleting…' : 'Delete run'}
          </button>
        </div>
      </header>

      <Section id="video" title="Video">
        {run.video ? (
          <div className="flex flex-col gap-2">
            <OpenLink href={run.video.viewer_url} />
            <div className="overflow-hidden rounded-xl border border-border bg-black">
              <video
                src={withBase(run.video.content_url)}
                controls
                className="max-h-[70vh] w-full bg-black"
              />
            </div>
          </div>
        ) : (
          <Empty>No video for this run.</Empty>
        )}
      </Section>

      <Section
        id="slides"
        title="Walkthrough slides"
        subtitle="canopy:walkthrough slideshow"
      >
        {run.slides ? (
          <HtmlEmbed
            contentUrl={run.slides.content_url}
            viewerUrl={run.slides.viewer_url}
            title={run.slides.title}
            sceneHash={sceneHash}
          />
        ) : (
          <Empty>No walkthrough slides for this run.</Empty>
        )}
      </Section>

      <Section id="documentation" title="Documentation" subtitle="feature docs page">
        {run.documentation ? (
          <HtmlEmbed
            contentUrl={run.documentation.content_url}
            viewerUrl={run.documentation.viewer_url}
            title={run.documentation.title}
          />
        ) : (
          <Empty>No documentation page for this run.</Empty>
        )}
      </Section>

      <Section
        id="narrative"
        title="Narrative"
        subtitle={run.narrative?.version != null ? `v${run.narrative.version}` : undefined}
      >
        {run.narrative?.review_id && (
          <a
            href={withBase(`/review/${run.narrative.review_id}`)}
            className="mb-2 inline-flex items-center gap-1.5 rounded-md border border-primary/30 bg-primary/10 px-3 py-1 text-xs text-primary transition-colors hover:bg-primary/20"
          >
            Edit narrative in review
            {run.narrative.version != null && ` (v${run.narrative.version})`}
            <span aria-hidden>→</span>
          </a>
        )}
        {run.narrative && (run.narrative.story || run.narrative.narration.length > 0) ? (
          <NarrativeBlock narrative={run.narrative} />
        ) : (
          <Empty>No narrative recorded for this run yet.</Empty>
        )}
      </Section>

      <Section
        id="external"
        title="External systems"
        subtitle="live systems this run used or created"
      >
        {run.links.length > 0 ? (
          <ExternalSystemsBlock links={run.links} />
        ) : (
          <Empty>No external systems recorded for this run.</Empty>
        )}
      </Section>

      <Section
        id="outputs"
        title="Outputs"
        subtitle={`${run.all_artifacts.length} file${run.all_artifacts.length === 1 ? '' : 's'} · every output we made, with a link`}
      >
        <div className="flex flex-col gap-1">
          {run.all_artifacts.map((a) => (
            <a
              key={a.id}
              href={a.viewer_url}
              target="_blank"
              rel="noopener noreferrer"
              title={a.viewer_url}
              className="group flex items-center gap-3 rounded-lg border border-border bg-background/40 px-3 py-1.5 text-xs transition-colors hover:border-input hover:bg-background/70"
            >
              <span className="w-28 shrink-0 text-[10px] font-medium text-foreground-secondary">
                {roleLabel(a.role, a.kind)}
              </span>
              <span className="flex-1 truncate text-foreground-secondary">{a.title}</span>
              <span className="shrink-0 text-muted-foreground">{fmtDate(a.created_at)}</span>
              <span
                aria-hidden
                className="shrink-0 truncate max-w-[16rem] font-mono text-[11px] text-muted-foreground transition-colors group-hover:text-primary"
              >
                {displayUrl(a.viewer_url)} ↗
              </span>
            </a>
          ))}
        </div>
      </Section>

    </div>
  )
}
