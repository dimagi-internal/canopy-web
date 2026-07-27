import { Link } from 'react-router-dom'
import { useAuth } from '@/auth/AuthProvider'
import { ThemeToggle } from '@/theme/ThemeProvider'

/**
 * The Canopy bar on public pages.
 *
 * Deliberately NOT `AppLayout`'s header. That one mounts `WorkspaceProvider`,
 * a workspace switcher and a user menu that polls the AI backend — all authed
 * calls, and an anonymous reviewer following a share link would be bounced to a
 * Google login by the first one. Being chrome-less was that hazard's blunt
 * answer; this is the precise one: the same wordmark and the same tokens, with
 * nothing behind it that a stranger cannot call.
 *
 * The wordmark links home only when there IS a home to go to — an anonymous
 * visitor clicking it would land on the login wall, which is a worse answer
 * than not offering the link.
 */
export function PublicHeader({ trail }: { trail?: React.ReactNode }) {
  const auth = useAuth()
  const isAuthed = auth.status === 'authenticated'

  return (
    <header className="border-b border-border bg-background">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <div className="flex min-w-0 items-baseline gap-3">
          {isAuthed ? (
            <Link to="/" className="shrink-0 text-lg font-semibold text-foreground">
              Canopy<span className="text-primary">.</span>
            </Link>
          ) : (
            <span className="shrink-0 text-lg font-semibold text-foreground">
              Canopy<span className="text-primary">.</span>
            </span>
          )}
          {trail && (
            <span className="truncate text-[12.5px] text-muted-foreground">{trail}</span>
          )}
        </div>
        <ThemeToggle />
      </div>
    </header>
  )
}
