import type { JSX, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { StatusBadge } from 'canopy-ui/ui'

/**
 * One app, as someone learning about it reads it, whatever kind of app it is: who made it and what
 * it says it is, then what it does on the left and the facts beside it.
 */

const BACK_CLASS = 'text-sm text-primary hover:underline'

/** The page before there's an app to draw: still loading, or the API's reason there isn't one. */
export function AppDetailPending({ back, error }: { back: string; error: string | null }): JSX.Element {
  return (
    <div className="mx-auto max-w-5xl p-6">
      <Link to={back} className={BACK_CLASS}>← Apps</Link>
      {error
        ? <p className="mt-4 text-sm text-destructive">{error}</p>
        : <p className="mt-4 text-sm text-muted-foreground">Loading…</p>}
    </div>
  )
}

export function AppDetailLayout({
  back,
  name,
  badge,
  byline,
  tagline,
  action,
  error,
  media,
  aside,
  children,
}: {
  back: string
  name: string
  badge?: ReactNode
  byline: ReactNode
  tagline?: ReactNode
  /** What to do about this app, top right. */
  action?: ReactNode
  error?: string | null
  /** Screenshots, for an app that has them. */
  media?: ReactNode
  aside: ReactNode
  children: ReactNode
}): JSX.Element {
  return (
    <div className="mx-auto max-w-5xl p-6" data-testid="app-detail">
      <Link to={back} className={BACK_CLASS}>← Apps</Link>
      <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 max-w-prose">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-semibold">{name}</h1>
            {badge}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">{byline}</div>
          {tagline && <p className="mt-2 text-base">{tagline}</p>}
        </div>
        {action && <div className="flex flex-col items-end gap-1 text-sm">{action}</div>}
      </div>
      {error && <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
      {media}
      <div className="mt-8 grid gap-8 lg:grid-cols-3">
        <div className="min-w-0 space-y-8 lg:col-span-2">{children}</div>
        <aside className="min-w-0 space-y-6 text-sm">{aside}</aside>
      </div>
    </div>
  )
}

export function AppDetailSection({ title, children, ...rest }: { title: string; children: ReactNode; 'data-testid'?: string; id?: string }): JSX.Element {
  return (
    <section {...rest}>
      <h2 className="text-lg font-semibold">{title}</h2>
      {children}
    </section>
  )
}

export function AppAsideSection({ title, children }: { title: string; children: ReactNode }): JSX.Element {
  return (
    <section>
      <h2 className="font-semibold">{title}</h2>
      {children}
    </section>
  )
}

/** What the app says it is, in its own paragraphs. */
export function AppOverview({ text }: { text: string }): JSX.Element {
  return (
    <AppDetailSection title="What it does">
      {text.split(/\n\s*\n/).filter(Boolean).map((p) => <p key={p.slice(0, 40)} className="mt-2 max-w-prose whitespace-pre-line text-sm leading-relaxed">{p}</p>)}
    </AppDetailSection>
  )
}

export function AppVersions({
  current,
  versions,
}: {
  current: string
  versions: readonly { version: string; released_at?: string | null; changelog?: string; revoked?: boolean }[]
}): JSX.Element {
  return (
    <AppAsideSection title="Versions">
      <div className="mt-1 space-y-3">
        {versions.map((v) => (
          <details key={v.version} open={v.version === current}>
            <summary className="cursor-pointer">
              {v.version} <span className="text-xs text-muted-foreground">{v.released_at?.slice(0, 10)}</span>
              {v.revoked && <StatusBadge tone="destructive" className="ml-2">Withdrawn</StatusBadge>}
            </summary>
            <p className="mt-1 whitespace-pre-line text-xs text-muted-foreground">{v.changelog?.split(/\n\s*\n/)[0]}</p>
          </details>
        ))}
      </div>
    </AppAsideSection>
  )
}
