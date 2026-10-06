import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { AnimatePresence, LayoutGroup, motion } from 'framer-motion'
import { type Project, projectsApi } from '@/api/projects'
import { relativeAge } from '@/lib/relativeAge'

const SPRING = { type: 'spring' as const, stiffness: 400, damping: 35 }

// Curated hygiene checklist rendered in the expanded card's "Actions" column.
// `key` MUST match the exact `skill_name` written via POST /api/projects/{slug}/actions/
// (canopy skills are namespaced with `canopy:`; other writers use bare names).
// `label` is what the user sees — keep it clean and prefix-free.
// Update this list as new hygiene skills are added to the portfolio workflow.
const HYGIENE_ACTIONS: Array<{ key: string; label: string }> = [
  { key: 'code-review', label: 'code-review' },
  { key: 'canopy:doc-regen', label: 'doc-regen' },
  { key: 'canopy:improve', label: 'improve' },
  { key: 'canopy:pm-scout', label: 'pm-scout' },
]

// This page had its own third copy of "how long ago", and it was the one that
// fell back to `toLocaleDateString()` past a week — which is how the freshness
// line read "Refreshed 5/30/2026": an ambiguous absolute date, in a US format,
// sitting among relative times everywhere else, hiding the fact that it was 101
// days old. `relativeAge` keeps climbing (d → w → mo → y) so the age stays
// legible instead of turning into a date the reader has to subtract.
const relativeTime = (iso: string): string => relativeAge(iso)

// A project is considered stale (and dropped behind the "Show stale" toggle)
// if its DB status is `stale` or `archived`, OR if its most recent summary is
// older than 7 days. We mirror the per-tile staleness threshold used on the
// collapsed tile's left-border so the visual hint and the page-level grouping
// stay in sync.
const STALE_DAYS = 7
function isProjectStale(p: Project): boolean {
  if (p.status === 'stale' || p.status === 'archived') return true
  const summaryDate = p.latest_context?.summary?.created_at
  if (!summaryDate) return false
  const ageDays = (Date.now() - new Date(summaryDate).getTime()) / (1000 * 60 * 60 * 24)
  return ageDays > STALE_DAYS
}

function StatusDot({ status }: { status: string }) {
  const color = status === 'active'
    ? 'bg-primary shadow-[0_0_6px_rgba(251,146,60,0.3)]'
    : status === 'stale'
      ? 'bg-muted-foreground'
      : 'bg-input'
  return <span className={`w-[7px] h-[7px] rounded-full shrink-0 ${color}`} />
}

function DeployBadge({ url, compact }: { url: string; compact?: boolean }) {
  if (!url) return null
  const hostname = (() => {
    try { return new URL(url).hostname.replace('www.', '') } catch { return url }
  })()
  return (
    <a href={url} target="_blank" rel="noopener noreferrer"
      title={hostname}
      className={`flex items-center gap-1.5 min-w-0 ${compact ? 'text-[10px] max-w-[110px]' : 'text-[11px] max-w-[240px]'} bg-muted text-foreground-secondary px-2 py-0.5 rounded hover:text-foreground-secondary transition-colors overflow-hidden`}
      onClick={(e) => e.stopPropagation()}>
      <span className="w-1.5 h-1.5 rounded-full bg-success shrink-0 animate-pulse-slow shadow-[0_0_4px_rgba(74,222,128,0.4)]" />
      <span className="truncate">{hostname}</span>
    </a>
  )
}

function PrivateBadge() {
  return (
    <span className="text-[9px] text-muted-foreground border border-primary/15 bg-primary/5 px-1.5 py-0.5 rounded uppercase tracking-wide">
      private
    </span>
  )
}

function CollapsedTile({ project, onExpand }: { project: Project; onExpand: () => void }) {
  const ctx = project.latest_context || {}
  const summaryText = ctx.summary?.content || ctx.current_work?.content
  const summaryDate = ctx.summary?.created_at
  const isStale = summaryDate
    ? (Date.now() - new Date(summaryDate).getTime()) / (1000 * 60 * 60 * 24) > 7
    : false
  const borderClass = isStale
    ? 'border border-l-2 border-border border-l-amber-400/30 hover:border-input hover:border-l-amber-400/50'
    : 'border border-border hover:border-input'
  return (
    <div
      className={`bg-card ${borderClass} rounded-lg p-4 cursor-pointer transition-colors h-full`}
      onClick={onExpand}
    >
      <div className="flex items-center gap-3 mb-2 min-w-0">
        <StatusDot status={project.status} />
        <span className="text-sm font-semibold text-foreground truncate min-w-0">{project.name}</span>
        <div className="ml-auto flex items-center gap-2 min-w-0 shrink">
          {project.visibility === 'private' && <PrivateBadge />}
          {project.deploy_url && <DeployBadge url={project.deploy_url} compact />}
        </div>
      </div>
      <div className="text-xs text-muted-foreground leading-relaxed line-clamp-2">
        {summaryText || <span className="text-foreground-subtle italic">No summary yet</span>}
      </div>
    </div>
  )
}

function ExpandedCard({ project, onClose, scrollIntoViewOnMount }: {
  project: Project
  onClose: () => void
  scrollIntoViewOnMount?: boolean
}) {
  const [skillsExpanded, setSkillsExpanded] = useState(false)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const ctx = project.latest_context || {}
  const skills = project.skills || []

  useEffect(() => {
    if (scrollIntoViewOnMount && cardRef.current) {
      cardRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
    // Only run on mount; the parent flips this flag exactly once per deep link.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div
      ref={cardRef}
      className="bg-card border border-input rounded-xl overflow-hidden"
      onClick={(e) => e.stopPropagation()}
    >
      {/* Header */}
      <div className="flex items-center gap-3 px-6 py-4 border-b border-border">
        <StatusDot status={project.status} />
        <span className="text-base font-bold text-foreground">{project.name}</span>
        {project.visibility === 'private' && <PrivateBadge />}
        {project.deploy_url && <DeployBadge url={project.deploy_url} />}
        <div className="ml-auto flex items-center gap-4 text-[11px]">
          {project.repo_url && (
            <a href={project.repo_url} target="_blank" rel="noopener noreferrer"
              className="text-primary/70 hover:text-primary transition-colors"
              onClick={(e) => e.stopPropagation()}>
              GitHub ↗
            </a>
          )}
          {project.deploy_url && (
            <a href={project.deploy_url} target="_blank" rel="noopener noreferrer"
              className="text-primary/70 hover:text-primary transition-colors"
              onClick={(e) => e.stopPropagation()}>
              Live Site ↗
            </a>
          )}
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onClose() }}
          className="text-foreground-subtle hover:text-foreground-secondary text-lg ml-2"
          aria-label="Close"
        >✕</button>
      </div>

      {/* Body — 3 columns */}
      <div className="grid grid-cols-1 md:grid-cols-3 divide-x divide-border">
        {/* Column 1: Summary */}
        <div className="p-6">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground font-semibold mb-4">Summary</div>
          {ctx.summary ? (
            <div>
              <div className="bg-background border-l-2 border-primary rounded-r-lg p-3 text-xs text-foreground-secondary leading-relaxed">
                {ctx.summary.content}
              </div>
              <div className="text-[10px] text-foreground-subtle mt-2">
                {ctx.summary.source} · {new Date(ctx.summary.created_at).toLocaleDateString()}
              </div>
            </div>
          ) : (
            <div className="text-xs text-foreground-subtle italic">No summary yet — run canopy:activity-summary</div>
          )}
        </div>

        {/* Column 2: Actions */}
        <div className="p-6">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground font-semibold mb-4">Actions</div>
          {(() => {
            const actions = project.latest_actions || {}
            return (
              <div className="space-y-2">
                {HYGIENE_ACTIONS.map(({ key, label }) => {
                  const action = actions[key]
                  return (
                    <div key={key} className="flex items-center justify-between text-[11px]">
                      <span className="text-foreground-secondary">{label}</span>
                      {action ? (
                        <div className="flex items-center gap-2">
                          <span className="text-muted-foreground">{relativeTime(action.completed_at || action.started_at)}</span>
                          {action.status === 'completed' && <span className="text-success">&#10003;</span>}
                          {action.status === 'failed' && <span className="text-destructive">&#10007;</span>}
                          {action.status === 'started' && <span className="text-primary">&#9679;</span>}
                        </div>
                      ) : (
                        <span className="text-foreground-subtle">never</span>
                      )}
                    </div>
                  )
                })}
              </div>
            )
          })()}
        </div>

        {/* Column 3: Details + Skills */}
        <div className="p-6">
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground font-semibold mb-4">Details</div>
          <div className="space-y-2 mb-6">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted-foreground">Repo</span>
              {project.repo_url ? (
                <a href={project.repo_url} target="_blank" rel="noopener noreferrer"
                  className="text-primary/80 hover:text-primary truncate ml-3 max-w-[180px]"
                  onClick={(e) => e.stopPropagation()}>
                  {project.repo_url.replace('https://github.com/', '')}
                </a>
              ) : <span className="text-foreground-subtle">—</span>}
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted-foreground">Deploy</span>
              {project.deploy_url ? (
                <a href={project.deploy_url} target="_blank" rel="noopener noreferrer"
                  className="text-primary/80 hover:text-primary truncate ml-3 max-w-[180px]"
                  onClick={(e) => e.stopPropagation()}>
                  {(() => { try { return new URL(project.deploy_url).hostname } catch { return project.deploy_url } })()}
                </a>
              ) : <span className="text-foreground-subtle">—</span>}
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted-foreground">Visibility</span>
              <span className="text-foreground-secondary">{project.visibility}</span>
            </div>
          </div>

          {/* Last actions */}
          {project.latest_actions && Object.keys(project.latest_actions).length > 0 && (
            <div className="mb-6">
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">Last actions</div>
              <div className="space-y-1">
                {Object.entries(project.latest_actions).map(([name, action]) => (
                  <div key={name} className="flex items-center justify-between text-[11px]">
                    <span className="text-foreground-secondary truncate mr-2">{name}</span>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-muted-foreground">{relativeTime(action.completed_at || action.started_at)}</span>
                      {action.status === 'completed' && <span className="text-success">&#10003;</span>}
                      {action.status === 'failed' && <span className="text-destructive">&#10007;</span>}
                      {action.status === 'started' && <span className="text-primary">&#9679;</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Walkthroughs link */}
          {project.walkthrough_count && project.walkthrough_count > 0 ? (
            <div className="mb-3">
              <Link
                to={`/walkthroughs?project=${project.slug}`}
                onClick={(e) => e.stopPropagation()}
                className="text-[11px] text-foreground-secondary hover:text-foreground"
              >
                Walkthroughs · {project.walkthrough_count}
              </Link>
            </div>
          ) : null}

          {/* Skills (collapsible) */}
          <div>
            <button
              onClick={(e) => { e.stopPropagation(); setSkillsExpanded(!skillsExpanded) }}
              className="flex items-center gap-2 text-[9px] uppercase tracking-wider text-muted-foreground font-semibold mb-2 hover:text-foreground-secondary transition-colors">
              <span>Skills ({skills.length})</span>
              <span>{skillsExpanded ? '▾' : '▸'}</span>
            </button>
            {skillsExpanded && (
              <div className="space-y-1">
                {skills.length === 0 ? (
                  <div className="text-[11px] text-foreground-subtle italic">No skills discovered</div>
                ) : (
                  skills.map((skill, i) => (
                    <div key={i} className="text-[11px] text-foreground-secondary py-1">
                      <div className="font-medium text-foreground-secondary">{skill.name}</div>
                      {skill.description && (
                        <div className="text-muted-foreground text-[10px] line-clamp-2">{skill.description}</div>
                      )}
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-lg font-semibold text-foreground">Projects</h1>
        <span className="text-xs text-foreground-subtle bg-card px-2.5 py-1 rounded">
          loading…
        </span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="bg-card border border-border rounded-lg p-4 animate-pulse"
            style={{ animationDelay: `${i * 60}ms` }}
          >
            <div className="flex items-center gap-3 mb-3">
              <div className="w-[7px] h-[7px] rounded-full bg-muted shrink-0" />
              <div className="h-3 bg-muted rounded w-1/2" />
            </div>
            <div className="space-y-2">
              <div className="h-2 bg-muted/70 rounded w-full" />
              <div className="h-2 bg-muted/70 rounded w-4/5" />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Ordered list of slugs that are expanded; first = most-recently clicked (top of stack).
  const [expandedOrder, setExpandedOrder] = useState<string[]>([])
  const [searchParams, setSearchParams] = useSearchParams()
  // The slug that the URL asked us to expand; we strip it from the URL once
  // applied so refreshing the page doesn't keep re-scrolling.
  const [pendingScrollSlug, setPendingScrollSlug] = useState<string | null>(null)
  // Stale projects (status=stale|archived OR summary >7d old) are tucked behind
  // this toggle so the daily-check-in surface leads with what's actually hot.
  const [showStale, setShowStale] = useState(false)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void (async () => {
      try {
        // Server order: list_projects returns most recently updated first.
        const projectsData = await projectsApi.list()
        if (cancelled) return
        setProjects(projectsData)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load projects')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [])

  // Handle ?expand=<slug> deep links (bookmarks, links from elsewhere in the app).
  // Wait until projects load so we only expand a slug that actually exists.
  useEffect(() => {
    if (loading) return
    const target = searchParams.get('expand')
    if (!target) return
    if (!projects.some((p) => p.slug === target)) {
      // Unknown slug — drop the param silently rather than leaving a stale URL.
      const next = new URLSearchParams(searchParams)
      next.delete('expand')
      setSearchParams(next, { replace: true })
      return
    }
    setExpandedOrder((order) => (order.includes(target) ? order : [target, ...order]))
    setPendingScrollSlug(target)
    const next = new URLSearchParams(searchParams)
    next.delete('expand')
    setSearchParams(next, { replace: true })
    // Clear the scroll flag after the ExpandedCard has had a chance to mount
    // and run its scrollIntoView, so a later collapse+re-expand of the same
    // card doesn't keep auto-scrolling.
    const t = window.setTimeout(() => setPendingScrollSlug(null), 600)
    return () => window.clearTimeout(t)
  }, [loading, projects, searchParams, setSearchParams])

  function expand(slug: string) {
    setExpandedOrder((order) => (order.includes(slug) ? order : [slug, ...order]))
  }

  function collapse(slug: string) {
    setExpandedOrder((order) => order.filter((s) => s !== slug))
    if (pendingScrollSlug === slug) setPendingScrollSlug(null)
  }

  if (loading) {
    return <LoadingSkeleton />
  }
  if (error) {
    return <div className="flex items-center justify-center h-64 text-destructive text-sm">{error}</div>
  }

  const expandedSet = new Set(expandedOrder)
  const expandedProjects = expandedOrder
    .map((slug) => projects.find((p) => p.slug === slug))
    .filter((p): p is Project => Boolean(p))
  const collapsedAll = projects.filter((p) => !expandedSet.has(p.slug))
  // Hot grid leads; stale grid is hidden behind the toggle. Expanded cards
  // are NOT filtered — if the user expanded a stale card, it stays open.
  const collapsedHot = collapsedAll.filter((p) => !isProjectStale(p))
  const collapsedStale = collapsedAll.filter((p) => isProjectStale(p))

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-lg font-semibold text-foreground">Projects</h1>
        <span className="text-xs text-muted-foreground bg-card px-2.5 py-1 rounded">
          {projects.length} projects
        </span>
      </div>

      {!loading && projects.length === 0 && (
        <div className="mb-6 rounded-xl border border-border bg-card px-5 py-6">
          <h2 className="text-sm font-semibold text-foreground">No projects in this workspace</h2>
          <p className="mt-1 max-w-prose text-xs leading-relaxed text-muted-foreground">
            Projects belong to a workspace. Switch workspace in the header to reach projects that
            live elsewhere.
          </p>
        </div>
      )}

      <LayoutGroup>
        {/* Stack of expanded cards at the top */}
        <div className="space-y-3 mb-3">
          <AnimatePresence initial={false}>
            {expandedProjects.map((project) => (
              <motion.div
                key={project.id}
                layout
                initial={{ opacity: 0, y: -8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8 }}
                transition={SPRING}
              >
                <ExpandedCard
                  project={project}
                  onClose={() => collapse(project.slug)}
                  scrollIntoViewOnMount={pendingScrollSlug === project.slug}
                />
              </motion.div>
            ))}
          </AnimatePresence>
        </div>

        {/* Collapsed tile grid — active projects */}
        <motion.div
          layout
          className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2"
          transition={SPRING}
        >
          <AnimatePresence initial={false}>
            {collapsedHot.map((project, i) => (
              <motion.div
                key={project.id}
                layout
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 8 }}
                transition={{ ...SPRING, delay: Math.min(i * 0.03, 0.3), duration: 0.2 }}
              >
                <CollapsedTile project={project} onExpand={() => expand(project.slug)} />
              </motion.div>
            ))}
          </AnimatePresence>
        </motion.div>

        {/* Stale toggle + (optional) stale grid */}
        {collapsedStale.length > 0 && (
          <div className="mt-6">
            <button
              onClick={() => setShowStale((v) => !v)}
              className="text-[11px] text-muted-foreground hover:text-foreground-secondary transition-colors flex items-center gap-2"
              aria-expanded={showStale}
            >
              <span>{showStale ? '▾' : '▸'}</span>
              <span>{showStale ? 'Hide' : 'Show'} stale ({collapsedStale.length})</span>
            </button>
            {showStale && (
              <motion.div
                layout
                className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2 mt-3 opacity-70"
                transition={SPRING}
              >
                <AnimatePresence initial={false}>
                  {collapsedStale.map((project, i) => (
                    <motion.div
                      key={project.id}
                      layout
                      initial={{ opacity: 0, y: 8 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: 8 }}
                      transition={{ ...SPRING, delay: Math.min(i * 0.03, 0.3), duration: 0.2 }}
                    >
                      <CollapsedTile project={project} onExpand={() => expand(project.slug)} />
                    </motion.div>
                  ))}
                </AnimatePresence>
              </motion.div>
            )}
          </div>
        )}
      </LayoutGroup>
    </div>
  )
}
