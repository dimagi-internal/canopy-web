import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listStoryboards, type StoryboardListItem } from '@/api/storyboards'

/**
 * The index of storyboards — the shared arcs, one row each.
 *
 * A storyboard is the thing you SEND: several DDD narratives as one link,
 * gated by its own token. Until this page existed the only way to find one
 * again was the API, so a board's share link lived wherever it was last
 * pasted. This is the member-side list; the board itself opens at
 * /storyboard/:slug, which a member reaches without the token.
 */

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; items: StoryboardListItem[] }
  | { kind: 'error'; message: string }

const LAYOUT_LABEL: Record<StoryboardListItem['layout'], string> = {
  review: 'Review',
  reel: 'Reel',
}

const CAPABILITY_LABEL: Record<StoryboardListItem['capability'], string> = {
  read: 'Read only',
  comment: 'Can comment',
  suggest: 'Can suggest edits',
}

export default function StoryboardsPage() {
  const [state, setState] = useState<LoadState>({ kind: 'loading' })

  useEffect(() => {
    let cancelled = false
    listStoryboards()
      .then((data) => {
        if (!cancelled) setState({ kind: 'ready', items: data.items })
      })
      .catch((e: unknown) => {
        if (!cancelled) setState({ kind: 'error', message: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="mx-auto max-w-5xl p-6">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Storyboards</h1>
        <p className="text-sm text-muted-foreground">
          Several demos as one shareable link — a review board for feedback, a reel for the finished videos
        </p>
      </header>

      {state.kind === 'error' && (
        <div className="text-sm text-destructive">Couldn’t load storyboards: {state.message}</div>
      )}
      {state.kind === 'loading' && <div className="text-sm text-muted-foreground">Loading…</div>}
      {state.kind === 'ready' && state.items.length === 0 && (
        <div className="text-sm text-muted-foreground">
          No storyboards yet. Author one beside its narratives and import it with{' '}
          <code>manage.py import_storyboard</code>.
        </div>
      )}
      {state.kind === 'ready' && state.items.length > 0 && (
        <ul className="flex flex-col gap-3">
          {state.items.map((b) => (
            <StoryboardRow key={b.slug} board={b} />
          ))}
        </ul>
      )}
    </div>
  )
}

function StoryboardRow({ board }: { board: StoryboardListItem }) {
  const [copied, setCopied] = useState(false)

  async function copyLink() {
    if (!board.share_url) return
    try {
      await navigator.clipboard.writeText(board.share_url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* the field beside the button still shows the link */
    }
  }

  return (
    <li className="grid gap-3 rounded-lg border border-border bg-card p-4 sm:grid-cols-[1fr_auto]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <Link
            to={`/storyboard/${encodeURIComponent(board.slug)}`}
            className="text-[15px] font-semibold text-foreground hover:underline"
          >
            {board.title}
          </Link>
          <span className="rounded bg-muted px-2 py-0.5 text-xs text-foreground-secondary">
            {LAYOUT_LABEL[board.layout] ?? board.layout}
          </span>
          <span className="text-xs text-muted-foreground">
            {board.act_count} {board.act_count === 1 ? 'act' : 'acts'} · {CAPABILITY_LABEL[board.capability] ?? board.capability}
          </span>
        </div>
        {board.lede && <p className="mt-1 max-w-prose text-sm text-muted-foreground">{board.lede}</p>}
        <p className="mt-2 font-mono text-xs text-muted-foreground">{board.slug}</p>
      </div>
      <div className="flex items-start gap-2 sm:justify-end">
        {board.share_url && (
          <button
            type="button"
            onClick={copyLink}
            className="rounded border border-border px-3 py-1.5 text-sm hover:bg-muted"
            aria-label={`Copy the share link for ${board.title}`}
          >
            {copied ? 'Copied' : 'Copy share link'}
          </button>
        )}
      </div>
    </li>
  )
}
