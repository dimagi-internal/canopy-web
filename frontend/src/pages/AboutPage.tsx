import { useEffect, useState } from 'react'
import { COMPONENTS, ONE_SENTENCE } from '@/guide/components'
import { USER_ROLES } from '@/guide/paths'
import { getPublicStats, type PublicStats } from '@/api/publicStats'
import { apiUrl } from '@/api/base'

/**
 * The public explainer. Chrome-less, anonymous, mounted OUTSIDE AppLayout so
 * the shell's authed calls cannot bounce a visitor to login.
 *
 * Counts are live because prose goes stale within a month — "we run a fleet of
 * agents" reads as a claim, while "2 runners online" reads as a fact. The page
 * renders fine without them.
 */
export function AboutPage() {
  const [stats, setStats] = useState<PublicStats | null>(null)

  useEffect(() => {
    void getPublicStats().then(setStats)
  }, [])

  const figures: Array<[string, number | undefined]> = [
    ['agents', stats?.agents],
    ['skills', stats?.skills],
    ['runners online', stats?.runners_online],
    ['turns executed', stats?.turns_executed],
    ['demos published', stats?.demos_published],
  ]

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-3xl px-6 py-16">
        <h1 className="text-2xl font-semibold text-foreground">Canopy</h1>
        <p className="mt-4 text-[15px] leading-relaxed text-foreground-secondary">
          Canopy runs a fleet of AI agents that do real work — shipping code, triaging
          mail, building demos — and keeps the durable record of what they did, so the
          work survives the session it happened in.
        </p>

        {stats ? (
          <dl className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-3">
            {figures.map(([label, value]) => (
              <div key={label} className="rounded-lg border border-border bg-card p-3">
                <dd className="text-xl font-semibold text-foreground">{value ?? '—'}</dd>
                <dt className="mt-0.5 text-[11px] uppercase tracking-wide text-muted-foreground">
                  {label}
                </dt>
              </div>
            ))}
          </dl>
        ) : null}

        <section className="mt-12">
          <h2 className="text-sm font-semibold text-foreground">How it fits together</h2>
          <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">{ONE_SENTENCE}</p>
          <div className="mt-4 space-y-2">
            {COMPONENTS.map((c) => (
              <div key={c.name} className="rounded-lg border border-border bg-card p-3">
                <h3 className="text-[13px] font-semibold text-foreground">{c.name}</h3>
                <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">{c.what}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="mt-12">
          <h2 className="text-sm font-semibold text-foreground">Four roles</h2>
          <div className="mt-4 space-y-3">
            {USER_ROLES.map((p) => (
              <div key={p.id} className="rounded-lg border border-border bg-card p-4">
                <h3 className="text-[13px] font-semibold text-foreground">{p.title}</h3>
                <p className="mt-0.5 text-[12px] text-muted-foreground">{p.who}</p>
              <p className="mt-1 text-[11px] text-warning">Enforced by: {p.enforcement}</p>
                <p className="mt-2 text-[13px] text-foreground-secondary">{p.startHere}</p>
                {p.note ? <p className="mt-1 text-[12px] text-foreground-subtle">{p.note}</p> : null}
                <div className="mt-2 flex flex-wrap gap-2">
                  {p.surfaces.map((s) => (
                    // Plain text, deliberately not a link — these are route
                    // TEMPLATES (e.g. /w/:workspace/chat), not URLs an
                    // anonymous visitor could actually navigate to.
                    <code
                      key={s}
                      className="rounded border border-border bg-muted px-1.5 py-0.5 text-[11px] text-foreground-secondary"
                    >
                      {s}
                    </code>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        <footer className="mt-12 border-t border-border pt-6 text-[12px] text-muted-foreground">
          <a href={apiUrl('/api/docs/')} className="text-primary hover:underline">
            API reference
          </a>
        </footer>
      </div>
    </div>
  )
}
