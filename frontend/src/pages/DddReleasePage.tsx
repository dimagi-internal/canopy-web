import { useEffect, useMemo, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { getRelease, type DddRunRelease, type DddLink } from '@/api/ddd'
import { withBase } from '@/lib/basePath'

/**
 * The clean, shareable DDD run RELEASE page — the outsider-legible face of a
 * run: title + final video + the narrative story + the live product URLs it
 * used. Deliberately release-only: no phase/gate jargon, no artifact dump, no
 * edit affordances (those live on the operator console at /w/:ws/ddd/...). Runs
 * OUTSIDE the app shell (PublicLayout) so a `?t=<share_token>` viewer with no
 * Dimagi login is served; members additionally see an "open the build view" link.
 */

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: DddRunRelease }
  | { kind: 'not_found' }
  | { kind: 'error'; message: string }

export default function DddReleasePage() {
  const { runId } = useParams()
  const [params] = useSearchParams()
  const token = params.get('t')
  const [state, setState] = useState<LoadState>({ kind: 'loading' })

  useEffect(() => {
    let live = true
    if (!runId) {
      setState({ kind: 'not_found' })
      return
    }
    setState({ kind: 'loading' })
    getRelease(runId, token)
      .then((data) => live && setState({ kind: 'ready', data }))
      .catch((e: Error) => {
        if (!live) return
        const msg = e.message || ''
        setState(
          /not found|404/i.test(msg)
            ? { kind: 'not_found' }
            : { kind: 'error', message: msg },
        )
      })
    return () => {
      live = false
    }
  }, [runId, token])

  if (state.kind === 'loading') return <Centered>Loading…</Centered>
  if (state.kind === 'not_found')
    return (
      <Centered>
        <p className="text-foreground">This demo isn’t available.</p>
        <p className="mt-1 text-sm text-muted-foreground">
          The link may be private or the run may have been removed.
        </p>
      </Centered>
    )
  if (state.kind === 'error')
    return (
      <Centered>
        <p className="text-foreground">Couldn’t load this demo.</p>
        <p className="mt-1 text-sm text-muted-foreground">{state.message}</p>
      </Centered>
    )

  return <Release data={state.data} />
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto flex min-h-screen max-w-3xl items-center justify-center px-6 text-center">
        <div>{children}</div>
      </div>
    </div>
  )
}

function Release({ data }: { data: DddRunRelease }) {
  const shareUrl = useShareUrl(data)
  const products = data.product_links
  // Only tag scenes with a persona when the walkthrough actually has more than
  // one — a single-persona demo repeats the same name on every card for no signal.
  const showPersona =
    new Set((data.narrative?.narration ?? []).map((n) => n.persona).filter(Boolean)).size > 1

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-3xl px-6 py-12 md:py-16">
        {/* Top bar: quiet wordmark + (members, when public) a copy-share affordance */}
        <div className="mb-10 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
            Canopy · Demo
          </span>
          {data.is_member && shareUrl && <CopyShare url={shareUrl} />}
        </div>

        {/* Hero */}
        <header className="mb-10">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-primary">
            Demo walkthrough
          </p>
          <h1 className="text-4xl font-semibold leading-tight tracking-tight text-foreground md:text-5xl">
            {data.title || data.narrative_slug}
          </h1>
          {data.lede && (
            <p className="mt-4 text-lg leading-relaxed text-foreground-secondary">{data.lede}</p>
          )}
        </header>

        {/* Final video */}
        {data.video && (
          <div className="mb-12 overflow-hidden rounded-2xl border border-border bg-black shadow-sm">
            <video
              controls
              preload="metadata"
              className="w-full"
              src={withBase(data.video.content_url)}
            >
              <a href={withBase(data.video.viewer_url)}>Watch the demo video</a>
            </video>
          </div>
        )}

        {/* The narration — the per-scene voiceover, opening on the overview
            scene. There is intentionally NO separate top-level "story": the
            overview scene serves that role and is what's actually in the video. */}
        {data.narrative && data.narrative.narration.length > 0 && (
          <Section title="The walkthrough" subtitle="what the video narrates, scene by scene">
            <ol className="flex flex-col gap-2">
              {data.narrative.narration.map((n, i) => {
                const persona = n.persona ? data.narrative?.personas?.[n.persona] : undefined
                return (
                  <li
                    key={n.id ?? i}
                    className="rounded-lg border border-border bg-card px-4 py-3"
                  >
                    <div className="mb-1 flex items-center gap-2">
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {n.scene ?? i + 1}
                      </span>
                      {n.title && (
                        <span className="text-[13px] font-medium text-foreground">{n.title}</span>
                      )}
                      {showPersona && persona?.name && (
                        <span className="text-[11px] text-primary" title={persona.role || ''}>
                          {persona.name}
                        </span>
                      )}
                    </div>
                    <p className="text-[13px] leading-relaxed text-foreground-secondary">
                      {n.text}
                    </p>
                  </li>
                )
              })}
            </ol>
          </Section>
        )}

        {/* Try it live — the actual product URLs the demo used */}
        {products.length > 0 && (
          <Section title="Try it live" subtitle="the real pages this demo used">
            <div className="flex flex-col gap-2">
              {products.map((l) => (
                <LinkButton key={l.url} link={l} />
              ))}
            </div>
          </Section>
        )}



        {/* Footer */}
        <footer className="mt-16 flex items-center justify-between border-t border-border pt-6 text-[11px] text-muted-foreground">
          <span>
            Generated by canopy · run <span className="font-mono">{data.run_id}</span>
          </span>
          {data.is_member && data.build_url && (
            <a
              href={withBase(data.build_url)}
              className="text-muted-foreground transition-colors hover:text-primary"
            >
              Open the full DDD view →
            </a>
          )}
        </footer>
      </div>
    </div>
  )
}

function Section({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children: React.ReactNode
}) {
  return (
    <section className="mb-12">
      <div className="mb-4 flex items-baseline gap-2 border-b border-border pb-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
          {title}
        </h2>
        {subtitle && <span className="text-[11px] text-muted-foreground">{subtitle}</span>}
      </div>
      {children}
    </section>
  )
}

const KIND_ICON: Record<DddLink['kind'], string> = {
  reference: '↗',
  narrative: '📖',
  companion: '🎞️',
}

/** A named, verb-labeled destination button — the product URL, not a raw href. */
function LinkButton({ link }: { link: DddLink }) {
  return (
    <a
      href={link.url}
      target="_blank"
      rel="noopener noreferrer"
      className="group flex items-center gap-3 rounded-lg border border-border bg-card px-4 py-2.5 transition-colors hover:border-input hover:bg-muted/40"
    >
      <span className="min-w-0 flex-1">
        <span className="block text-[13px] font-medium text-foreground group-hover:text-primary">
          {link.label || link.url}
        </span>
        <span className="block truncate font-mono text-[11px] text-muted-foreground">
          {link.url}
        </span>
      </span>
      <span
        aria-hidden
        className="shrink-0 text-[13px] text-muted-foreground transition-colors group-hover:text-primary"
      >
        {KIND_ICON[link.kind] ?? '↗'}
      </span>
    </a>
  )
}

function CopyShare({ url }: { url: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(url).then(
          () => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          },
          () => {},
        )
      }}
      className="rounded-md border border-border px-3 py-1 text-[11px] text-foreground-secondary transition-colors hover:border-input hover:text-primary"
      title="Copy the public share link"
    >
      {copied ? 'Copied ✓' : 'Copy share link'}
    </button>
  )
}

/** The absolute, tokened share URL for this release (only when public). */
function useShareUrl(data: DddRunRelease): string | null {
  return useMemo(() => {
    if (!data.is_public || !data.share_token) return null
    const base = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
    const path = `/ddd-release/${data.narrative_slug}/${data.run_id}`
    return `${window.location.origin}${base}${path}?t=${data.share_token}`
  }, [data.is_public, data.share_token, data.narrative_slug, data.run_id])
}
