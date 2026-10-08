import { forwardRef, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type TextareaHTMLAttributes } from 'react'
import { useParams } from 'react-router-dom'
import { DddShell } from '@/components/ddd/DddShell'
import { useAuth } from '@/auth/AuthProvider'
import {
  getReview,
  submitReview,
  suggestReview,
  submitFindingsReview,
  type ReviewDetail,
  type ReviewSuggestion,
  type ReviewDecision,
  type ReviewSceneActionability,
  type ReviewSubmitPayload,
  type ReviewSubmittedScene,
  type ReviewPersona,
  type ReviewWhySpineItem,
  type ReviewWhyGap,
  type ProductFindingsRequestJson,
  type ProductFindingsResponseJson,
} from '../api/reviews'
import { getWalkthrough, walkthroughContentUrl } from '../api/walkthroughs'
import { withBase } from '../lib/basePath'
import { UPLOADED_CONTENT_SANDBOX } from '../lib/uploadedContentSandbox'
import {
  ReviewEditorProvider,
  useReviewEditor,
} from '../components/reviews/ReviewEditorContext'
import { ProductFindingsReview } from '../components/reviews/ProductFindingsReview'
import { CutVideoList, groupScenesByCut } from '../components/ddd/CutVideoList'

// The cut grouping moved beside the cuts view it now also serves; re-exported
// for the callers and tests that import it from here.
export { groupScenesByCut }

// ---------------------------------------------------------------------------
// AutoTextarea — a textarea that grows to fit its content (never internally
// scrolls). Re-fits on value changes and window resize; honors any min-height
// supplied via className.
// ---------------------------------------------------------------------------

const AutoTextarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function AutoTextarea(props, fwdRef) {
    const innerRef = useRef<HTMLTextAreaElement | null>(null)
    const setRef = (el: HTMLTextAreaElement | null) => {
      innerRef.current = el
      if (typeof fwdRef === 'function') fwdRef(el)
      else if (fwdRef) (fwdRef as React.MutableRefObject<HTMLTextAreaElement | null>).current = el
    }
    const resize = useCallback(() => {
      const el = innerRef.current
      if (!el) return
      el.style.height = 'auto'
      el.style.height = `${el.scrollHeight}px`
    }, [])
    useLayoutEffect(() => {
      resize()
    }, [resize, props.value])
    useEffect(() => {
      window.addEventListener('resize', resize)
      return () => window.removeEventListener('resize', resize)
    }, [resize])
    const userOnInput = props.onInput
    return (
      <textarea
        {...props}
        ref={setRef}
        onInput={(e) => {
          resize()
          userOnInput?.(e)
        }}
      />
    )
  },
)

// ---------------------------------------------------------------------------
// Small utility components
// ---------------------------------------------------------------------------

function ScoreBadge({
  score,
  total = 5,
  tooltip,
}: {
  score: number
  total?: number
  tooltip?: string
}) {
  const formatted = Number.isInteger(score) ? String(score) : score.toFixed(1)
  return (
    <span
      title={tooltip}
      className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-semibold bg-special/20 text-special border border-special/30 cursor-default select-none"
    >
      {formatted}
      <span className="text-special font-normal">/ {total}</span>
    </span>
  )
}

function DirtyBadge({ isDirty }: { isDirty: boolean }) {
  if (!isDirty) return null
  return (
    <span className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-semibold bg-warning/20 text-warning border border-warning/30">
      Edited
    </span>
  )
}

function PersonaChip({ persona, dim = false }: { persona: ReviewPersona; dim?: boolean }) {
  return (
    <span
      title={`${persona.name} — ${persona.role}`}
      className={[
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium select-none',
        dim ? 'border-input text-foreground-secondary' : 'border-input text-foreground-secondary',
      ].join(' ')}
    >
      <span
        className="inline-block h-2 w-2 rounded-full shrink-0"
        style={{ backgroundColor: persona.color || '#78716c' }}
      />
      {persona.name}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Decision tiles
// ---------------------------------------------------------------------------

interface NarrativeVerdictProps {
  decision: ReviewDecision
  chosen: string
  onChange: (value: string) => void
  readOnly: boolean
}

function NarrativeVerdictControl({
  decision,
  chosen,
  onChange,
  readOnly,
}: NarrativeVerdictProps) {
  const LABELS: Record<string, { label: string; explanation: string; accent: string }> = {
    approve: {
      label: 'Approve & continue',
      explanation:
        'Lock this narrative as the build plan (including any edits I made); proceed to build the features.',
      accent: 'emerald',
    },
    redraft: {
      label: 'Send back to re-draft',
      explanation:
        'The framing/approach is wrong — re-author the narrative from scratch.',
      accent: 'amber',
    },
  }

  return (
    <div className="rounded-lg border border-input bg-card p-4 space-y-3">
      <p className="text-sm text-foreground-secondary leading-snug">{decision.prompt}</p>
      <div className="flex flex-col sm:flex-row gap-3">
        {decision.options.map((opt) => {
          const meta = LABELS[opt] ?? { label: opt, explanation: '', accent: 'stone' }
          const isSelected = chosen === opt
          const accent = meta.accent

          const selectedStyles: Record<string, string> = {
            emerald:
              'border-success/60 bg-success/15 text-success ring-1 ring-success/30',
            amber:
              'border-warning/60 bg-warning/15 text-warning ring-1 ring-warning/30',
            stone:
              'border-muted-foreground bg-input text-foreground ring-1 ring-muted-foreground/30',
          }
          const unselectedStyles =
            'border-input text-foreground-secondary hover:border-muted-foreground hover:text-foreground-secondary hover:bg-muted/50'

          return (
            <button
              key={opt}
              type="button"
              disabled={readOnly}
              onClick={() => !readOnly && onChange(opt)}
              className={[
                'flex-1 text-left rounded-lg border px-4 py-3 transition-all',
                isSelected
                  ? (selectedStyles[accent] ?? selectedStyles.stone)
                  : unselectedStyles,
                readOnly ? 'pointer-events-none opacity-80' : 'cursor-pointer',
              ].join(' ')}
            >
              <p className="font-semibold text-sm leading-tight">{meta.label}</p>
              {meta.explanation && (
                <p
                  className={[
                    'mt-1 text-xs leading-snug',
                    isSelected ? 'opacity-80' : 'text-muted-foreground',
                  ].join(' ')}
                >
                  {meta.explanation}
                </p>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

interface DecisionGroupProps {
  decision: ReviewDecision
  chosen: string
  onChange: (value: string) => void
  readOnly: boolean
}

function DecisionGroup({ decision, chosen, onChange, readOnly }: DecisionGroupProps) {
  const classLabel = decision.class ? decision.class.replace(/_/g, ' ') : ''

  return (
    <div className="rounded-lg border border-input bg-card p-4">
      <div className="flex items-start gap-2 mb-3">
        {classLabel && (
          <span className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider bg-primary/20 text-primary border border-primary/30">
            {classLabel}
          </span>
        )}
        <p className="text-sm text-foreground-secondary leading-snug">{decision.prompt}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {decision.options.map((opt) => {
          const isSelected = chosen === opt
          const isRecommended = decision.recommended === opt
          return (
            <label
              key={opt}
              className={[
                'flex items-center gap-2 cursor-pointer rounded px-3 py-1.5 text-sm border transition-colors',
                isSelected
                  ? 'bg-primary/20 border-primary/50 text-primary'
                  : 'border-input text-foreground-secondary hover:border-muted-foreground hover:text-foreground-secondary',
                readOnly ? 'pointer-events-none opacity-80' : '',
              ].join(' ')}
            >
              <input
                type="radio"
                name={`decision-${decision.id}`}
                value={opt}
                checked={isSelected}
                onChange={() => !readOnly && onChange(opt)}
                className="sr-only"
                disabled={readOnly}
              />
              {opt}
              {isRecommended && !isSelected && (
                <span className="text-[10px] text-muted-foreground">(recommended)</span>
              )}
              {isRecommended && isSelected && (
                <span className="text-[10px] text-primary/70">(recommended)</span>
              )}
            </label>
          )
        })}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Feature row — editable description + verify + optional feedback
// ---------------------------------------------------------------------------

interface FeatureRowProps {
  feature: {
    id: string
    description: string
    verify: string
    feedback: string
    deleted: boolean
  }
  readOnly: boolean
  isMissed: boolean
  onEdit: (field: 'description' | 'verify', value: string) => void
  onFeedback: (text: string) => void
  onDelete: () => void
}

function FeatureRow({
  feature,
  readOnly,
  isMissed,
  onEdit,
  onFeedback,
  onDelete,
}: FeatureRowProps) {
  if (feature.deleted) return null

  return (
    <li
      className={[
        'rounded border px-3 py-2 space-y-2',
        isMissed
          ? 'border-warning/30 bg-warning/5'
          : 'border-border bg-card/60',
      ].join(' ')}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Feature
        </p>
        {!readOnly && (
          <button
            type="button"
            onClick={onDelete}
            title="Delete feature"
            className="text-muted-foreground hover:text-destructive transition-colors text-xs"
          >
            ✕
          </button>
        )}
      </div>

      {/* description */}
      <div>
        <FieldLabel>What to build</FieldLabel>
        <AutoTextarea
          className={[
            'w-full rounded border bg-card px-2 py-1.5 text-sm text-foreground-secondary resize-none min-h-[2.5rem]',
            'border-input focus:border-muted-foreground focus:outline-none transition-colors',
            readOnly ? 'opacity-70 cursor-default' : '',
          ].join(' ')}
          value={feature.description}
          onChange={(e) => !readOnly && onEdit('description', e.target.value)}
          readOnly={readOnly}
          placeholder="The buildable unit — what an engineer implements"
          rows={2}
        />
      </div>

      {/* verify */}
      <div>
        <FieldLabel>Verify — how we'll confirm it's built</FieldLabel>
        <AutoTextarea
          className={[
            'w-full rounded border bg-card px-2 py-1.5 font-mono text-[11px] text-foreground-secondary resize-none min-h-[2.25rem] whitespace-pre-wrap break-words',
            'border-input focus:border-muted-foreground focus:outline-none transition-colors',
            readOnly ? 'opacity-70 cursor-default' : '',
          ].join(' ')}
          value={feature.verify}
          onChange={(e) => !readOnly && onEdit('verify', e.target.value)}
          readOnly={readOnly}
          rows={2}
          placeholder="A runnable check — API assertion, UI state, or test command"
        />
      </div>

      {isMissed && (
        <p className="text-[11px] text-warning/80">⚠ eval flagged as under-specified</p>
      )}

      {/* optional feedback */}
      {!readOnly && (
        <input
          type="text"
          className="w-full rounded border bg-card px-2 py-1.5 text-[11px] text-foreground-secondary border-input focus:border-muted-foreground focus:outline-none transition-colors"
          value={feature.feedback}
          onChange={(e) => onFeedback(e.target.value)}
          placeholder="Feedback on this feature (optional)"
        />
      )}
      {readOnly && feature.feedback && (
        <p className="text-[11px] text-muted-foreground italic">Feedback: {feature.feedback}</p>
      )}
    </li>
  )
}

// ---------------------------------------------------------------------------
// Scene card — narration + features + add-feature + delete-scene + per-scene feedback
// ---------------------------------------------------------------------------

interface SceneCardProps {
  scene: {
    id: string
    title: string
    persona: string
    narration: string
    deleted: boolean
    features: Array<{
      id: string
      description: string
      verify: string
      feedback: string
      deleted: boolean
    }>
    feedback: string
  }
  sceneNumber?: number
  persona?: ReviewPersona
  /** The spine item this scene grounds (resolved by provenance), if any. */
  grounding?: ReviewWhySpineItem
  /** Gaps whose claim_ref points at this scene's spine item. */
  gaps?: ReviewWhyGap[]
  readOnly: boolean
  sceneActionability?: ReviewSceneActionability
  onEditNarration: (text: string) => void
  onEditFeature: (featureId: string, field: 'description' | 'verify', value: string) => void
  onFeatureFeedback: (featureId: string, text: string) => void
  onDeleteFeature: (featureId: string) => void
  onAddFeature: () => void
  onDeleteScene: () => void
  onSceneFeedback: (text: string) => void
  /** Edit the grounding rationale (persists to the why-brief spine item). */
  onEditRationale?: (value: string) => void
  /** Edit a gap field (persists to the why-brief gap). */
  onEditGap?: (gapId: string, field: 'detail' | 'proposed_action', value: string) => void
  /** When true, the scene's details start expanded (e.g. ?expand=1 / print/judge view). */
  defaultOpen?: boolean
  /** When true, the scene has pending edits in the current session — drives the
   *  "Edited" badge + a sky-tinted border so the reviewer sees at a glance which
   *  beats they've touched. */
  isEdited?: boolean
  /** A suggester (share-link guest, or someone who can't decide) edits the WORDS
   *  and nothing else: no scores, build chips, features, grounding or delete —
   *  that scaffolding is for whoever approves the build plan (canopy-web#1267). */
  suggestOnly?: boolean
}

function StatusBadge({ status, frontier: frontierOverride }: { status?: string; frontier?: boolean }) {
  if (frontierOverride === undefined && !status) return null
  // Caller may pass an explicit `frontier` (e.g. grounded claim WITH an open gap → still
  // to-build); otherwise fall back to the spine status.
  const frontier = frontierOverride ?? status !== 'grounded'
  return (
    <span
      title={frontier ? 'New feature — not yet built; this scene shows intended behavior' : 'Existing feature — backed by shipped code/evidence (not re-verified live)'}
      className={[
        'shrink-0 rounded px-2 py-0.5 text-[11px] font-medium select-none',
        frontier
          ? 'bg-warning/15 text-warning border border-warning/30'
          : 'bg-success/15 text-success border border-success/30',
      ].join(' ')}
    >
      {frontier ? 'New feature' : 'Existing feature'}
    </span>
  )
}

function SceneCard({
  scene,
  sceneNumber,
  persona,
  grounding,
  gaps,
  readOnly,
  sceneActionability,
  onEditNarration,
  onEditFeature,
  onFeatureFeedback,
  onDeleteFeature,
  onAddFeature,
  onDeleteScene,
  onSceneFeedback,
  onEditRationale,
  onEditGap,
  defaultOpen,
  isEdited,
  suggestOnly = false,
}: SceneCardProps) {
  if (scene.deleted) return null

  const missedIds = new Set(sceneActionability?.missed ?? [])
  const activeFeatures = scene.features.filter((f) => !f.deleted)
  const hasGrounding = grounding != null || (gaps != null && gaps.length > 0)
  // Collapsed by default: header + narration stay visible (the skimmable arc);
  // features + grounding hide behind a toggle so the page isn't a wall.
  // defaultOpen (?expand=1) starts expanded so the full substance is captured for judging/print.
  const [open, setOpen] = useState(defaultOpen ?? false)
  // Top-level trust signal: did this scene's verify actually run green? (kept visible
  // in the collapsed view so the proof isn't buried behind the details toggle.)
  const verifiedGreen = (grounding?.evidence ?? []).some((e) =>
    /verify ran green/i.test(e.ref),
  )

  return (
    <div
      id={`scene-${scene.id}`}
      className={[
        'rounded-lg border p-4 space-y-3 transition-colors scroll-mt-4',
        isEdited
          ? 'border-info/60 bg-info/5 ring-1 ring-info/20'
          : 'border-input bg-background',
      ].join(' ')}
    >
      {/* Scene header */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 flex-wrap min-w-0">
          {sceneNumber != null && (
            <span className="text-[11px] font-semibold text-muted-foreground tabular-nums shrink-0">
              Scene {sceneNumber}
            </span>
          )}
          {persona && <PersonaChip persona={persona} />}
          <span className="text-sm font-medium text-foreground">{scene.title}</span>
          {!suggestOnly && (grounding?.status || (gaps && gaps.length > 0)) && (
            <StatusBadge
              frontier={(grounding?.status ?? 'grounded') !== 'grounded' || (gaps?.length ?? 0) > 0}
            />
          )}
          {isEdited && (
            <span
              title="This scene has pending edits in your current session"
              className="shrink-0 rounded px-2 py-0.5 text-[11px] font-medium bg-info/15 text-info border border-info/30 select-none"
            >
              Edited
            </span>
          )}
          {verifiedGreen && (
            <span
              title="This scene's verify actually ran and passed"
              className="shrink-0 rounded px-2 py-0.5 text-[11px] font-medium bg-success/15 text-success border border-success/30 select-none"
            >
              ✓ verify passed
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {!suggestOnly && sceneActionability != null && (
            <ScoreBadge
              score={sceneActionability.score}
              tooltip="Scene actionability score (AI buildability estimate)"
            />
          )}
          {!readOnly && !suggestOnly && (
            <button
              type="button"
              onClick={onDeleteScene}
              title="Delete scene"
              className="text-muted-foreground hover:text-destructive transition-colors text-xs px-2 py-0.5 rounded border border-input hover:border-destructive/40"
            >
              Delete scene
            </button>
          )}
        </div>
      </div>

      {/* Editable narration */}
      <div>
        <FieldLabel>What plays in the demo</FieldLabel>
        <AutoTextarea
          className={[
            'w-full rounded border bg-card px-3 py-2 text-sm text-foreground-secondary resize-none min-h-[4rem]',
            'border-input focus:border-muted-foreground focus:outline-none transition-colors',
            readOnly ? 'opacity-70 cursor-default' : '',
          ].join(' ')}
          value={scene.narration}
          onChange={(e) => !readOnly && onEditNarration(e.target.value)}
          readOnly={readOnly}
          rows={3}
          placeholder="The beat the viewer watches in this scene…"
        />
      </div>

      {/* Collapsed-by-default details. A one-line buildability summary stays visible
          so a reviewer can judge what they're approving without expanding all scenes.
          A suggester is not approving a build, so none of it renders for them. */}
      {!suggestOnly && (
        <>
      {!open && activeFeatures.length > 0 && (
        <p className="text-xs text-muted-foreground">
          <span className="text-muted-foreground">Builds: </span>
          {activeFeatures[0].description}
          {activeFeatures.length > 1 ? ` +${activeFeatures.length - 1} more` : ''}
        </p>
      )}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-xs text-muted-foreground hover:text-foreground-secondary transition-colors flex items-center gap-1.5"
      >
        <span className="text-muted-foreground">{open ? '▾' : '▸'}</span>
        {open
          ? 'Hide details'
          : `Show ${activeFeatures.length} feature${activeFeatures.length === 1 ? '' : 's'} + verify${hasGrounding ? ' + why' : ''}`}
      </button>

      {open && (
        <>
      {/* Features list */}
      {(activeFeatures.length > 0 || !readOnly) && (
        <div className="space-y-2">
          {activeFeatures.length > 0 && (
            <>
              <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Features to build
              </p>
              <ul className="space-y-2">
                {scene.features.map((f) => (
                  <FeatureRow
                    key={f.id}
                    feature={f}
                    readOnly={readOnly}
                    isMissed={missedIds.has(f.id)}
                    onEdit={(field, value) => onEditFeature(f.id, field, value)}
                    onFeedback={(text) => onFeatureFeedback(f.id, text)}
                    onDelete={() => onDeleteFeature(f.id)}
                  />
                ))}
              </ul>
            </>
          )}
          {!readOnly && (
            <button
              type="button"
              onClick={onAddFeature}
              className="mt-1 text-xs text-muted-foreground hover:text-foreground-secondary border border-dashed border-input hover:border-muted-foreground rounded px-3 py-1.5 transition-colors"
            >
              + Add feature
            </button>
          )}
        </div>
      )}

      {/* Grounding — why this scene matters, co-located with the scene it grounds.
          Muted + below the demo/features because the narration is what's most
          important to get right; this is the supporting "why" + build status. */}
      {hasGrounding && (
        <div className="rounded border border-border bg-card/40 p-3 space-y-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Why this matters {grounding?.id ? `· ${grounding.id}` : ''}
          </p>
          {grounding != null && (
            <div>
              <FieldLabel>Why it matters (rationale)</FieldLabel>
              <AutoTextarea
                className={inputCls(readOnly) + ' resize-none min-h-[3rem]'}
                value={grounding.rationale ?? ''}
                readOnly={readOnly || !onEditRationale}
                rows={2}
                placeholder="The reason this capability earns its place in the demo"
                onChange={(e) => !readOnly && onEditRationale?.(e.target.value)}
              />
            </div>
          )}
          {grounding?.evidence && grounding.evidence.length > 0 && (
            <div>
              <FieldLabel>
                {grounding.status === 'grounded' ? 'Backed by (shipped code/docs)' : 'Evidence'}
              </FieldLabel>
              <ul className="space-y-0.5">
                {grounding.evidence.map((ev, i) => (
                  <li key={i} className="text-[11px] text-foreground-secondary font-mono break-words">
                    <span className="text-muted-foreground">{ev.kind ?? 'ref'}:</span> {ev.ref}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(gaps ?? []).map((gap) => (
            <div key={gap.id} className="rounded border border-warning/20 bg-warning/5 p-2 space-y-2">
              <div className="flex items-center gap-2 text-[11px]">
                {gap.type && (
                  <span className="rounded bg-warning/15 text-warning px-1.5 py-0.5">{gap.type}</span>
                )}
                <span className="text-muted-foreground">what's missing for this scene</span>
              </div>
              <AutoTextarea
                className={inputCls(readOnly) + ' resize-none min-h-[2.5rem]'}
                value={gap.detail}
                readOnly={readOnly || !onEditGap}
                rows={2}
                placeholder="The gap"
                onChange={(e) => !readOnly && onEditGap?.(gap.id, 'detail', e.target.value)}
              />
              <div>
                <FieldLabel>Proposed action</FieldLabel>
                <AutoTextarea
                  className={inputCls(readOnly) + ' resize-none min-h-[2.5rem]'}
                  value={gap.proposed_action}
                  readOnly={readOnly || !onEditGap}
                  rows={2}
                  placeholder="What to do to close it"
                  onChange={(e) => !readOnly && onEditGap?.(gap.id, 'proposed_action', e.target.value)}
                />
              </div>
            </div>
          ))}
        </div>
      )}
        </>
      )}
        </>
      )}

      {/* Per-scene feedback */}
      {!readOnly && (
        <input
          type="text"
          className="w-full rounded border bg-card px-3 py-1.5 text-xs text-foreground-secondary border-input focus:border-muted-foreground focus:outline-none transition-colors"
          value={scene.feedback}
          onChange={(e) => onSceneFeedback(e.target.value)}
          placeholder="Scene-level feedback (optional)"
        />
      )}
      {readOnly && scene.feedback && (
        <p className="text-xs text-muted-foreground italic">Feedback: {scene.feedback}</p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Build Sequence Section — reorder the tackle sequence independent of video order
// ---------------------------------------------------------------------------

interface BuildSequenceSectionProps {
  effectiveScenes: Array<{ id: string; title: string; deleted: boolean }>
  /** Effective build order — ordered list of scene ids from the op-buffer projection. */
  buildOrder: string[]
  readOnly: boolean
  onReorder: (orderedIds: string[]) => void
  /** Submitted build order (readOnly mode). */
  resolvedBuildOrder: string[] | null
  /** scene id → frontier (true = New feature / to-build, false = Existing feature).
   *  Same `sceneIsFrontier` the scene cards use, so the build sequence labels
   *  already-built beats instead of presenting everything as undifferentiated to-do. */
  frontierById: Map<string, boolean>
}

function BuildSequenceSection({
  effectiveScenes,
  buildOrder,
  readOnly,
  onReorder,
  resolvedBuildOrder,
  frontierById,
}: BuildSequenceSectionProps) {
  // Build a lookup from scene id → title (only active scenes)
  const sceneById = new Map(
    effectiveScenes
      .filter((s) => !s.deleted)
      .map((s) => [s.id, s.title]),
  )

  // The order to display — in readOnly mode prefer the submitted order.
  const displayOrder = readOnly && resolvedBuildOrder ? resolvedBuildOrder : buildOrder

  // Filter to only scenes that still exist (guard against stale ids in stored data).
  const orderedScenes = displayOrder
    .filter((id) => sceneById.has(id))
    .map((id) => ({ id, title: sceneById.get(id) ?? id }))

  function move(index: number, direction: 'up' | 'down') {
    const next = orderedScenes.map((s) => s.id)
    const target = direction === 'up' ? index - 1 : index + 1
    if (target < 0 || target >= next.length) return
    // Swap
    ;[next[index], next[target]] = [next[target], next[index]]
    onReorder(next)
  }

  if (orderedScenes.length === 0) return null

  return (
    <section>
      <div className="mb-3">
        <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider">
          Build sequence
        </h2>
        {!readOnly && (
          <p className="text-xs text-muted-foreground mt-0.5">
            Order you'll tackle these when building — independent of the video order. Drag or use
            the arrows to rearrange.
          </p>
        )}
      </div>

      <ol className="space-y-2">
        {orderedScenes.map((scene, idx) => {
          // Also look up this scene's video (narrative) position for context.
          const narrativePos = effectiveScenes.filter((s) => !s.deleted).findIndex((s) => s.id === scene.id)
          return (
            <li
              key={scene.id}
              className="flex items-center gap-3 rounded-lg border border-input bg-card px-3 py-2"
            >
              {/* Build # badge */}
              <span className="shrink-0 w-6 h-6 rounded-full bg-primary/20 text-primary border border-primary/30 flex items-center justify-center text-xs font-semibold select-none">
                {idx + 1}
              </span>

              {/* Title + narrative position hint */}
              <div className="flex-1 min-w-0">
                <p className="text-sm text-foreground-secondary truncate">{scene.title}</p>
                {narrativePos !== idx && (
                  <p className="text-[10px] text-muted-foreground">
                    video position: {narrativePos + 1}
                  </p>
                )}
              </div>

              {/* Built vs to-build — so the reviewer isn't asked to "build" what's already live */}
              <StatusBadge frontier={frontierById.get(scene.id) ?? false} />

              {/* Up / Down controls — edit mode only */}
              {!readOnly && (
                <div className="shrink-0 flex gap-1">
                  <button
                    type="button"
                    onClick={() => move(idx, 'up')}
                    disabled={idx === 0}
                    title="Move earlier in build sequence"
                    className={[
                      'rounded border px-1.5 py-0.5 text-xs transition-colors',
                      idx === 0
                        ? 'border-border text-foreground-subtle cursor-default'
                        : 'border-input text-foreground-secondary hover:border-muted-foreground hover:text-foreground-secondary',
                    ].join(' ')}
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => move(idx, 'down')}
                    disabled={idx === orderedScenes.length - 1}
                    title="Move later in build sequence"
                    className={[
                      'rounded border px-1.5 py-0.5 text-xs transition-colors',
                      idx === orderedScenes.length - 1
                        ? 'border-border text-foreground-subtle cursor-default'
                        : 'border-input text-foreground-secondary hover:border-muted-foreground hover:text-foreground-secondary',
                    ].join(' ')}
                  >
                    ↓
                  </button>
                </div>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Personas section — the cast, visible + editable
// ---------------------------------------------------------------------------

function inputCls(readOnly: boolean): string {
  return [
    'w-full rounded border bg-card px-2.5 py-1.5 text-sm text-foreground-secondary',
    'border-input focus:border-muted-foreground focus:outline-none transition-colors',
    readOnly ? 'opacity-70 cursor-default' : 'hover:border-muted-foreground cursor-text',
  ].join(' ')
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <span className="block text-[10px] uppercase tracking-wider text-muted-foreground mb-0.5">{children}</span>
}

function PersonasSection({
  personas,
  readOnly,
  onEdit,
}: {
  personas: Record<string, ReviewPersona>
  readOnly: boolean
  onEdit: (key: string, field: 'name' | 'org' | 'role' | 'intro', value: string) => void
}) {
  const keys = Object.keys(personas)
  if (keys.length === 0) return null
  return (
    <section>
      <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-3">
        Personas
      </h2>
      <div className="grid gap-3 sm:grid-cols-2">
        {keys.map((key) => {
          const p = personas[key]
          return (
            <div key={key} className="rounded-lg border border-input bg-background p-4 space-y-2">
              <div className="flex items-center gap-2">
                <span
                  className="inline-block h-3 w-3 rounded-full shrink-0"
                  style={{ backgroundColor: p.color || '#78716c' }}
                />
                <span className="text-sm font-medium text-foreground">{p.name || key}</span>
                {p.org && <span className="text-xs text-muted-foreground">· {p.org}</span>}
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <FieldLabel>Name</FieldLabel>
                  <input
                    className={inputCls(readOnly)}
                    value={p.name}
                    readOnly={readOnly}
                    onChange={(e) => !readOnly && onEdit(key, 'name', e.target.value)}
                  />
                </div>
                <div>
                  <FieldLabel>Org</FieldLabel>
                  <input
                    className={inputCls(readOnly)}
                    value={p.org ?? ''}
                    readOnly={readOnly}
                    placeholder="e.g. Dimagi"
                    onChange={(e) => !readOnly && onEdit(key, 'org', e.target.value)}
                  />
                </div>
              </div>
              <div>
                <FieldLabel>Role</FieldLabel>
                <input
                  className={inputCls(readOnly)}
                  value={p.role}
                  readOnly={readOnly}
                  onChange={(e) => !readOnly && onEdit(key, 'role', e.target.value)}
                />
              </div>
              <div>
                <FieldLabel>Intro</FieldLabel>
                <AutoTextarea
                  className={inputCls(readOnly) + ' resize-none min-h-[3rem]'}
                  value={p.intro}
                  readOnly={readOnly}
                  rows={2}
                  onChange={(e) => !readOnly && onEdit(key, 'intro', e.target.value)}
                />
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// External suggestions — read-only panel shown to the OWNER (populated only for
// a writer). Each is an external reviewer's suggested wording; the owner applies
// the ones they agree with in the editor, then submits.
// ---------------------------------------------------------------------------

function ExternalSuggestions({ suggestions }: { suggestions: ReviewSuggestion[] }) {
  return (
    <section className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-amber-500">
        Suggested edits ({suggestions.length})
      </h2>
      <ol className="space-y-3">
        {suggestions.map((s, i) => {
          const scenes = (s.response_json?.edited_scenes ?? []).filter((sc) => sc.narration != null)
          return (
            <li key={i} className="rounded-md border border-border/60 bg-background/40 p-2 text-sm">
              <div className="mb-1 text-[11px] text-muted-foreground">
                {s.name || 'Guest reviewer'}
                {s.created_at ? ` · ${new Date(s.created_at).toLocaleString()}` : ''}
              </div>
              {s.response_json?.overall_feedback && (
                <p className="mb-1 whitespace-pre-line text-foreground-secondary">
                  {s.response_json.overall_feedback}
                </p>
              )}
              {scenes.map((sc) => (
                <p key={sc.id} className="mb-1 text-foreground-secondary">
                  <span className="text-muted-foreground">{sc.title || sc.id}: </span>
                  {sc.narration}
                </p>
              ))}
            </li>
          )
        })}
      </ol>
      <p className="mt-2 text-[11px] text-muted-foreground">
        Read-only — apply any you agree with in the editor below, then submit.
      </p>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Inner editor — has access to the ReviewEditor context
// Owns all user interaction + submit logic.
// ---------------------------------------------------------------------------

interface ReviewEditorInnerProps {
  review: ReviewDetail
  readOnly: boolean
  /** External (share-token) reviewer: edits go in as SUGGESTIONS, never resolve the gate. */
  canSuggest?: boolean
  /** Called after a successful submit with the updated review */
  onResolved: (updated: ReviewDetail) => void
}

/** The review's cut, by what it IS. A video plays in a `<video>` on this page —
 *  NOT in a frame: an uploaded-content frame has an opaque origin (no
 *  `allow-same-origin`, see UPLOADED_CONTENT_SANDBOX), and the browser's own media
 *  document inside it fetches the bytes without the session cookie, so a private
 *  cut 404s there. Anything else (a deck) goes in the sandboxed frame, where its
 *  own script may drive its slides and nothing more. Until the kind is known we
 *  render nothing rather than guess; if it cannot be learned, the frame. */
function ReviewCut({ walkthroughId }: { walkthroughId: string }) {
  const [kind, setKind] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    getWalkthrough(walkthroughId)
      .then((w) => { if (live) setKind(w.kind) })
      .catch(() => { if (live) setKind('unknown') })
    return () => { live = false }
  }, [walkthroughId])
  const contentSrc = walkthroughContentUrl(walkthroughId)
  if (kind === null) return <div className="h-[60vh]" />
  if (kind === 'video') {
    return <video src={contentSrc} controls className="w-full max-h-[60vh] bg-black" />
  }
  return (
    <iframe
      src={contentSrc}
      title="Review cut"
      sandbox={UPLOADED_CONTENT_SANDBOX}
      className="w-full h-[60vh]"
    />
  )
}

function ReviewEditorInner({ review, readOnly, canSuggest = false, onResolved }: ReviewEditorInnerProps) {
  const {
    state,
    effectiveScenes,
    effectivePersonas,
    effectiveWhyBrief,
    overallFeedback,
    buildOrder,
    isDirty,
    dispatch,
  } = useReviewEditor()

  // Scenes that have any pending edit op in the buffer — drives the per-scene
  // "Edited" badge + border tint so the reviewer can see at a glance which
  // beats they've touched in this session.
  const editedSceneIds = useMemo(() => {
    const ids = new Set<string>()
    for (const op of state.buffer) {
      if ('sceneId' in op && op.sceneId) ids.add(op.sceneId)
    }
    return ids
  }, [state.buffer])

  const shareToken = useRef(new URLSearchParams(window.location.search).get('t')).current

  const newFeatureCounterRef = useRef(0)
  const newSceneCounterRef = useRef(0)

  // Active tab — reflected in the URL (?tab=cuts) so it's shareable/linkable.
  const initialTab = new URLSearchParams(window.location.search).get('tab') === 'cuts' ? 'cuts' : 'narrative'
  // ?expand=1 starts every scene expanded — so the full build substance + evidence is visible
  // (the default collapsed view is for fast human skimming; expand is for judging/print/audit).
  const expandAll = new URLSearchParams(window.location.search).get('expand') === '1'
  const [tab, setTab] = useState<'narrative' | 'cuts'>(initialTab)
  const selectTab = useCallback((t: 'narrative' | 'cuts') => {
    setTab(t)
    const url = new URL(window.location.href)
    if (t === 'cuts') url.searchParams.set('tab', 'cuts')
    else url.searchParams.delete('tab')
    window.history.replaceState({}, '', url)
  }, [])

  const [choices, setChoices] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const dec of review.request_json.decisions ?? []) {
      // The narrative verdict is never pre-chosen: with "approve" pre-selected, a
      // member who only meant to fix a word met one enabled button — "Submit —
      // approve & build" — and one click locked the build plan (canopy-web#1266).
      initial[dec.id] =
        dec.id === 'narrative-verdict' ? '' : (dec.recommended ?? dec.options[0] ?? '')
    }
    return initial
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set once an external reviewer's suggestions land (they don't resolve the gate).
  const [suggested, setSuggested] = useState(false)
  // Set once a decider saves edits without deciding (mode 'save').
  const [saved, setSaved] = useState(false)
  // A share-link guest is anonymous; these let them be named in, and cc'd on, the
  // email the team gets. A signed-in caller's account supplies both server-side.
  const [guestName, setGuestName] = useState('')
  const [guestEmail, setGuestEmail] = useState('')
  const auth = useAuth()
  const viaShareLink = !!shareToken && review.visibility === 'link'
  const [auditOpen, setAuditOpen] = useState(false)

  const req = review.request_json
  const decisions: ReviewDecision[] = req.decisions ?? []
  const autonomousAudit: string[] = req.autonomous_audit ?? []
  const actionability = req.actionability ?? null
  const overallScore = actionability?.overall_score ?? null
  const perScene = actionability?.per_scene ?? {}
  // Use the projected personas so chips + cards reflect in-flight edits.
  const personas = effectivePersonas

  // 1-based scene numbers among non-deleted scenes (renumbers as scenes are added/deleted).
  const sceneNumberById = new Map<string, number>()
  effectiveScenes
    .filter((s) => !s.deleted)
    .forEach((s, i) => sceneNumberById.set(s.id, i + 1))

  // Join each scene to its why-brief grounding (spine item by provenance) + gaps
  // (by claim_ref), so the "why" lives inside the scene it grounds instead of a
  // parallel section. Anything not tied to a scene falls through to "other grounding".
  const spineById = new Map((effectiveWhyBrief.spine ?? []).map((s) => [s.id, s]))
  const gapsByRef = new Map<string, ReviewWhyGap[]>()
  for (const g of effectiveWhyBrief.gaps ?? []) {
    const ref = g.claim_ref ?? ''
    if (!gapsByRef.has(ref)) gapsByRef.set(ref, [])
    gapsByRef.get(ref)!.push(g)
  }
  const usedProvenance = new Set(
    effectiveScenes.filter((s) => !s.deleted).map((s) => s.provenance).filter(Boolean),
  )
  const orphanSpine = (effectiveWhyBrief.spine ?? []).filter((s) => !usedProvenance.has(s.id))
  const orphanGaps = (effectiveWhyBrief.gaps ?? []).filter(
    (g) => !g.claim_ref || !usedProvenance.has(g.claim_ref),
  )

  // Honesty flag before approval: how many scenes are New features (not built) or below the ≥4 bar.
  // A scene is "frontier" (to-build) if its spine item is a gap OR a why-brief gap
  // (CAPABILITY/RESEARCH/DECISION) references it — a grounded claim with an open
  // capability gap is still something we'd build, so it must read as New, not Existing.
  const liveScenes = effectiveScenes.filter((s) => !s.deleted)
  const sceneIsFrontier = (s: { provenance?: string }) =>
    (s.provenance ? (spineById.get(s.provenance)?.status ?? 'grounded') : 'grounded') !== 'grounded' ||
    (s.provenance ? (gapsByRef.get(s.provenance)?.length ?? 0) : 0) > 0
  const frontierScenes = liveScenes.filter(sceneIsFrontier)
  // scene id → frontier, so the BUILD SEQUENCE panel can label built vs to-build
  // using the exact same rule as the scene cards.
  const frontierById = new Map(liveScenes.map((s) => [s.id, sceneIsFrontier(s)]))
  const frontierCount = frontierScenes.length
  const builtSceneCount = liveScenes.length - frontierCount
  const toBuildFeatures = frontierScenes.flatMap((s) => (s.features ?? []).filter((f) => !f.deleted))
  const belowBarCount = liveScenes.filter((s) => (perScene[s.id]?.score ?? 5) < 4).length

  const narrativeVerdictDecision = decisions.find((d) => d.id === 'narrative-verdict') ?? null
  const otherDecisions = decisions.filter((d) => d.id !== 'narrative-verdict')

  // Resolved display: prefer submitted response_json values over local choices
  function resolvedChoice(decId: string): string {
    if (readOnly && review.response_json?.decisions) {
      return review.response_json.decisions[decId] ?? choices[decId] ?? ''
    }
    return choices[decId] ?? ''
  }

  // Submit — builds payload from op-buffer projection
  // mode 'save': a decider keeps their wording edits WITHOUT deciding — they land as
  // a suggestion, exactly like a guest's. Before this, "Submit — approve & build"
  // was the only way to keep an edit, so fixing a word locked the build plan
  // (canopy-web#1266).
  const handleSubmit = useCallback(async (mode: 'decide' | 'save' = 'decide') => {
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      // Build edited_scenes from effectiveScenes (op-buffer projection)
      const editedScenes: ReviewSubmittedScene[] = effectiveScenes.map((scene) => ({
        id: scene.id,
        title: scene.title,
        narration: scene.narration,
        deleted: scene.deleted,
        features: scene.features.map((f) => ({
          id: f.id,
          description: f.description,
          verify: f.verify,
          feedback: f.feedback,
        })),
        feedback: scene.feedback,
      }))

      // Diff personas: send only changed fields per persona key.
      const origPersonas = review.request_json.personas ?? {}
      const editedPersonas: ReviewSubmitPayload['edited_personas'] = {}
      for (const [key, p] of Object.entries(effectivePersonas)) {
        const o = origPersonas[key]
        const delta: Record<string, string> = {}
        for (const f of ['name', 'org', 'role', 'intro'] as const) {
          if ((p[f] ?? '') !== (o?.[f] ?? '')) delta[f] = p[f] ?? ''
        }
        if (Object.keys(delta).length > 0) editedPersonas![key] = delta
      }

      // Diff why-brief prose fields.
      const origWb = review.request_json.why_brief ?? {}
      const wbDelta: ReviewSubmitPayload['edited_why_brief'] = {}
      if ((effectiveWhyBrief.problem ?? '') !== (origWb.problem ?? '')) {
        wbDelta!.problem = effectiveWhyBrief.problem ?? ''
      }
      const spineDelta: Record<string, { claim?: string; rationale?: string }> = {}
      for (const item of effectiveWhyBrief.spine ?? []) {
        const o = (origWb.spine ?? []).find((s) => s.id === item.id)
        const d: { claim?: string; rationale?: string } = {}
        if ((item.claim ?? '') !== (o?.claim ?? '')) d.claim = item.claim ?? ''
        if ((item.rationale ?? '') !== (o?.rationale ?? '')) d.rationale = item.rationale ?? ''
        if (Object.keys(d).length > 0) spineDelta[item.id] = d
      }
      if (Object.keys(spineDelta).length > 0) wbDelta!.spine = spineDelta
      const gapDelta: Record<string, { detail?: string; proposed_action?: string }> = {}
      for (const gap of effectiveWhyBrief.gaps ?? []) {
        const o = (origWb.gaps ?? []).find((g) => g.id === gap.id)
        const d: { detail?: string; proposed_action?: string } = {}
        if ((gap.detail ?? '') !== (o?.detail ?? '')) d.detail = gap.detail ?? ''
        if ((gap.proposed_action ?? '') !== (o?.proposed_action ?? '')) d.proposed_action = gap.proposed_action ?? ''
        if (Object.keys(d).length > 0) gapDelta[gap.id] = d
      }
      if (Object.keys(gapDelta).length > 0) wbDelta!.gaps = gapDelta

      const payload: ReviewSubmitPayload = {
        decisions: choices,
        edited_scenes: editedScenes,
        overall_feedback: overallFeedback,
        build_order: buildOrder,
      }
      if (Object.keys(editedPersonas!).length > 0) payload.edited_personas = editedPersonas
      if (Object.keys(wbDelta!).length > 0) payload.edited_why_brief = wbDelta

      if (canSuggest || mode === 'save') {
        // Land the edits as a SUGGESTION — never resolve the gate. A share-link
        // guest authenticates with the token; a signed-in member with a session.
        await suggestReview(
          review.id, payload, viaShareLink ? shareToken : null,
          guestName.trim() || null, guestEmail.trim() || null,
        )
        if (canSuggest) setSuggested(true)
        else setSaved(true)
      } else {
        const updated = await submitReview(review.id, payload, shareToken)
        onResolved(updated)
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [effectiveScenes, effectivePersonas, effectiveWhyBrief, overallFeedback, buildOrder, choices, review.id, review.request_json, shareToken, viaShareLink, canSuggest, onResolved, guestName, guestEmail])

  // An external suggester sends language edits without resolving the gate, so they
  // are NOT gated on filling in the gate decisions.
  const canSubmit = !busy && (canSuggest || decisions.every((d) => !!choices[d.id]))

  const cutVideos = review.cut_videos ?? []

  // Video embed
  let videoElement: React.ReactNode = null
  if (req.video?.walkthrough_id) {
    videoElement = <ReviewCut walkthroughId={req.video.walkthrough_id} />
  } else if (req.video?.url) {
    videoElement = (
      <video src={withBase(req.video.url)} controls className="w-full max-h-[60vh] bg-black" />
    )
  } else if (review.version_video?.video_url) {
    // The version's own video, uploaded after the review was posted.
    videoElement = (
      <video src={withBase(review.version_video.video_url)} controls className="w-full max-h-[60vh] bg-black" />
    )
  } else {
    videoElement = (
      <div className="flex items-center justify-center h-40 text-muted-foreground text-sm">
        No cut available yet
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto py-8 px-4 space-y-8">
      {canSuggest && (
        <div className="rounded-lg border border-primary/30 bg-primary/10 px-4 py-2 text-sm text-foreground-secondary">
          You can suggest wording here. Edit any narration, then{' '}
          <strong>Send suggestions</strong> — the team is notified; you're not approving anything.
        </div>
      )}
      {review.suggestions.length > 0 && <ExternalSuggestions suggestions={review.suggestions} />}
      {/* Header */}
      <header className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-start gap-3 flex-wrap">
          <div>
            <h1 className="text-xl font-semibold text-foreground">
              {canSuggest
                ? 'Suggest edits to this demo story'
                : req.gate === 'concept_change'
                ? 'Approve the story before we build it'
                : req.gate === 'external_release'
                  ? 'Approve for release'
                  : req.gate === 'product_findings'
                    ? 'Findings review'
                    : `Review: ${req.gate}`}
            </h1>
            {/* The narrative's name, not the run id — "chlorine-dispenser-walkthroughs-
                2026-10-07-002" told a first-time reviewer nothing (canopy-web#1271). */}
            <p className="text-sm text-muted-foreground mt-0.5">{review.title || req.run_id}</p>
          </div>
          {!canSuggest && overallScore != null && (
            <div className="mt-0.5">
              <ScoreBadge
                score={overallScore}
                tooltip="Actionability score — how confidently an AI could build this narrative as written (out of 5)"
              />
              <p className="text-[10px] text-muted-foreground mt-0.5 text-center">Actionability · AI eval</p>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <DirtyBadge isDirty={isDirty} />
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

      {/* Undo bar */}
      {isDirty && !readOnly && (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => dispatch({ type: 'UNDO_LAST_OP' })}
            className="text-xs text-foreground-secondary hover:text-foreground-secondary border border-input hover:border-muted-foreground rounded px-3 py-1 transition-colors"
          >
            ↩ Undo last edit
          </button>
        </div>
      )}

      {/* Tabs — narrative review vs the rendered cut(s) */}
      <div className="flex items-center gap-1 border-b border-border">
        {(['narrative', 'cuts'] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => selectTab(t)}
            className={[
              'px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors',
              tab === t
                ? 'border-primary text-foreground'
                : 'border-transparent text-muted-foreground hover:text-foreground-secondary',
            ].join(' ')}
          >
            {t === 'narrative' ? 'Narrative' : 'Cuts'}
          </button>
        ))}
      </div>

      {/* Cuts tab — the rendered demo cut; kept off the main view so it isn't distracting */}
      {tab === 'cuts' && cutVideos.length > 0 && (
        <CutVideoList
          cuts={cutVideos}
          scenes={effectiveScenes
            .filter((sc) => !sc.deleted)
            .map((sc) => ({ id: sc.id, title: sc.title, text: sc.narration }))}
        />
      )}
      {tab === 'cuts' && cutVideos.length === 0 && (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-2">
            Current cut
          </h2>
          <div className="rounded-lg border border-input bg-black overflow-hidden">
            {videoElement}
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            {canSuggest
              ? 'The recorded demo appears here once it has been rendered.'
              : 'This tab shows the rendered demo cut. Spec/yaml history lives in git — not version-controlled here.'}
          </p>
        </section>
      )}

      {/* Narrative tab — the story, personas, why-brief, scenes, decision */}
      {tab === 'narrative' && (
        <>
      {!readOnly && (
        <p className="text-xs text-foreground-secondary rounded border border-input bg-card/60 px-3 py-2">
          {canSuggest
            ? 'Click any narration to change its wording, then Send suggestions at the bottom.'
            : 'Every field on this page is editable — click any text to change it, drag the build sequence to reorder, then approve or send back at the bottom.'}
        </p>
      )}
      {/* The demo — the cohesive story + the one problem it all serves */}
      {(req.narrative?.trim() || effectiveWhyBrief.problem || Object.keys(personas).length > 0) && (
        <section className="rounded-lg border border-input bg-card/60 p-5 space-y-3">
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider">
            The demo
          </h2>
          {req.narrative?.trim() && (
            <div className="space-y-3">
              {(() => {
                // v2 (gap-flexible-scene-length): iterate the per-scene
                // narration items from the request. Each item's `text` is the
                // canonical scene narrative (single OR multi-sentence). We
                // render each as a clickable span so multi-sentence beats
                // work without breaking the click-to-scroll mapping.
                const liveScenes = effectiveScenes.filter((s) => !s.deleted)
                if (liveScenes.length === 0) {
                  return (
                    <p className="text-[15px] leading-relaxed text-foreground-secondary">
                      {req.narrative.trim()}
                    </p>
                  )
                }
                // One paragraph per cut. Joining every beat into a single <p> ran a
                // seven-cut demo together as ~450 unbroken words (canopy-web#1270).
                let n = 0
                return groupScenesByCut(liveScenes).map((group, gi) => (
                  <div key={gi}>
                    {group.cut && (
                      <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground mb-0.5">
                        {group.cut}
                      </p>
                    )}
                    <p className="text-[15px] leading-relaxed text-foreground-secondary">
                      {group.scenes.map((scene, i) => {
                        const sceneId = scene.id
                        const sceneNo = ++n
                        const isEdited = editedSceneIds.has(sceneId)
                        const stateCls = isEdited
                          ? 'underline decoration-info/60 hover:decoration-info'
                          : 'hover:underline hover:decoration-foreground-secondary/70'
                        return (
                          <span key={sceneId}>
                            {i > 0 && ' '}
                            <span
                              className={[
                                'transition-colors decoration-1 underline-offset-4',
                                'cursor-pointer',
                                stateCls,
                              ].join(' ')}
                              onClick={() => {
                                const el = document.getElementById(`scene-${sceneId}`)
                                if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
                              }}
                              title={`Jump to scene ${sceneNo}`}
                            >
                              {scene.narration}
                            </span>
                          </span>
                        )
                      })}
                    </p>
                  </div>
                ))
              })()}
            </div>
          )}
          {(effectiveWhyBrief.problem || (!readOnly && !canSuggest)) && (
            <div className="pt-1">
              <FieldLabel>The problem we're solving</FieldLabel>
              <AutoTextarea
                className={inputCls(readOnly) + ' resize-none min-h-[3rem]'}
                value={effectiveWhyBrief.problem ?? ''}
                readOnly={readOnly || canSuggest}
                rows={3}
                placeholder="The core problem this whole demo exists to solve"
                onChange={(e) =>
                  !readOnly && dispatch({ type: 'APPEND_OP', op: { op: 'edit-why-problem', value: e.target.value } })
                }
              />
            </div>
          )}
          {Object.keys(personas).length > 0 && (
            <div className="flex items-center gap-2 flex-wrap pt-1">
              <span className="text-[11px] uppercase tracking-wider text-muted-foreground">Cast</span>
              {Object.values(personas).map((p) => (
                <PersonaChip key={p.name} persona={p} dim />
              ))}
            </div>
          )}
        </section>
      )}

      {/* Personas — visible + editable. A suggester already sees the cast above;
          the editable persona form is build-plan input, not wording. */}
      {!canSuggest && (
        <PersonasSection
          personas={personas}
          readOnly={readOnly}
          onEdit={(key, field, value) =>
            dispatch({ type: 'APPEND_OP', op: { op: 'edit-persona', key, field, value } })
          }
        />
      )}

      {/* Narrative verdict decision now lives at the bottom, next to Submit (single decision zone). */}

      {/* Other decisions */}
      {!canSuggest && otherDecisions.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-3">
            {readOnly ? 'Additional decisions (submitted)' : 'Additional decisions'}
          </h2>
          <div className="space-y-3">
            {otherDecisions.map((dec) => (
              <DecisionGroup
                key={dec.id}
                decision={dec}
                chosen={resolvedChoice(dec.id)}
                onChange={(val) => setChoices((prev) => ({ ...prev, [dec.id]: val }))}
                readOnly={readOnly}
              />
            ))}
          </div>
        </section>
      )}

      {/* What approving does — the next-action + engage-vs-delegate summary */}
      {!readOnly && !canSuggest && liveScenes.length > 0 && resolvedChoice('narrative-verdict') !== 'redraft' && (
        <section className="rounded-lg border border-info/30 bg-info/[0.06] p-4">
          <h2 className="text-sm font-semibold text-info mb-2">If you approve, running DDD next will…</h2>
          <ul className="space-y-1 text-sm text-foreground-secondary">
            <li>
              <span className="text-muted-foreground">▶</span>{' '}
              <span className="text-foreground">Render {liveScenes.length} scene{liveScenes.length === 1 ? '' : 's'}</span>{' '}
              against the live app and screenshot each beat.
            </li>
            <li>
              <span className="text-muted-foreground">🔨</span>{' '}
              <span className="text-foreground">
                Build {toBuildFeatures.length} new feature{toBuildFeatures.length === 1 ? '' : 's'}
                {frontierCount > 0 && ` (across ${frontierCount} beat${frontierCount === 1 ? '' : 's'})`}
              </span>
              {frontierCount > 0 && toBuildFeatures.length > 0 ? (
                <>
                  :{' '}
                  <span className="text-warning">
                    {toBuildFeatures.map((f) => f.id).join(', ')}
                  </span>
                </>
              ) : (
                <span className="text-muted-foreground"> — none; every beat already rides shipped code</span>
              )}
              .
            </li>
            <li>
              <span className="text-muted-foreground">⚖</span>{' '}
              <span className="text-foreground">Re-judge</span> (concept + actionability) and loop, stopping at the
              next <em className="text-foreground-secondary">concept_change</em> or <em className="text-foreground-secondary">external_release</em> gate.
            </li>
          </ul>
          <p className="text-xs text-muted-foreground mt-3">
            {builtSceneCount}/{liveScenes.length} scenes already ride shipped code.{' '}
            {frontierCount > 0 ? (
              <>
                The build is {toBuildFeatures.length} scoped feature{toBuildFeatures.length === 1 ? '' : 's'} —{' '}
                <span className="text-success">safe to delegate</span>. The story framing is the part that&apos;s{' '}
                <span className="text-info">yours to approve</span>.
              </>
            ) : (
              <>Nothing new to build — this is a <span className="text-success">render-and-confirm</span> pass.</>
            )}
          </p>
        </section>
      )}

      {/* Scene cards — driven by op-buffer projection */}
      {(effectiveScenes.length > 0 || !readOnly) && (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-1">
            {readOnly ? 'Scenes (submitted)' : 'Scenes — the story, beat by beat'}
          </h2>
          {canSuggest ? (
            <p className="text-xs text-muted-foreground mb-3">
              Each scene is one beat of the demo. Change any wording below.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground mb-3">
              <span className="text-success">Existing feature</span> = backed by shipped code ·{' '}
              <span className="text-warning">New feature</span> = intended, not built yet. Each scene = one beat of the demo.
              Per-scene numbers are AI actionability estimates (1–5; passes at ≥4).
            </p>
          )}
          <div className="space-y-4">
            {effectiveScenes.map((scene) => (
              <SceneCard
                key={scene.id}
                scene={scene}
                sceneNumber={sceneNumberById.get(scene.id)}
                isEdited={editedSceneIds.has(scene.id)}
                suggestOnly={canSuggest}
                defaultOpen={
                  expandAll ||
                  sceneIsFrontier(scene) ||
                  (perScene[scene.id]?.score ?? 5) < 4 ||
                  editedSceneIds.has(scene.id)
                }
                persona={scene.persona ? personas[scene.persona] : undefined}
                grounding={scene.provenance ? spineById.get(scene.provenance) : undefined}
                gaps={scene.provenance ? gapsByRef.get(scene.provenance) : undefined}
                readOnly={readOnly}
                sceneActionability={perScene[scene.id] ?? undefined}
                onEditRationale={(value) =>
                  scene.provenance &&
                  dispatch({
                    type: 'APPEND_OP',
                    op: { op: 'edit-why-spine', id: scene.provenance, field: 'rationale', value },
                  })
                }
                onEditGap={(gapId, field, value) =>
                  dispatch({ type: 'APPEND_OP', op: { op: 'edit-why-gap', id: gapId, field, value } })
                }
                onEditNarration={(text) =>
                  dispatch({ type: 'APPEND_OP', op: { op: 'edit-narration', sceneId: scene.id, text } })
                }
                onEditFeature={(featureId, field, value) =>
                  dispatch({
                    type: 'APPEND_OP',
                    op: { op: 'edit-feature', sceneId: scene.id, featureId, field, value },
                  })
                }
                onFeatureFeedback={(featureId, text) =>
                  dispatch({
                    type: 'APPEND_OP',
                    op: { op: 'set-feature-feedback', sceneId: scene.id, featureId, text },
                  })
                }
                onDeleteFeature={(featureId) =>
                  dispatch({ type: 'APPEND_OP', op: { op: 'delete-feature', sceneId: scene.id, featureId } })
                }
                onAddFeature={() => {
                  newFeatureCounterRef.current += 1
                  const featureId = `new-${newFeatureCounterRef.current}`
                  dispatch({ type: 'APPEND_OP', op: { op: 'add-feature', sceneId: scene.id, featureId } })
                }}
                onDeleteScene={() =>
                  dispatch({ type: 'APPEND_OP', op: { op: 'delete-scene', sceneId: scene.id } })
                }
                onSceneFeedback={(text) =>
                  dispatch({ type: 'APPEND_OP', op: { op: 'set-scene-feedback', sceneId: scene.id, text } })
                }
              />
            ))}

            {/* Add scene */}
            {!readOnly && !canSuggest && (
              <button
                type="button"
                onClick={() => {
                  newSceneCounterRef.current += 1
                  const sceneId = `new-${newSceneCounterRef.current}`
                  dispatch({
                    type: 'APPEND_OP',
                    op: { op: 'add-scene', sceneId, title: `New Scene ${newSceneCounterRef.current}` },
                  })
                }}
                className="w-full rounded-lg border border-dashed border-input hover:border-muted-foreground text-muted-foreground hover:text-foreground-secondary py-3 text-sm transition-colors"
              >
                + Add scene
              </button>
            )}
          </div>
        </section>
      )}

      {/* Other grounding — spine claims / gaps not tied to any scene (kept so nothing is lost) */}
      {(orphanSpine.length > 0 || orphanGaps.length > 0) && (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-1">
            Other grounding
          </h2>
          <p className="text-xs text-muted-foreground mb-3">
            Why-brief items not tied to any scene above. Tie one to a scene by setting that scene's provenance.
          </p>
          <div className="rounded-lg border border-border bg-card/40 p-4 space-y-3">
            {orphanSpine.map((item) => (
              <div key={item.id} className="text-sm">
                <div className="flex items-center gap-2 text-xs mb-1">
                  <span className="font-mono text-foreground-secondary">{item.id}</span>
                  <StatusBadge status={item.status} />
                </div>
                <p className="text-foreground-secondary">{item.claim}</p>
                {item.rationale && <p className="text-muted-foreground text-xs mt-0.5">{item.rationale}</p>}
              </div>
            ))}
            {orphanGaps.map((gap) => (
              <div key={gap.id} className="text-sm">
                <div className="flex items-center gap-2 text-xs mb-1">
                  <span className="font-mono text-foreground-secondary">{gap.id}</span>
                  {gap.type && (
                    <span className="rounded bg-warning/15 text-warning px-1.5 py-0.5">{gap.type}</span>
                  )}
                </div>
                <p className="text-foreground-secondary">{gap.detail}</p>
                {gap.proposed_action && (
                  <p className="text-muted-foreground text-xs mt-0.5">→ {gap.proposed_action}</p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Build sequence — independent of the video order. Engineering's tackle
          order; a suggester is editing words, not planning the build. */}
      {!canSuggest && effectiveScenes.filter((s) => !s.deleted).length > 0 && (
        <BuildSequenceSection
          effectiveScenes={effectiveScenes}
          buildOrder={buildOrder}
          readOnly={readOnly}
          onReorder={(orderedIds) =>
            dispatch({ type: 'APPEND_OP', op: { op: 'set-build-order', orderedSceneIds: orderedIds } })
          }
          resolvedBuildOrder={
            readOnly && review.response_json?.build_order
              ? review.response_json.build_order
              : null
          }
          frontierById={frontierById}
        />
      )}

      {/* Overall feedback */}
      {!readOnly && (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-2">
            Overall feedback
          </h2>
          <AutoTextarea
            className="w-full rounded border bg-card px-3 py-2 text-sm text-foreground-secondary resize-none min-h-[3rem] border-input focus:border-muted-foreground focus:outline-none transition-colors"
            value={overallFeedback}
            onChange={(e) =>
              dispatch({ type: 'APPEND_OP', op: { op: 'set-overall-feedback', text: e.target.value } })
            }
            rows={2}
            placeholder={
              canSuggest
                ? 'Anything else the team should know (optional)'
                : 'Any overall feedback for the re-draft (optional)'
            }
          />
        </section>
      )}
      {readOnly && review.response_json?.overall_feedback && (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-2">
            Overall feedback (submitted)
          </h2>
          <p className="text-sm text-foreground-secondary italic">{review.response_json.overall_feedback}</p>
        </section>
      )}

      {/* Autonomous audit — collapsed by default */}
      {autonomousAudit.length > 0 && (
        <section>
          <button
            type="button"
            onClick={() => setAuditOpen((o) => !o)}
            className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground-secondary transition-colors"
          >
            <svg
              className={['h-4 w-4 transition-transform', auditOpen ? 'rotate-90' : ''].join(' ')}
              viewBox="0 0 12 12"
              fill="none"
            >
              <path
                d="M4 3l4 3-4 3"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            What I did autonomously ({autonomousAudit.length} step
            {autonomousAudit.length === 1 ? '' : 's'})
          </button>
          {auditOpen && (
            <ul className="mt-3 space-y-1.5 pl-4 border-l border-border">
              {autonomousAudit.map((entry, i) => (
                <li key={i} className="text-sm text-foreground-secondary">
                  {entry}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* Error */}
      {error && (
        <p className="text-sm text-destructive rounded border border-destructive/30 bg-destructive/10 px-3 py-2">
          {error}
        </p>
      )}

      {/* Your decision — single decision zone, immediately above Submit. A guest
          only suggests, so they get no decision: showing them "Approve & continue"
          under a banner saying they approve nothing is the contradiction #1267 is. */}
      {narrativeVerdictDecision && !canSuggest && (
        <section>
          <h2 className="text-sm font-semibold text-foreground-secondary uppercase tracking-wider mb-3">
            {readOnly ? 'Decision (submitted)' : 'Your decision'}
          </h2>
          <NarrativeVerdictControl
            decision={narrativeVerdictDecision}
            chosen={resolvedChoice('narrative-verdict')}
            onChange={(val) => setChoices((prev) => ({ ...prev, 'narrative-verdict': val }))}
            readOnly={readOnly}
          />
        </section>
      )}

      {/* Submit / resolved state */}
      {readOnly ? (
        <div className="rounded-lg border border-success/30 bg-success/10 px-4 py-3">
          <p className="text-sm text-success font-medium">
            Review resolved — the loop will continue.
          </p>
          {review.resolved_at && (
            <p className="text-xs text-success mt-0.5">
              Submitted {new Date(review.resolved_at).toLocaleString()}
            </p>
          )}
        </div>
      ) : suggested ? (
        <div className="flex flex-col items-end gap-1">
          <p className="text-sm text-success">✓ Thanks — your suggestions were sent.</p>
          <span className="text-[11px] text-muted-foreground">
            The team has been notified and will review your wording. You can keep editing and send more.
          </span>
        </div>
      ) : (
        <div className="flex flex-col items-end gap-1">
          {canSuggest && auth.status !== 'authenticated' && (
            <div className="flex flex-col sm:flex-row gap-2 mb-2 w-full sm:w-auto">
              <input
                type="text"
                aria-label="Your name"
                placeholder="Your name (optional)"
                value={guestName}
                onChange={(e) => setGuestName(e.target.value)}
                className="rounded border border-input bg-card px-3 py-1.5 text-sm text-foreground-secondary focus:border-muted-foreground focus:outline-none"
              />
              <input
                type="email"
                aria-label="Your email"
                placeholder="Your email — to get a copy (optional)"
                value={guestEmail}
                onChange={(e) => setGuestEmail(e.target.value)}
                className="rounded border border-input bg-card px-3 py-1.5 text-sm text-foreground-secondary focus:border-muted-foreground focus:outline-none sm:w-72"
              />
            </div>
          )}
          {!canSuggest && (frontierCount > 0 || belowBarCount > 0) && resolvedChoice('narrative-verdict') !== 'redraft' && (
            <p className="text-[11px] text-warning/90 max-w-md text-right mb-1">
              ⚠ {frontierCount > 0 && `${frontierCount} scene${frontierCount === 1 ? ' shows a new feature' : 's show new features'} (not built yet)`}
              {frontierCount > 0 && belowBarCount > 0 && '; '}
              {belowBarCount > 0 && `${belowBarCount} below the ≥4 actionability bar`}
              . Approving commits to building these as intended — they are not yet verified.
            </p>
          )}
          {!canSuggest && (
            <div className="flex flex-col items-end gap-0.5 mb-2">
              <button
                type="button"
                onClick={() => void handleSubmit('save')}
                disabled={busy || !isDirty}
                className={[
                  'px-4 py-1.5 rounded border text-sm transition-colors',
                  busy || !isDirty
                    ? 'border-input text-muted-foreground cursor-not-allowed'
                    : 'border-muted-foreground text-foreground hover:bg-muted/50',
                ].join(' ')}
              >
                Save edits without deciding
              </button>
              <span className="text-[11px] text-muted-foreground">
                {saved
                  ? '✓ Saved as a suggestion — nothing was approved.'
                  : 'Keeps your wording as a suggestion. Nothing is approved or built.'}
              </span>
            </div>
          )}
          <button
            type="button"
            onClick={() => void handleSubmit('decide')}
            disabled={!canSubmit}
            className={[
              'px-6 py-2 rounded font-medium text-sm transition-colors',
              !canSubmit
                ? 'bg-input text-foreground-secondary cursor-not-allowed'
                : 'bg-primary hover:bg-primary/90 text-white',
            ].join(' ')}
          >
            {busy
              ? canSuggest
                ? 'Sending…'
                : 'Submitting…'
              : canSuggest
                ? 'Send suggestions'
                : narrativeVerdictDecision && !resolvedChoice('narrative-verdict')
                  ? 'Choose approve or re-draft above'
                  : resolvedChoice('narrative-verdict') === 'redraft'
                    ? 'Submit — send back to re-draft'
                    : 'Submit — approve & build'}
          </button>
          <span className="text-[11px] text-muted-foreground">
            {canSuggest
              ? 'Sends your wording suggestions to the team — this does not approve anything.'
              : 'Commits your decision above (with any edits you made).'}
          </span>
        </div>
      )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Outer page shell — fetches data, owns resolved state, wraps provider
// ---------------------------------------------------------------------------

export function ReviewPage() {
  // `workspace` is set on /w/:workspace/review/:id — the API confirms the
  // review lives there (see FlatArtifactRedirect for the flat route).
  const { id, workspace } = useParams<{ id: string; workspace?: string }>()
  const auth = useAuth()

  // ?t= share-token (stable for page lifetime — intentionally not in deps)
  const shareToken = useRef(new URLSearchParams(window.location.search).get('t')).current

  const [review, setReview] = useState<ReviewDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    getReview(id, shareToken, workspace)
      .then((d) => { if (!cancelled) setReview(d) })
      .catch((e) => { if (!cancelled) setError(String(e?.message ?? e)) })
    return () => { cancelled = true }
  }, [id]) // eslint-disable-line react-hooks/exhaustive-deps

  // The DDD rail belongs to internal, signed-in browsing of a review that is
  // actually part of a narrative. Three cases get no rail:
  //   - anonymous (the rail's APIs need a session and would just 403)
  //   - a run-child gate (narrative_slug === null) — it belongs to no narrative,
  //     so the DDD rail is not its chrome. Rendering it anyway is what put Ada's
  //     fleet audit in a rail it could not navigate, under a narrative that only
  //     existed because someone parsed its run_id.
  //   - still loading — we don't know yet, and flashing a rail in then out is
  //     worse than showing it a beat late.
  // Either way we keep our own scroll container so the full-bleed AppLayout main
  // doesn't clip the editor.
  const showShell =
    !shareToken && auth.status === 'authenticated' && review?.narrative_slug != null
  const withChrome = (node: ReactNode) =>
    showShell ? (
      <DddShell activeSlug={review?.narrative_slug ?? undefined} activeRunId={review?.run_id}>
        {node}
      </DddShell>
    ) : (
      <div className="h-full overflow-y-auto">{node}</div>
    )

  // -------------------------------------------------------------------
  // Loading / error states
  // -------------------------------------------------------------------

  if (error && !review) {
    return withChrome(
      <div className="max-w-4xl mx-auto p-6 text-destructive">
        <p className="font-semibold">Error loading review</p>
        <p className="text-sm mt-1 text-destructive">{error}</p>
      </div>,
    )
  }

  if (!review) {
    return withChrome(<div className="max-w-4xl mx-auto p-6 text-muted-foreground">Loading…</div>)
  }

  const isResolved = review.status === 'resolved'

  // Anyone who may NOT decide gets the suggest-only editor: their action sends
  // suggestions (never resolves the gate). Keyed on the server's `can_decide`, not
  // on "is anyone signed in" — that handed a signed-in non-editor (say, a Dimagi
  // reviewer opening the guest link) an approve button the server then refused
  // with a 403, losing their edits (canopy-web#1268).
  const viaShareLink = !!shareToken && review.visibility === 'link'
  const canSuggest =
    !isResolved &&
    !review.can_decide &&
    (viaShareLink || auth.status === 'authenticated')

  // Product-findings reviews are run-children with their own first-class
  // surface — they don't use the narrative editor machinery. Branch before the
  // ReviewEditorProvider so none of the narration/scene scaffolding mounts.
  if (review.request_json.gate === 'product_findings') {
    const findingsReq = review.request_json as unknown as ProductFindingsRequestJson
    const findingsResolved = review.response_json as unknown as ProductFindingsResponseJson | null
    const findingsReadOnly = isResolved || auth.status !== 'authenticated'
    return withChrome(
      <ProductFindingsReview
        review={findingsReq}
        readOnly={findingsReadOnly}
        resolved={findingsResolved}
        resolvedAt={review.resolved_at}
        onSubmit={async (response) => {
          const updated = await submitFindingsReview(review.id, response, shareToken)
          setReview(updated)
        }}
      />,
    )
  }

  return withChrome(
    <ReviewEditorProvider
      original={review.request_json.narration ?? []}
      initialBuildOrder={review.request_json.build_order ?? null}
      personas={review.request_json.personas ?? {}}
      whyBrief={review.request_json.why_brief ?? null}
    >
      <ReviewEditorInner
        review={review}
        // Resolving the gate requires a Dimagi login; read-only once resolved. An
        // external share-token holder may edit to SUGGEST (canSuggest), so the editor
        // is live for them even though they're not authenticated.
        readOnly={isResolved || (!review.can_decide && !canSuggest)}
        canSuggest={canSuggest}
        onResolved={(updated) => setReview(updated)}
      />
    </ReviewEditorProvider>,
  )
}
