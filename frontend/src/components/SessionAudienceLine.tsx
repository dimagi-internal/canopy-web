/**
 * "Who can see this" — one line, from the session's `audience`
 * (apps/canopy_sessions/audience.py, the same pieces as the session ACL).
 * Shown at the top of the chat page and in the embedded panel. With `href` (a
 * Slack thread's `slack://` link) the label opens that conversation in Slack.
 */
export type SessionAudience = {
  label: string
  extras?: ReadonlyArray<string>
  count: number
}

export function SessionAudienceLine({ audience, href, className = '' }: {
  audience: SessionAudience | null | undefined
  href?: string
  className?: string
}) {
  if (!audience?.label) return null
  const extras = audience.extras ?? []
  const title = `${audience.count} ${audience.count === 1 ? 'person' : 'people'} can read this`
  return (
    <p
      className={`truncate text-[11px] text-muted-foreground ${className}`}
      data-testid="session-audience"
      title={title}
    >
      <span className="font-medium">Who can see this:</span>{' '}
      {href ? (
        <a href={href} className="hover:text-foreground hover:underline" data-testid="session-audience-link"
          title="Open in Slack">{audience.label}</a>
      ) : audience.label}
      {extras.length > 0 && <span> · {extras.join(' · ')}</span>}
    </p>
  )
}
