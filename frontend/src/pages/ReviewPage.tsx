import { useEffect, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { DddShell } from '@/components/ddd/DddShell'
import { useAuth } from '@/auth/AuthProvider'
import {
  getReview,
  getReviewDestination,
  submitFindingsReview,
  type ProductFindingsRequestJson,
  type ProductFindingsResponseJson,
  type ReviewDetail,
} from '../api/reviews'
import { ProductFindingsReview } from '../components/reviews/ProductFindingsReview'

/**
 * `/w/:workspace/review/:id` — the retired review page's address.
 *
 * A narrative review is read, commented on and decided on its STORYBOARD: the
 * gate decision is a decision row there, and ruling it resolves the review
 * server-side with the response the DDD loop reads. Review links already went
 * out, so this address forwards: the server 302s a fresh load
 * (apps/reviews/views.py), and an in-app navigation lands here and asks the
 * same question of `/api/reviews/<id>/destination`. The board's share token
 * rides along only for a caller holding the review's own valid token.
 *
 * A findings review (`product_findings`) belongs to no narrative and keeps its
 * own page, rendered below.
 */
export function ReviewPage() {
  const { id, workspace } = useParams<{ id: string; workspace?: string }>()
  const navigate = useNavigate()
  const auth = useAuth()
  // ?t= share token (stable for the page's lifetime).
  const [shareToken] = useState(() => new URLSearchParams(window.location.search).get('t'))

  const [review, setReview] = useState<ReviewDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    getReviewDestination(id, shareToken, workspace)
      .then(async (dest) => {
        if (cancelled) return
        if (dest.url) {
          navigate(dest.url, { replace: true })
          return
        }
        const d = await getReview(id, shareToken, workspace)
        if (!cancelled) setReview(d)
      })
      .catch((e) => { if (!cancelled) setError(String(e?.message ?? e)) })
    return () => { cancelled = true }
  }, [id]) // eslint-disable-line react-hooks/exhaustive-deps

  const showShell = !shareToken && auth.status === 'authenticated' && review?.narrative_slug != null
  const withChrome = (node: ReactNode) =>
    showShell ? (
      <DddShell activeSlug={review?.narrative_slug ?? undefined} activeRunId={review?.run_id}>
        {node}
      </DddShell>
    ) : (
      <div className="h-full overflow-y-auto">{node}</div>
    )

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
  if (review.request_json.gate !== 'product_findings') {
    // A narrative review with no board to forward to (it has no workspace).
    return withChrome(
      <div className="max-w-4xl mx-auto p-6 text-muted-foreground">
        This review is read and decided on its storyboard, and none could be found for it.
      </div>,
    )
  }

  const isResolved = review.status === 'resolved'
  return withChrome(
    <ProductFindingsReview
      review={review.request_json as unknown as ProductFindingsRequestJson}
      readOnly={isResolved || auth.status !== 'authenticated'}
      resolved={review.response_json as unknown as ProductFindingsResponseJson | null}
      resolvedAt={review.resolved_at}
      onSubmit={async (response) => {
        const updated = await submitFindingsReview(review.id, response, shareToken)
        setReview(updated)
      }}
    />,
  )
}
