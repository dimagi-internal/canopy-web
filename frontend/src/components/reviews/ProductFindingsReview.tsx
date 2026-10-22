/**
 * ProductFindingsReview — the first-class review surface for
 * `gate === 'product_findings'`.
 *
 * A run-child review (NOT a narrative version). It embeds the iteration clip at
 * the top, then renders one CARD per finding cluster: severity / fix_kind /
 * route / scene chips, the suggested fix, inline evidence thumbnails, a
 * "Watch @ m:ss" button that seeks the embedded clip to the evidence's
 * video_t, a deck deep-link, and a per-finding implement / skip control with an
 * always-visible comment box.
 *
 * Nothing is pre-selected. The footer is a single "Save Edits" button (partial
 * saves allowed); on save it produces { decisions: { <id>: { decision, comment } } }
 * and then instructs the reviewer to have their AI agent retrieve + apply it.
 */
import { memo, useCallback, useMemo, useRef, useState } from 'react'
import { CopyButton } from 'canopy-ui/ui'
import type {
  FindingsCluster,
  FindingsDecision,
  FindingsEvidence,
  FindingsResolution,
  FindingsSeverity,
  ProductFindingsRequestJson,
  ProductFindingsResponseJson,
} from '../../api/reviews'
import { withBase } from '../../lib/basePath'
import { Markdown } from '../Markdown'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Seconds → "m:ss". */
function fmtTime(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds))
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}:${String(sec).padStart(2, '0')}`
}

const SEVERITY_STYLES: Record<FindingsSeverity, string> = {
  high: 'bg-destructive/15 text-destructive border-destructive/30',
  medium: 'bg-warning/15 text-warning border-warning/30',
  low: 'bg-muted-foreground/15 text-foreground-secondary border-input/40',
}

const DECISION_META: Record<
  FindingsDecision,
  { label: string; hint: string; accent: string }
> = {
  implement: {
    label: 'Implement',
    hint: 'Apply this fix in the next iteration.',
    accent: 'emerald',
  },
  skip: {
    label: 'Skip',
    hint: "Don't act on this finding.",
    accent: 'stone',
  },
}

const DECISION_SELECTED: Record<string, string> = {
  emerald: 'border-success/60 bg-success/15 text-success ring-1 ring-success/30',
  sky: 'border-info/60 bg-info/15 text-info ring-1 ring-info/30',
  stone: 'border-muted-foreground bg-input text-foreground ring-1 ring-muted-foreground/30',
}

// ---------------------------------------------------------------------------
// Chip
// ---------------------------------------------------------------------------

function Chip({
  children,
  className,
  title,
}: {
  children: React.ReactNode
  className?: string
  title?: string
}) {
  return (
    <span
      title={title}
      className={[
        'inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium select-none border',
        className ?? 'bg-muted/60 text-foreground-secondary border-input',
      ].join(' ')}
    >
      {children}
    </span>
  )
}

// ---------------------------------------------------------------------------
// 3-way decision control
// ---------------------------------------------------------------------------

function DecisionControl({
  clusterId,
  chosen,
  readOnly,
  onChange,
}: {
  clusterId: string
  chosen: FindingsDecision | ''
  readOnly: boolean
  onChange: (value: FindingsDecision) => void
}) {
  const options: FindingsDecision[] = ['implement', 'skip']
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className="sr-only">Decision for {clusterId}</legend>
      <div className="flex flex-wrap gap-2">
        {options.map((opt) => {
          const meta = DECISION_META[opt]
          const isSelected = chosen === opt
          return (
            <label
              key={opt}
              title={meta.hint}
              className={[
                'flex items-center gap-2 cursor-pointer rounded border px-3 py-1.5 text-sm transition-colors',
                isSelected
                  ? DECISION_SELECTED[meta.accent] ?? DECISION_SELECTED.stone
                  : 'border-input text-foreground-secondary hover:border-muted-foreground hover:text-foreground-secondary',
                readOnly ? 'pointer-events-none opacity-80' : '',
              ].join(' ')}
            >
              <input
                type="radio"
                name={`finding-${clusterId}`}
                value={opt}
                checked={isSelected}
                onChange={() => !readOnly && onChange(opt)}
                disabled={readOnly}
                className="sr-only"
              />
              {meta.label}
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}

// ---------------------------------------------------------------------------
// Evidence row — thumbnail + watch + deck deep-link
// ---------------------------------------------------------------------------

function EvidenceCard({
  evidence,
  deckUrl,
  onWatch,
}: {
  evidence: FindingsEvidence
  deckUrl?: string
  onWatch: (videoT: number) => void
}) {
  const deckHref = deckUrl ? `${deckUrl}${evidence.deck_anchor ?? ''}` : null
  return (
    <figure className="flex flex-col gap-1.5 rounded-lg border border-border bg-card/60 p-2">
      {evidence.thumb ? (
        <img
          src={evidence.thumb}
          alt={`Scene ${evidence.scene} evidence`}
          className="h-40 w-full rounded border border-border bg-black object-cover object-top"
          loading="lazy"
        />
      ) : (
        <div className="flex h-24 items-center justify-center rounded border border-border text-[11px] text-muted-foreground">
          No thumbnail
        </div>
      )}
      <figcaption className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
          Scene {evidence.scene}
        </span>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => onWatch(evidence.video_t)}
            className="rounded border border-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:border-muted-foreground hover:text-foreground transition-colors"
            title={`Seek the clip to ${fmtTime(evidence.video_t)}`}
          >
            ▶ Watch @ {fmtTime(evidence.video_t)}
          </button>
          {deckHref && (
            <a
              href={deckHref}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded border border-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:border-muted-foreground hover:text-foreground-secondary transition-colors"
              title="Open this scene in the deck"
            >
              Deck ↗
            </a>
          )}
        </div>
      </figcaption>
    </figure>
  )
}

// ---------------------------------------------------------------------------
// Cluster card
// ---------------------------------------------------------------------------

// Memoized so a decision/comment on ONE finding re-renders only that card —
// NOT all 24 image-heavy cards + the embedded clip. Re-rendering the whole tree
// on every click caused a Chromium paint catastrophe (page goes black until scroll).
// onChoose/onComment are id-aware so the parent can pass stable (memo-safe) callbacks.
const ClusterCard = memo(function ClusterCard({
  cluster,
  index,
  chosen,
  comment,
  readOnly,
  deckUrl,
  onChoose,
  onComment,
  onWatch,
}: {
  cluster: FindingsCluster
  index: number
  chosen: FindingsDecision | ''
  comment: string
  readOnly: boolean
  deckUrl?: string
  onChoose: (clusterId: string, value: FindingsDecision) => void
  onComment: (clusterId: string, value: string) => void
  onWatch: (videoT: number) => void
}) {
  const evidence = cluster.evidence ?? []
  const scenes = cluster.scenes ?? []
  return (
    // content-visibility:auto + intrinsic-size isolate each card's layout/paint/style:
    // a decision/comment change can no longer invalidate the whole page (the bug where
    // clicking Skip blanked the left rail + half the page until a scroll forced a repaint),
    // and offscreen cards (24 image-heavy ones) skip painting entirely.
    <div
      className="rounded-lg border border-input bg-background p-4 space-y-3"
      style={{ contentVisibility: 'auto', containIntrinsicSize: 'auto 420px' }}
    >
      {/* Chips */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-semibold text-muted-foreground tabular-nums">
          #{index + 1}
        </span>
        {cluster.severity && (
          <Chip className={SEVERITY_STYLES[cluster.severity]} title="Severity">
            {cluster.severity}
          </Chip>
        )}
        {cluster.fix_kind && (
          <Chip title="Fix kind">{cluster.fix_kind}</Chip>
        )}
        {cluster.route && (
          <Chip className="bg-special/15 text-special border-special/30" title="Route">
            {cluster.route}
          </Chip>
        )}
        {scenes.length > 0 && (
          <Chip title="Scenes referenced">
            {scenes.length === 1 ? `scene ${scenes[0]}` : `scenes ${scenes.join(', ')}`}
          </Chip>
        )}
        {typeof cluster.count === 'number' && cluster.count > 1 && (
          <Chip title="Occurrences">×{cluster.count}</Chip>
        )}
      </div>

      {/* Title */}
      <h3 className="text-sm font-medium text-foreground leading-snug">{cluster.title}</h3>

      {/* Suggested fix */}
      {cluster.suggested_fix && (
        <div>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-0.5">
            Suggested fix
          </p>
          <Markdown className="text-sm text-foreground-secondary leading-relaxed">
            {cluster.suggested_fix}
          </Markdown>
        </div>
      )}

      {/* Evidence */}
      {evidence.length > 0 && (
        <div>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">Evidence</p>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {evidence.map((ev, i) => (
              <EvidenceCard
                key={`${ev.scene}-${i}`}
                evidence={ev}
                deckUrl={deckUrl}
                onWatch={onWatch}
              />
            ))}
          </div>
        </div>
      )}

      {/* Decision + per-finding comment */}
      <div className="pt-1 space-y-2">
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {readOnly ? 'Decision' : 'What should we do?'}
        </p>
        <DecisionControl
          clusterId={cluster.id}
          chosen={chosen}
          readOnly={readOnly}
          onChange={(v) => onChoose(cluster.id, v)}
        />
        {readOnly ? (
          comment ? (
            <p className="text-sm text-foreground-secondary rounded border border-border bg-card/60 px-3 py-2 whitespace-pre-wrap">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground block mb-0.5">
                Your comment
              </span>
              {comment}
            </p>
          ) : null
        ) : (
          <textarea
            value={comment}
            onChange={(e) => onComment(cluster.id, e.target.value)}
            rows={2}
            placeholder="Comment on this finding (optional) — what to change, or why skip…"
            aria-label={`Comment on finding ${index + 1}`}
            className="w-full rounded border border-border bg-card px-3 py-2 text-sm text-foreground-secondary placeholder:text-muted-foreground focus:border-muted-foreground focus:outline-none transition-colors"
          />
        )}
      </div>
    </div>
  )
})

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export interface ProductFindingsReviewProps {
  review: ProductFindingsRequestJson
  /** Submit handler — receives the assembled response_json. */
  onSubmit: (response: ProductFindingsResponseJson) => Promise<void> | void
  /** Read-only once resolved (or for anonymous viewers). */
  readOnly?: boolean
  /** Pre-filled response when the review is already resolved. */
  resolved?: ProductFindingsResponseJson | null
  /** Stamp shown in the resolved banner. */
  resolvedAt?: string | null
}

export function ProductFindingsReview({
  review,
  onSubmit,
  readOnly = false,
  resolved = null,
  resolvedAt = null,
}: ProductFindingsReviewProps) {
  const clusters = useMemo(() => review.clusters ?? [], [review.clusters])
  const videoRef = useRef<HTMLVideoElement | null>(null)

  // Per-cluster decision — NOTHING is pre-selected. Resolved reviews show the
  // submitted picks.
  const [decisions, setDecisions] = useState<Record<string, FindingsDecision | ''>>(() => {
    const initial: Record<string, FindingsDecision | ''> = {}
    for (const c of clusters) initial[c.id] = resolved?.decisions?.[c.id]?.decision ?? ''
    return initial
  })

  // Per-cluster comment.
  const [comments, setComments] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const c of clusters) initial[c.id] = resolved?.decisions?.[c.id]?.comment ?? ''
    return initial
  })

  const [busy, setBusy] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Seek the embedded clip; fall back to opening the clip at #t when no element.
  const handleWatch = useCallback(
    (videoT: number) => {
      const el = videoRef.current
      if (el) {
        try {
          el.currentTime = videoT
          el.scrollIntoView({ behavior: 'smooth', block: 'start' })
          void el.play().catch(() => {})
          return
        } catch {
          // fall through to the deep-link fallback
        }
      }
      if (review.video?.url) {
        window.open(`${review.video.url}#t=${videoT}`, '_blank', 'noopener,noreferrer')
      }
    },
    [review.video],
  )

  // Stable, id-aware handlers so memoized cards don't re-render on every parent
  // update (the inline-arrow versions allocated a fresh fn per card per render,
  // defeating React.memo and re-rendering all 24 cards on each click).
  const handleChoose = useCallback(
    (id: string, value: FindingsDecision) => setDecisions((prev) => ({ ...prev, [id]: value })),
    [],
  )
  const handleComment = useCallback(
    (id: string, value: string) => setComments((prev) => ({ ...prev, [id]: value })),
    [],
  )

  // Partial saves allowed — enable once the reviewer has touched anything
  // (a decision or a comment on any finding).
  const touchedCount = useMemo(
    () => clusters.filter((c) => decisions[c.id] || (comments[c.id] ?? '').trim()).length,
    [clusters, decisions, comments],
  )
  const canSubmit = !busy && !readOnly && touchedCount > 0

  const handleSubmit = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      // Only findings the reviewer acted on (a decision or a comment) are sent.
      const out: Record<string, FindingsResolution> = {}
      for (const c of clusters) {
        const d = decisions[c.id]
        const cm = (comments[c.id] ?? '').trim()
        if (d || cm) out[c.id] = { decision: d || null, comment: cm }
      }
      await onSubmit({ decisions: out })
      setSubmitted(true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [clusters, decisions, comments, onSubmit])

  const summary = review.summary
  const decidedCount = clusters.filter((c) => !!decisions[c.id]).length

  // What the reviewer hands their AI agent after saving, to apply the picks.
  const applyPrompt =
    `Retrieve the resolved product_findings review for ${review.feature || review.run_id} ` +
    `(run ${review.run_id}) and implement my picks — apply the findings I marked "implement" ` +
    `(honoring my per-finding comments) and skip the rest.`

  return (
    <div className="max-w-4xl mx-auto py-8 px-4 space-y-6">
      {/* Header */}
      <header className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold text-foreground">
            Findings review
            {review.feature ? <span className="text-foreground-secondary"> · {review.feature}</span> : null}
            {review.iteration != null ? (
              <span className="text-muted-foreground"> · iteration {review.iteration}</span>
            ) : null}
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">{review.run_id}</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {summary?.verdict && (
            <Chip
              className={
                /fail/i.test(summary.verdict)
                  ? 'bg-destructive/15 text-destructive border-destructive/30'
                  : 'bg-success/15 text-success border-success/30'
              }
              title="Eval verdict"
            >
              {summary.verdict}
            </Chip>
          )}
          {summary?.concept_score != null && (
            <Chip
              className="bg-special/15 text-special border-special/30"
              title="Concept eval score"
            >
              concept {summary.concept_score}
            </Chip>
          )}
          {summary?.user_score != null && (
            <Chip
              className="bg-special/15 text-special border-special/30"
              title="User-artifact eval score"
            >
              user {summary.user_score}
            </Chip>
          )}
          <span
            className={[
              'shrink-0 rounded px-2 py-0.5 text-xs font-medium',
              readOnly
                ? 'bg-success/20 text-success border border-success/30'
                : 'bg-warning/20 text-warning border border-warning/30',
            ].join(' ')}
          >
            {readOnly ? 'Resolved' : 'Needs your input'}
          </span>
        </div>
      </header>

      {/* Embedded iteration clip */}
      {review.video?.url ? (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-2">
            Iteration clip
          </h2>
          <div className="rounded-lg border border-input bg-black overflow-hidden">
            <video
              ref={videoRef}
              src={withBase(review.video.url)}
              controls
              preload="metadata"
              className="w-full max-h-[60vh] bg-black"
            />
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            Each finding's “Watch @ m:ss” seeks this clip to that scene.
          </p>
        </section>
      ) : null}

      {/* Clusters */}
      <section className="space-y-4">
        <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider">
          Findings ({clusters.length})
        </h2>
        {clusters.length === 0 ? (
          <p className="text-sm text-muted-foreground rounded border border-border bg-card/40 px-3 py-2">
            No findings in this review.
          </p>
        ) : (
          clusters.map((cluster, i) => (
            <ClusterCard
              key={cluster.id}
              cluster={cluster}
              index={i}
              chosen={decisions[cluster.id] ?? ''}
              comment={comments[cluster.id] ?? ''}
              readOnly={readOnly}
              deckUrl={review.deck_url}
              onChoose={handleChoose}
              onComment={handleComment}
              onWatch={handleWatch}
            />
          ))
        )}
      </section>

      {/* Error */}
      {error && (
        <p className="text-sm text-destructive rounded border border-destructive/30 bg-destructive/10 px-3 py-2">
          {error}
        </p>
      )}

      {/* Footer — Save Edits + apply instruction */}
      <section className="space-y-4 border-t border-border pt-6">
        {readOnly || submitted ? (
          <div className="rounded-lg border border-success/30 bg-success/10 px-4 py-3 space-y-2">
            <p className="text-sm text-success font-medium">
              ✓ Edits saved
              {resolvedAt ? ` · ${new Date(resolvedAt).toLocaleString()}` : ''}
            </p>
            <p className="text-sm text-foreground-secondary">
              To apply them, tell your AI agent to retrieve this review and implement what you said:
            </p>
            <div className="flex items-start gap-2">
              <code className="flex-1 rounded border border-input bg-card px-3 py-2 text-xs text-foreground-secondary whitespace-pre-wrap">
                {applyPrompt}
              </code>
              <CopyButton
                text={applyPrompt}
                variant="outline"
                className="shrink-0 border-input px-2 py-1 text-xs text-foreground-secondary hover:border-muted-foreground hover:bg-transparent hover:text-foreground transition-colors"
              />
            </div>
            <p className="text-[11px] text-muted-foreground">
              It reads your implement / skip picks and per-finding comments from this review.
            </p>
          </div>
        ) : (
          <div className="flex flex-col items-end gap-1.5">
            <p className="text-[11px] text-muted-foreground text-right">
              {decidedCount} of {clusters.length} decided · partial saves are fine.
            </p>
            <button
              type="button"
              onClick={() => void handleSubmit()}
              disabled={!canSubmit}
              className={[
                'px-6 py-2 rounded font-medium text-sm transition-colors',
                !canSubmit
                  ? 'bg-input text-foreground-secondary cursor-not-allowed'
                  : 'bg-primary hover:bg-primary/90 text-white',
              ].join(' ')}
            >
              {busy ? 'Saving…' : 'Save Edits'}
            </button>
          </div>
        )}
      </section>
    </div>
  )
}
