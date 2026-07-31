import { useEffect, useState, useRef } from 'react'
import { Outlet, Link, useLocation, useParams, useNavigate } from 'react-router-dom'
import { clsx } from 'clsx'
import { PresenceBadge, pageKeyFor, usePresence } from 'canopy-ui/presence'
import { aiStatus, aiSwitch } from '@/api/ai'
import { useAuth } from '@/auth/AuthProvider'
import { useTheme } from '@/theme/ThemeProvider'
import { WorkspaceProvider, useWorkspace } from '@/workspace/WorkspaceProvider'
import { wsUrl } from '@/lib/wsUrl'
import { usePresenceReconnectNonce } from '@/presence/usePresenceReconnectNonce'
import { canopyPresenceRules } from '@/presence/routes'

// `tenant` items live under /w/:workspace; the rest are personal/global (root).
const NAV_ITEMS = [
  { path: '', label: 'Projects', tenant: true },
  { path: 'chat', label: 'Chats', tenant: true },
  { path: 'ddd', label: 'DDD', tenant: true },
  { path: 'agents', label: 'Agents', tenant: true },
  { path: 'walkthroughs', label: 'Walkthroughs', tenant: true },
  { path: 'shareouts', label: 'Shareouts', tenant: true },
  { path: 'timeline', label: 'Timeline', tenant: true },
  { path: 'members', label: 'Members', tenant: true },
  { path: 'inbound', label: 'Inbound', tenant: true },
  { path: '/insights', label: 'Insights', tenant: false },
  { path: '/sessions', label: 'Sessions', tenant: false },
  { path: '/system', label: 'System', tenant: false },
  { path: '/supervisor', label: 'Supervisor', tenant: false },
  { path: '/schedules', label: 'Schedule', tenant: false },
  { path: '/activity', label: 'Activity', tenant: false },
]

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
        className="flex items-center gap-2 rounded-full border border-border bg-card pl-1 pr-3 py-1 hover:bg-muted"
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

// Tenant switcher — navigates between the caller's workspaces by rewriting the
// :workspace URL segment. Hidden when there's nothing to switch to (the common
// single-tenant case), so today's UI is unchanged.
function WorkspaceSwitcher() {
  const { workspaces, active } = useWorkspace()
  const navigate = useNavigate()
  if (workspaces.length <= 1) return null
  return (
    <select
      aria-label="Workspace"
      className="bg-input border border-input text-foreground text-[13px] rounded px-2 py-1"
      value={active ?? ''}
      onChange={(e) => navigate(`/w/${e.target.value}/agents`)}
    >
      {workspaces.map((w) => (
        <option key={w.slug} value={w.slug}>
          {w.display_name}
        </option>
      ))}
    </select>
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

  // Tenant nav items resolve to /w/:active/<path>; personal/global items keep
  // their absolute path. Tenant items are hidden until the active workspace is
  // known (avoids linking to a broken /w//… path on first paint).
  const navItems = isAuthed
    ? NAV_ITEMS.flatMap((item) => {
        if (!item.tenant) return [{ path: item.path, label: item.label }]
        if (!active) return []
        return [{ path: `/w/${active}${item.path ? `/${item.path}` : ''}`, label: item.label }]
      })
    : []

  // Collapse the mobile menu whenever the route changes (e.g. tapping a link).
  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  function navLinkClass(path: string, block: boolean) {
    // The workspace index (/w/<slug>) is a prefix of every tenant route, so it
    // must match exactly (else "Projects" highlights on every tenant page).
    const isIndex = path === '/' || /^\/w\/[^/]+$/.test(path)
    const isActive =
      location.pathname === path ||
      (!isIndex && location.pathname.startsWith(path + '/'))
    return clsx(
      'text-sm font-medium rounded transition-colors',
      block ? 'block px-3 py-2' : 'px-3 py-1.5',
      isActive
        ? 'text-foreground bg-card'
        : 'text-muted-foreground hover:text-foreground-secondary hover:bg-card/50',
    )
  }

  return (
    <div className="min-h-screen bg-background text-foreground-secondary">
      <header className="border-b border-border bg-background relative">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 py-3 flex items-center justify-between gap-3">
          {isAuthed ? (
            <Link to="/" className="text-lg font-semibold text-foreground shrink-0">Canopy<span className="text-primary">.</span></Link>
          ) : (
            <span className="text-lg font-semibold text-foreground shrink-0">Canopy<span className="text-primary">.</span></span>
          )}
          <div className="flex min-w-0 items-center gap-2 xl:gap-3">
            {/* Full inline nav only once all items fit (~xl); below that it
                overflows the viewport, so we collapse it into the menu below. */}
            <nav className="hidden min-w-0 xl:flex gap-0.5">
              {navItems.map((item) => (
                <Link key={item.path} to={item.path} className={navLinkClass(item.path, false)}>
                  {item.label}
                </Link>
              ))}
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
              className="xl:hidden -mr-1 p-2 rounded text-foreground-secondary hover:text-foreground-secondary hover:bg-card"
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
              className="xl:hidden fixed inset-0 top-[53px] z-30 bg-background/40 cursor-default"
            />
            <nav className="xl:hidden absolute left-0 right-0 top-full z-40 border-b border-border bg-background px-3 py-2 shadow-lg flex flex-col gap-1 max-h-[calc(100vh-53px)] overflow-y-auto">
              {navItems.map((item) => (
                <Link key={item.path} to={item.path} className={navLinkClass(item.path, true)}>
                  {item.label}
                </Link>
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
        <main className="mx-auto max-w-7xl px-6 py-8"><Outlet /></main>
      )}
    </div>
  )
}
