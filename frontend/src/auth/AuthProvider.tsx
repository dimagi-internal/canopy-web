import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { bootstrapCsrf } from '@/api/csrf'
import { isLoginBounceInFlight, noteAuthSucceeded } from '@/api/client.v2'
import { getMe, type MeOut as MeResponse } from '@/api/me'

type AuthState =
  | { status: 'loading'; user: null }
  | { status: 'authenticated'; user: MeResponse }
  | { status: 'anonymous'; user: null }

/** Exported so components that render on PUBLIC routes — where the user may or
 *  may not be signed in — can be tested against either state without standing
 *  up the provider's /api/me/ fetch. */
export const AuthContext = createContext<AuthState>({ status: 'loading', user: null })

// Routes reachable without a Dimagi session: public (visibility=link) walkthroughs
// and reviews. These are tokenless — the UUID in the URL is the only secret, and
// the API self-enforces (private resources 404 to anonymous callers).
// Legacy /w/<uuid> walkthrough links pass too (the router redirects them to
// /walkthrough/<uuid>), but /w/<workspace> tenant paths stay behind the gate.
// /invite/<token> is the odd one out: the invitee has no Dimagi session (may
// not even be a Dimagi address), so the accept page must render for an
// anonymous visitor — it self-enforces via the token-gated preview/accept
// endpoints, same shape as the others.
const LEGACY_WALKTHROUGH_RE =
  /^\/w\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\/|$)/i
function isPublicLinkRoute(): boolean {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '')
  const path = window.location.pathname.slice(base.length)
  return (
    path.startsWith('/review/') ||
    path.startsWith('/walkthrough/') ||
    path.startsWith('/share/') ||
    path.startsWith('/ddd-release/') ||
    path.startsWith('/storyboard/') ||
    path.startsWith('/narrative/') ||
    path.startsWith('/invite/') ||
    LEGACY_WALKTHROUGH_RE.test(path)
  )
}

export function useAuth(): AuthState {
  return useContext(AuthContext)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading', user: null })

  useEffect(() => {
    let cancelled = false
    async function boot() {
      try {
        await bootstrapCsrf()
        const me = await getMe()
        if (cancelled) return
        if (me) {
          // Reset the login-loop guard: this session took, so a genuine later
          // expiry gets its own clean single bounce (see client.v2 redirectToLogin).
          noteAuthSucceeded()
          setState({ status: 'authenticated', user: me })
        } else setState({ status: 'anonymous', user: null })
      } catch {
        if (!cancelled) setState({ status: 'anonymous', user: null })
      }
    }
    void boot()
    return () => {
      cancelled = true
    }
  }, [])

  if (state.status === 'loading') {
    return (
      <div className="min-h-screen bg-background text-muted-foreground flex items-center justify-center text-sm">
        Loading…
      </div>
    )
  }

  if (state.status === 'anonymous' && !isPublicLinkRoute()) {
    // A 401 on /api/me has already committed us to a full-page bounce to Google
    // (the onResponse middleware runs before the fetch promise resolves, so this
    // is settled by the time we render). Painting the login card here would show
    // a Sign-in button for the few hundred ms before the browser leaves — the
    // flicker from our button to Google's. Show continuity instead; the card is
    // still what renders when the loop guard declines to bounce, which is the
    // case where the user genuinely does need to drive it by hand.
    if (isLoginBounceInFlight()) {
      return (
        <div className="min-h-screen bg-background text-muted-foreground flex items-center justify-center text-sm">
          Signing in…
        </div>
      )
    }
    return <LoginPrompt />
  }

  // Authenticated, OR anonymous on a public link route (e.g. /walkthrough/<id> or /review/<id>).
  // Public resources are tokenless; the API self-enforces (private → 404), so these
  // pages must render without a Dimagi session.
  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>
}

function LoginPrompt() {
  const next = encodeURIComponent(window.location.pathname + window.location.search)
  const loginHref = `${import.meta.env.BASE_URL.replace(/\/$/, '')}/accounts/google/login/?next=${next}`
  return (
    <div className="min-h-screen bg-background text-foreground-secondary flex items-center justify-center px-6">
      <div className="max-w-md w-full bg-card border border-border rounded-xl p-8 text-center">
        <h1 className="text-2xl font-semibold text-foreground mb-2">
          Canopy<span className="text-primary">.</span>
        </h1>
        <p className="text-sm text-foreground-secondary mb-6">
          Sign in with your Dimagi Google account to continue.
        </p>
        <a
          href={loginHref}
          className="inline-block w-full rounded-lg bg-primary text-white font-medium py-2.5 hover:bg-primary/90 transition-colors"
        >
          Sign in with Google
        </a>
      </div>
    </div>
  )
}
