import { useContext, type JSX } from 'react'
import { Link, UNSAFE_LocationContext } from 'react-router-dom'

import { LIVE, type BrainSource } from '@/api/brain'

// Which data source the brain's screens read: live, or one of your preview snapshots. It lives in the URL
// (`?preview=<id>`), so a preview view is a link (that only its owner can open); every brain read passes it through
// as `source`, and the screens stay the same.

export function useBrainSource(): { source: BrainSource; preview: string | null; withSource: (path: string) => string } {
  // Read without `useSearchParams`, which throws outside a router: a menu that shows a session's learning is also
  // drawn where there is none (a widget, a test), and there it is simply live.
  const location = useContext(UNSAFE_LocationContext)?.location
  const raw = new URLSearchParams(location?.search ?? '').get('preview')
  const preview = raw && /^\d+$/.test(raw) ? raw : null
  return {
    source: preview ? `preview:${preview}` : LIVE,
    preview,
    // A link to another brain screen keeps the source.
    withSource: (path: string) => (preview ? `${path}${path.includes('?') ? '&' : '?'}preview=${preview}` : path),
  }
}

type Window = { since?: string | null; until?: string | null } | null | undefined

function windowText(w: Window): string {
  if (!w?.since || !w.until) return ''
  const fmt = (s: string) => new Date(s).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
  return `${fmt(w.since)} – ${fmt(w.until)}`
}

/** The one banner a screen shows while it reads a preview, with the way back to live. */
export function BrainSourceBanner({ window }: { window?: Window }): JSX.Element | null {
  const { preview } = useBrainSource()
  const location = useContext(UNSAFE_LocationContext)?.location
  if (!preview || !location) return null
  const params = new URLSearchParams(location.search)
  params.delete('preview')
  const exit = `${location.pathname}${params.toString() ? `?${params}` : ''}`
  const when = windowText(window)
  return (
    <div role="status" data-testid="brain-preview-banner"
         className="flex flex-wrap items-center gap-2 rounded-md border border-info/30 bg-info/10 px-3 py-2 text-[13px] text-foreground">
      <span className="font-medium">Preview: what the brain would do</span>
      {when && <span className="text-muted-foreground">· {when}</span>}
      <span className="text-muted-foreground">·</span>
      <Link to={exit} className="underline">Exit preview</Link>
    </div>
  )
}
