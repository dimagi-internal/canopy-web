import { useEffect, useMemo, useState } from 'react'
import { usePageState } from '@/widget/usePageState'
import { describeSelection } from '@/widget/pageState'
import { usePageAction } from '@/widget/usePageAction'
import { dismissInsightsAction } from './insightsDismissAction'
import { Link, useSearchParams } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import {
  type Insight,
  insightsApi,
  parseInsightBody,
  parseInsightCategory,
} from '@/api/insights'
import { CATEGORY_STYLES, CategoryBadge } from '@/components/InsightChip'
import { Markdown } from '@/components/Markdown'

const CATEGORIES = [
  { key: 'all', label: 'All' },
  { key: 'ship_gap', label: 'Ship Gaps' },
  { key: 'hygiene', label: 'Hygiene' },
  { key: 'pattern', label: 'Patterns' },
  { key: 'stale', label: 'Stale' },
  { key: 'opportunity', label: 'Opportunities' },
  { key: 'alignment', label: 'Alignment' },
]

function InsightCard({ insight, onDismiss }: { insight: Insight; onDismiss: (id: number) => void }) {
  const category = parseInsightCategory(insight.content)
  const body = parseInsightBody(insight.content)
  const style = category ? CATEGORY_STYLES[category] : null

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, x: -40, transition: { duration: 0.2 } }}
      transition={{ type: 'spring', stiffness: 400, damping: 35 }}
      className={`bg-card border rounded-lg p-4 ${style ? style.border : 'border-border'}`}
    >
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <Link
            to={`/?expand=${encodeURIComponent(insight.project_slug)}`}
            className="text-xs font-medium text-foreground-secondary hover:text-primary transition-colors shrink-0"
            title={`Open ${insight.project_name} on the dashboard`}
          >
            {insight.project_name}
          </Link>
          <CategoryBadge category={category} />
        </div>
        <button
          onClick={() => onDismiss(insight.id)}
          className="text-foreground-subtle hover:text-foreground-secondary text-sm shrink-0 leading-none"
          aria-label="Dismiss"
        >
          ✕
        </button>
      </div>
      <Markdown className="text-sm text-foreground-secondary leading-relaxed mb-2">{body}</Markdown>
      <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
        <span>{insight.source}</span>
        <span>·</span>
        <span>{new Date(insight.created_at).toLocaleDateString()}</span>
      </div>
    </motion.div>
  )
}

function SkeletonCard({ delay }: { delay: number }) {
  return (
    <div
      className="bg-card border border-border rounded-lg p-4 animate-pulse"
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="flex items-center gap-3 mb-3">
        <div className="h-3 bg-muted rounded w-20" />
        <div className="h-4 bg-muted rounded w-16" />
      </div>
      <div className="space-y-2">
        <div className="h-3 bg-muted/70 rounded w-full" />
        <div className="h-3 bg-muted/70 rounded w-4/5" />
      </div>
      <div className="flex items-center gap-2 mt-3">
        <div className="h-2 bg-muted/50 rounded w-24" />
        <div className="h-2 bg-muted/50 rounded w-16" />
      </div>
    </div>
  )
}

export function InsightsPage() {
  const [insights, setInsights] = useState<Insight[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeFilter, setActiveFilter] = useState('all')
  const [clearing, setClearing] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const projectFilter = searchParams.get('project') || ''

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void (async () => {
      try {
        const data = await insightsApi.list({
          category: activeFilter === 'all' ? undefined : activeFilter,
          project: projectFilter || undefined,
        })
        if (!cancelled) setInsights(data)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load insights')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [activeFilter, projectFilter])

  async function handleDismiss(id: number) {
    try {
      await insightsApi.dismiss(id)
      setInsights((prev) => prev.filter((i) => i.id !== id))
    } catch {
      // silent
    }
  }

  // --- what the embedded agent can see and do here -------------------------
  //
  // This page is the reason the widget exists for Jonathan: it accumulates
  // stale entries he legitimately wants closed, and saying "close these" is
  // cheap in front of the list and expensive anywhere else.
  //
  // The agent could already bulk-clear by FILTER (clear_insights is
  // MCP-exposed), but not dismiss a chosen set, and it cannot see which
  // insights are on screen — the active filter, the project narrowing, what
  // has already been triaged. That selection is page-local, which is what
  // makes this a bridge case rather than a server one.

  // Selection, not data. This used to serialise six fields for every visible
  // row — which duplicated `list_insights`, could go stale between render and
  // send, and was a second place the workspace ACL could be got wrong. Now the
  // page says WHICH rows are on screen and WHICH tool resolves them, and the
  // agent reads the rows itself, live, under the user's own permissions.
  //
  // It is also pushed on every change rather than read once when a conversation
  // opens, so filtering the page mid-chat updates what "these" means instead of
  // leaving the agent acting on a screen that has moved.
  usePageState(
    () =>
      describeSelection({
        backingTool: 'list_insights',
        ids: insights.map((i) => i.id),
        filters: {
          category: activeFilter === 'all' ? null : activeFilter,
          project: projectFilter || null,
        },
      }),
    [insights, activeFilter, projectFilter],
  )

  usePageAction(
    'dismissInsights',
    async (args) =>
      dismissInsightsAction((args.ids as number[] | undefined) ?? [], {
        visible: new Set(insights.map((i) => i.id)),
        dismiss: (id) => insightsApi.dismiss(id),
        onDismissed: (ids) =>
          setInsights((prev) => prev.filter((i) => !ids.includes(i.id))),
      }),
    {
      description:
        'Dismiss specific insights from the list the user is currently viewing. ' +
        'Only ids present in the page context can be dismissed.',
      parameters: {
        type: 'object',
        properties: {
          ids: {
            type: 'array',
            items: { type: 'integer' },
            description: 'Insight ids, taken from the page context',
          },
        },
        required: ['ids'],
      },
    },
  )

  function clearProjectFilter() {
    const next = new URLSearchParams(searchParams)
    next.delete('project')
    setSearchParams(next, { replace: true })
  }

  // Display name for the active project filter (best-effort: use the first
  // matching insight's project_name; fall back to the slug). Avoids a separate
  // API call just to render a chip label.
  const projectFilterLabel = useMemo(() => {
    if (!projectFilter) return ''
    const hit = insights.find((i) => i.project_slug === projectFilter)
    return hit?.project_name || projectFilter
  }, [projectFilter, insights])

  // Clear exactly what the active filter currently shows: category (unless
  // 'all') and project (if set). Refetches afterward and surfaces the count.
  async function handleClear() {
    const catLabel =
      activeFilter === 'all'
        ? ''
        : (CATEGORIES.find((c) => c.key === activeFilter)?.label ?? activeFilter) + ' '
    const scope = projectFilter ? ` for ${projectFilterLabel}` : ''
    const confirmed = window.confirm(
      `Clear all ${insights.length} ${catLabel}insight${insights.length === 1 ? '' : 's'}${scope}? This cannot be undone.`,
    )
    if (!confirmed) return
    setClearing(true)
    setNotice(null)
    try {
      const { cleared } = await insightsApi.clear({
        category: activeFilter === 'all' ? undefined : activeFilter,
        project: projectFilter || undefined,
      })
      const data = await insightsApi.list({
        category: activeFilter === 'all' ? undefined : activeFilter,
        project: projectFilter || undefined,
      })
      setInsights(data)
      setNotice(`Cleared ${cleared} insight${cleared === 1 ? '' : 's'}.`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear insights')
    } finally {
      setClearing(false)
    }
  }

  const emptyMessage = (() => {
    if (projectFilter && activeFilter !== 'all') {
      const catLabel = CATEGORIES.find((c) => c.key === activeFilter)?.label.toLowerCase() ?? activeFilter
      return `No ${catLabel} insights for ${projectFilterLabel}.`
    }
    if (projectFilter) return `No insights for ${projectFilterLabel} yet.`
    if (activeFilter !== 'all') {
      const catLabel = CATEGORIES.find((c) => c.key === activeFilter)?.label.toLowerCase() ?? activeFilter
      return `No ${catLabel} insights yet.`
    }
    return 'No insights yet.'
  })()

  return (
    <div className="max-w-3xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-lg font-semibold text-foreground">Insights</h1>
        <div className="flex items-center gap-2">
          {!loading && !error && insights.length > 0 && (
            <button
              onClick={handleClear}
              disabled={clearing}
              className="text-xs font-medium text-foreground-secondary hover:text-destructive border border-border hover:border-destructive/40 bg-card px-2.5 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title="Clear the insights currently in view"
            >
              {clearing ? 'Clearing…' : 'Clear'}
            </button>
          )}
          <span className="text-xs text-muted-foreground bg-card px-2.5 py-1 rounded">
            {loading ? 'loading...' : `${insights.length} insights`}
          </span>
        </div>
      </div>

      {/* Clear confirmation notice */}
      {notice && (
        <div className="mb-3 text-xs text-foreground-secondary bg-card border border-border px-3 py-2 rounded">
          {notice}
        </div>
      )}

      {/* Active project filter chip */}
      {projectFilter && (
        <div className="mb-3 flex items-center gap-2">
          <span className="inline-flex items-center gap-1.5 text-[11px] text-foreground-secondary bg-card border border-primary/30 px-2.5 py-1 rounded">
            <span className="text-muted-foreground">Project:</span>
            <span className="font-medium">{projectFilterLabel}</span>
            <button
              onClick={clearProjectFilter}
              className="text-muted-foreground hover:text-foreground-secondary leading-none ml-0.5"
              aria-label="Clear project filter"
              title="Clear project filter"
            >
              ✕
            </button>
          </span>
        </div>
      )}

      {/* Category filter tabs */}
      <div className="flex gap-1 mb-6 bg-card rounded-lg p-1 overflow-x-auto">
        {CATEGORIES.map((cat) => (
          <button
            key={cat.key}
            onClick={() => setActiveFilter(cat.key)}
            className={`text-xs font-medium px-3 py-1.5 rounded transition-colors whitespace-nowrap ${
              activeFilter === cat.key
                ? 'text-foreground bg-muted'
                : 'text-muted-foreground hover:text-foreground-secondary hover:bg-muted/50'
            }`}
          >
            {cat.label}
          </button>
        ))}
      </div>

      {/* Error state */}
      {error && (
        <div className="flex items-center justify-center h-48 text-destructive text-sm">{error}</div>
      )}

      {/* Loading skeleton */}
      {loading && (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <SkeletonCard key={i} delay={i * 80} />
          ))}
        </div>
      )}

      {/* Insight feed */}
      {!loading && !error && (
        <div className="space-y-3">
          <AnimatePresence initial={false}>
            {insights.map((insight) => (
              <InsightCard key={insight.id} insight={insight} onDismiss={handleDismiss} />
            ))}
          </AnimatePresence>

          {/* Empty state */}
          {insights.length === 0 && (
            <div className="flex flex-col items-center justify-center h-48 text-center">
              <p className="text-sm text-muted-foreground mb-1">{emptyMessage}</p>
              <p className="text-xs text-foreground-subtle">
                Run <code className="text-primary/70 bg-card px-1.5 py-0.5 rounded">canopy:portfolio-review</code> to generate insights.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
