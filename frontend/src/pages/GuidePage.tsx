import { useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { guideGroups } from '@/guide/grouping'
import { USER_ROLES } from '@/guide/paths'

/**
 * The in-app guide: what every surface is, generated from guide/surfaces.ts and
 * grouped by the header nav. A coverage test guarantees nothing is missing;
 * empty states deep-link here with #<route-path> anchors, so the stuck-user
 * surface and the documentation surface are the same content.
 */
export function GuidePage() {
  const { hash } = useLocation()

  // React Router navigates with pushState, which does NOT trigger the browser's
  // native fragment scrolling — so a /guide#<route-path> link from an empty state
  // would land at the top of the page and silently do nothing. Anchor ids are
  // route paths ('/w/:workspace/agents'), so they MUST be looked up with
  // getElementById: '#/w/:workspace/agents' is not a valid CSS selector and
  // querySelector would throw.
  useEffect(() => {
    if (!hash) return
    const el = document.getElementById(decodeURIComponent(hash.slice(1)))
    el?.scrollIntoView({ block: 'start' })
  }, [hash])
  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <h1 className="text-lg font-semibold text-foreground">Guide</h1>
      <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
        What each part of Canopy is for. Generated from the app&apos;s own route table, so
        it cannot quietly miss a page.
      </p>

      <section className="mt-8">
        <h2 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Four roles — find yours
        </h2>
        <ul className="mt-3 space-y-3">
          {USER_ROLES.map((p) => (
            <li key={p.id} className="rounded-lg border border-border bg-card p-3">
              <h3 className="text-sm font-semibold text-foreground">{p.title}</h3>
              <p className="mt-0.5 text-[12px] text-muted-foreground">{p.who}</p>
              <p className="mt-1 text-[11px] text-warning">Enforced by: {p.enforcement}</p>
              <p className="mt-1.5 text-[13px] text-foreground-secondary">{p.startHere}</p>
              {p.note ? <p className="mt-1 text-[12px] text-foreground-subtle">{p.note}</p> : null}
              <div className="mt-2 flex flex-wrap gap-2">
                {p.surfaces.map((s) => (
                  <Link
                    key={s}
                    to={'#' + s}
                    className="rounded border border-border bg-muted px-1.5 py-0.5 text-[11px] text-foreground-secondary hover:text-foreground"
                  >
                    {s}
                  </Link>
                ))}
              </div>
            </li>
          ))}
        </ul>
      </section>

      {guideGroups().map((group) => (
        <section key={group.label} className="mt-8">
          <h2 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            {group.label}
          </h2>
          <div className="mt-3 space-y-3">
            {group.surfaces.map((s) => (
              <article
                key={s.path}
                id={s.path}
                className="scroll-mt-20 rounded-lg border border-border bg-card p-4"
              >
                <div className="flex items-baseline justify-between gap-3">
                  <h3 className="text-sm font-semibold text-foreground">{s.title}</h3>
                  <code className="shrink-0 text-[11px] text-foreground-subtle">{s.path}</code>
                </div>
                <p className="mt-1 text-[12px] text-muted-foreground">{s.audience}</p>
                <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">{s.what}</p>
                {s.needsFirst ? (
                  <p className="mt-2 text-[12px] text-warning">Needs first: {s.needsFirst}</p>
                ) : null}
                {s.actions?.length ? (
                  <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[12px] text-foreground-secondary">
                    {s.actions.map((a) => (
                      <li key={a}>{a}</li>
                    ))}
                  </ul>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
