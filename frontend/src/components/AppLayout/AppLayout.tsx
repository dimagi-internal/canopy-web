import { useEffect, useState, useRef } from 'react'
import { Outlet, Link, useLocation, useParams, useNavigate } from 'react-router-dom'
import { clsx } from 'clsx'
import { ChevronDown } from 'lucide-react'
import { PresenceBadge, pageKeyFor, usePresence } from 'canopy-ui/presence'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from 'canopy-ui/ui'
import { aiStatus, aiSwitch } from '@/api/ai'
import { useAuth } from '@/auth/AuthProvider'
import { useTheme } from '@/theme/ThemeProvider'
import { WorkspaceProvider, useWorkspace } from '@/workspace/WorkspaceProvider'
import { wsUrl } from '@/lib/wsUrl'
import { usePresenceReconnectNonce } from '@/presence/usePresenceReconnectNonce'
import { canopyPresenceRules } from '@/presence/routes'
import { isNavGroupActive, isNavItemActive, resolveNavGroups } from './nav'

const BACKENDS = [
  { key: 'api' as const, label: 'API', description: 'Direct Anthropic API' },
  { key: 'cli' as const, label: 'Claude CLI', description: 'Claude subscription via CLI' },
]

function UserMenu() {
  const auth = useAuth()
  const { theme, setTheme } = useTheme()
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<{
    backend: string; ready: boolean; detail: string; setup_hint: string | null
  } | null>(null)
  const [switching, setSwitching] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // AI backend status — polled until ready.
  useEffect(() => {
    aiStatus().then(setStatus).catch(() => {})
    const interval = setInterval(() => {
      aiStatus().then((s) => {
        setStatus(s)
        if (s.ready) clearInterval(interval)
      }).catch(() => {})
    }, 5000)
    return () => clearInterval(interval)
  }, [])

  // Close on outside click.
  useEffect(() => {
    if (!open) return
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open])

  if (auth.status !== 'authenticated') return null

  const csrfToken = (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)?.[1] ?? '')
  const initials = (auth.user.name || auth.user.email).slice(0, 1).toUpperCase()

  async function handleSwitch(backend: 'api' | 'cli') {
    if (status?.backend === backend || switching) return
    setSwitching(true)
    try {
      await aiSwitch(backend)
      setStatus(await aiStatus())
    } catch {
      // silent
    } finally {
      setSwitching(false)
    }
  }

  const segBtn = (active: boolean) =>
    clsx(
      'flex-1 rounded-md border px-2 py-1.5 text-xs capitalize transition-colors disabled:opacity-50',
      active
        ? 'border-primary/30 bg-primary/10 text-primary font-medium'
        : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground-secondary',
    )

  return (
    <div className="relative" ref={menuRef}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-label="Account menu"
        className="flex min-h-11 items-center gap-2 rounded-full border border-border bg-card py-1 pl-1 pr-3 hover:bg-muted sm:min-h-0"
      >
        {auth.user.avatar_url ? (
          <img src={auth.user.avatar_url} alt="" className="h-6 w-6 rounded-full" />
        ) : (
          <span className="h-6 w-6 rounded-full bg-primary text-white text-xs font-semibold flex items-center justify-center">
            {initials}
          </span>
        )}
        <span className="hidden sm:inline text-xs text-foreground-secondary max-w-[12rem] truncate">{auth.user.email}</span>
        {/* AI-not-ready indicator on the chip so it's discoverable without opening the menu. */}
        {status && !status.ready && <span className="h-1.5 w-1.5 rounded-full bg-warning" aria-label="AI not connected" />}
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-64 overflow-hidden rounded-lg border border-input bg-card shadow-lg z-50">
          <div className="border-b border-border px-3 py-2 text-xs text-muted-foreground">
            Signed in as
            <div className="truncate text-foreground-secondary">{auth.user.email}</div>
          </div>

          {/* AI backend */}
          <div className="border-b border-border px-3 py-2">
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">AI backend</div>
            {!status ? (
              <div className="text-xs text-muted-foreground">Checking…</div>
            ) : (
              <>
                <div className="flex gap-1">
                  {BACKENDS.map((b) => (
                    <button
                      key={b.key}
                      type="button"
                      disabled={switching}
                      onClick={() => void handleSwitch(b.key)}
                      title={b.description}
                      className={segBtn(status.backend === b.key)}
                    >
                      {b.label}
                    </button>
                  ))}
                </div>
                {!status.ready && (
                  <Link
                    to="/settings"
                    onClick={() => setOpen(false)}
                    className="mt-1.5 block text-[11px] text-warning hover:underline"
                  >
                    Not connected — set up →
                  </Link>
                )}
              </>
            )}
          </div>

          {/* Theme */}
          <div className="border-b border-border px-3 py-2">
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Theme</div>
            <div className="flex gap-1">
              {(['light', 'dark'] as const).map((t) => (
                <button key={t} type="button" onClick={() => setTheme(t)} className={segBtn(theme === t)}>
                  {t}
                </button>
              ))}
            </div>
          </div>

          <Link
            to="/settings"
            onClick={() => setOpen(false)}
            className="block px-3 py-2 text-sm text-foreground-secondary hover:bg-muted"
          >
            Settings
          </Link>

          <form method="post" action="/accounts/logout/" className="border-t border-border">
            <input type="hidden" name="csrfmiddlewaretoken" value={decodeURIComponent(csrfToken)} />
            <button
              type="submit"
              className="w-full px-3 py-2 text-left text-sm text-foreground-secondary hover:bg-muted"
            >
              Sign out
            </button>
          </form>
        </div>
      )}
    </div>
  )
}

// Workspace switcher and create affordance. The switcher appears only when
// there are multiple workspaces to choose from; create is always present.
function WorkspaceSwitcher() {
  const { workspaces, active } = useWorkspace()
  const navigate = useNavigate()
  // TWO independent decisions, which is the entire point of this component.
  // Whether to show a SWITCHER depends on having something to switch between.
  // Whether to show CREATE does not depend on anything. Conflating them is what
  // left a one-workspace user with no path to a second and a zero-workspace user
  // with no path at all — and the first fix of it left a five-workspace user
  // with no path to a sixth.
  return (
    <div className="flex items-center gap-2">
      {workspaces.length > 1 ? (
        <select
          aria-label="Workspace"
          className="min-h-11 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground sm:min-h-0"
          value={active ?? ''}
          onChange={(e) => navigate(`/w/${e.target.value}/agents`)}
        >
          {workspaces.map((w) => (
            <option key={w.slug} value={w.slug}>
              {w.display_name}
            </option>
          ))}
        </select>
      ) : null}
      <NewWorkspaceLink />
    </div>
  )
}

/** The one affordance that must never be conditional on how many workspaces
 *  you already have. Routes to the first-run screen, which owns the form. */
function NewWorkspaceLink() {
  return (
    <Link
      to="/new-workspace"
      className="text-xs text-muted-foreground hover:text-foreground"
    >
      + Workspace
    </Link>
  )
}

// One presence socket per tab, mounted here so it persists across
// navigation (usePresence re-keys on pathname change without reconnecting).
//
// Keyed by `reconnectNonce` from the parent: the PresenceConsumer only
// re-reads the user's visibility preference on `presence.enter` — i.e. on
// the NEXT navigation (see apps/realtime/presence_consumer.py). A user who
// just opted out via the Settings toggle would otherwise still show as
// visible on whatever page they're already on, until they happen to
// navigate somewhere. Bumping `reconnectNonce` changes this component's
// `key`, which makes React tear down the old instance (closing its socket
// in the `usePresence` cleanup) and mount a fresh one — a full reconnect
// that sends a brand new `presence.enter` under the just-saved preference.
// Simplest correct fix that needs no new imperative API on the shared
// `usePresence` hook from canopy-ui.
function PresenceHeaderBadge() {
  const { pathname } = useLocation()
  const location = pageKeyFor('canopy', pathname, canopyPresenceRules)
  const { viewers } = usePresence({ url: wsUrl('ws/presence/'), location })
  return <PresenceBadge viewers={viewers} />
}

export function AppLayout() {
  const { workspace } = useParams()
  return (
    <WorkspaceProvider urlSlug={workspace ?? null}>
      <AppShell />
    </WorkspaceProvider>
  )
}

function AppShell() {
  const location = useLocation()
  const auth = useAuth()
  const { active } = useWorkspace()
  const [mobileOpen, setMobileOpen] = useState(false)
  // See `PresenceHeaderBadge` above for why bumping this forces a full
  // socket reconnect rather than something lighter.
  const presenceReconnectNonce = usePresenceReconnectNonce()

  // Anonymous visitors only ever reach the app shell on a public link route
  // (/walkthrough/:id, /review/:id — see AuthProvider). Every nav destination
  // is behind the login gate, so the whole nav (inline + mobile hamburger)
  // would be dead links that bounce to sign-in. Show just the wordmark instead.
  const isAuthed = auth.status === 'authenticated'

  // See `nav.ts` for the grouping and why the header is menus rather than a
  // flat row of links.
  const navGroups = resolveNavGroups({ isAuthed, active })

  // Collapse the mobile menu whenever the route changes (e.g. tapping a link).
  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  function navLinkClass(path: string, block: boolean) {
    return clsx(
      'text-sm font-medium rounded transition-colors',
      block ? 'block px-3 py-2' : 'px-3 py-1.5',
      isNavItemActive(path, location.pathname)
        ? 'text-foreground bg-card'
        : 'text-muted-foreground hover:text-foreground-secondary hover:bg-card/50',
    )
  }

  return (
    <div className="min-h-screen bg-background text-foreground-secondary">
      <header className="border-b border-border bg-background relative">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 py-3 flex items-center justify-between gap-3">
          {isAuthed ? (
            <Link to="/" className="flex min-h-11 shrink-0 items-center text-lg font-semibold text-foreground sm:min-h-0">Canopy<span className="text-primary">.</span></Link>
          ) : (
            <span className="text-lg font-semibold text-foreground shrink-0">Canopy<span className="text-primary">.</span></span>
          )}
          <div className="flex min-w-0 items-center gap-2 md:gap-3">
            {/* Four menu triggers, so the inline nav fits from `md` up. It was
                15 flat links needing `xl` plus an `overflow-x-auto` — which put
                a horizontal scrollbar on every page at 1280px (Tailwind's xl,
                and Desktop Chrome's default width) and showed no nav at all
                below it. Grouping is what makes the room; see `nav.ts`. */}
            <nav className="hidden md:flex gap-0.5" aria-label="Main">
              {navGroups.map((group) => {
                const groupActive = isNavGroupActive(group, location.pathname)
                return (
                  <DropdownMenu key={group.label}>
                    <DropdownMenuTrigger
                      className={clsx(
                        'flex shrink-0 items-center gap-1 rounded px-3 py-1.5 text-sm font-medium transition-colors',
                        groupActive
                          ? 'text-foreground bg-card'
                          : 'text-muted-foreground hover:text-foreground-secondary hover:bg-card/50',
                      )}
                    >
                      {group.label}
                      <ChevronDown className="h-3.5 w-3.5 opacity-60" aria-hidden="true" />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start" className="min-w-44">
                      {group.items.map((item) => (
                        <DropdownMenuItem
                          key={item.href}
                          className={clsx(
                            'cursor-pointer',
                            isNavItemActive(item.href, location.pathname) &&
                              'text-foreground font-medium',
                          )}
                          render={<Link to={item.href} />}
                        >
                          {item.label}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuContent>
                  </DropdownMenu>
                )
              })}
            </nav>
            <WorkspaceSwitcher />
            {isAuthed && <PresenceHeaderBadge key={presenceReconnectNonce} />}
            <UserMenu />
            {isAuthed && (
            <button
              type="button"
              onClick={() => setMobileOpen((o) => !o)}
              aria-label="Toggle navigation menu"
              aria-expanded={mobileOpen}
              className="-mr-1 flex min-h-11 min-w-11 items-center justify-center rounded text-foreground-secondary hover:bg-card hover:text-foreground-secondary md:hidden"
            >
              <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                {mobileOpen ? (
                  <>
                    <line x1="6" y1="6" x2="18" y2="18" />
                    <line x1="6" y1="18" x2="18" y2="6" />
                  </>
                ) : (
                  <>
                    <line x1="3" y1="6" x2="21" y2="6" />
                    <line x1="3" y1="12" x2="21" y2="12" />
                    <line x1="3" y1="18" x2="21" y2="18" />
                  </>
                )}
              </svg>
            </button>
            )}
          </div>
        </div>
        {mobileOpen && (
          <>
            {/* Backdrop: tap anywhere outside the panel to dismiss. */}
            <button
              type="button"
              aria-hidden="true"
              tabIndex={-1}
              onClick={() => setMobileOpen(false)}
              className="md:hidden fixed inset-0 top-[53px] z-30 bg-background/40 cursor-default"
            />
            {/* One flat scrolling list under section headers — deliberately
                not an accordion. The desktop grouping survives as labels, but
                every destination stays one tap away, which is what matters on
                a phone. */}
            <nav
              aria-label="Main"
              className="md:hidden absolute left-0 right-0 top-full z-40 border-b border-border bg-background px-3 py-2 shadow-lg flex flex-col gap-1 max-h-[calc(100vh-53px)] overflow-y-auto"
            >
              {navGroups.map((group) => (
                <div key={group.label} className="flex flex-col gap-0.5 pb-1">
                  <div className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {group.label}
                  </div>
                  {group.items.map((item) => (
                    <Link key={item.href} to={item.href} className={navLinkClass(item.href, true)}>
                      {item.label}
                    </Link>
                  ))}
                </div>
              ))}
            </nav>
          </>
        )}
      </header>
      {location.pathname.startsWith('/ddd') ||
      location.pathname.startsWith('/review') ||
      location.pathname.startsWith('/timeline') ||
      // An individual Agent Workspace (/agents/<slug>) is a full-bleed workbench
      // like DDD; the bare /agents LIST stays in the standard container.
      /^\/agents\/[^/]+/.test(location.pathname) ||
      // The standalone live chat (/w/:ws/chat/:id) is a full-height surface —
      // the composer must sit at the viewport bottom with the list scrolling
      // above it, not flow inside the padded max-w container.
      /\/chat\/[^/]+/.test(location.pathname) ? (
        // DDD (and the narrative editor at /review) is a full-bleed workspace:
        // persistent left rail + wide main. The page owns its own scroll.
        <main className="h-[calc(100vh-53px)]"><Outlet /></main>
      ) : (
        // `px-6` unconditionally, on top of the `p-6` several pages set for
        // themselves, spent 96px of a 375px screen on padding — a quarter of the
        // width, before any content. The gutter now scales with the viewport;
        // the header above already did this.
        <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-8"><Outlet /></main>
      )}
    </div>
  )
}
