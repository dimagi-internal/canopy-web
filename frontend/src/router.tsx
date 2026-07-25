import { lazy, Suspense } from 'react'
import type { RouteObject } from 'react-router-dom'
import { createBrowserRouter, Navigate, useLocation, useParams } from 'react-router-dom'
import { useWorkspace } from './workspace/WorkspaceProvider'
import { AppLayout } from './components/AppLayout/AppLayout'
import { RouteErrorBoundary } from './components/RouteErrorBoundary'
import { NotFound } from './components/NotFound'
import { ShareRouteErrorBoundary } from './components/ShareRouteErrorBoundary'
import { ProjectsPage } from './pages/ProjectsPage'
import { InsightsPage } from './pages/InsightsPage'
import { ShareoutsPage } from './pages/ShareoutsPage'
import { SettingsPage } from './pages/SettingsPage'
import { WalkthroughsPage } from './pages/WalkthroughsPage'
import { WalkthroughViewerPage } from './pages/WalkthroughViewerPage'
import { ReviewPage } from './pages/ReviewPage'
import { InviteAcceptPage } from './pages/InviteAcceptPage'
import { WorkspaceMembersPage } from './pages/WorkspaceMembersPage'
import { DddPage } from './pages/DddPage'
import { TimelinePage } from './pages/TimelinePage'
import { SystemPage } from './pages/SystemPage'
import { SessionsPage } from './pages/SessionsPage'
import { AgentsPage } from './pages/AgentsPage'
import { AgentWorkspacePage } from './pages/AgentWorkspacePage'
import SessionSharePage from './pages/SessionSharePage'
import DddReleasePage from './pages/DddReleasePage'
import { PublicLayout } from './components/PublicLayout'
import SupervisorPage from '@/pages/SupervisorPage'
import ActivityPage from '@/pages/ActivityPage'
import SchedulesPage from './pages/SchedulesPage'

// Agent Workspace sections are lazy-loaded — each owns its data fetch and only
// the active section's bundle is pulled in.
const InboxSection = lazy(() =>
  import('./pages/agents/InboxSection').then((m) => ({ default: m.InboxSection })),
)
const AgentOverviewSection = lazy(() =>
  import('./pages/agents/AgentOverviewSection').then((m) => ({ default: m.AgentOverviewSection })),
)
const AgentTasksSection = lazy(() =>
  import('./pages/agents/AgentTasksSection').then((m) => ({ default: m.AgentTasksSection })),
)
const AgentTurnsSection = lazy(() =>
  import('./pages/agents/AgentTurnsSection').then((m) => ({ default: m.AgentTurnsSection })),
)
const ItemsSection = lazy(() =>
  import('./pages/agents/ItemsSection').then((m) => ({ default: m.ItemsSection })),
)
const SchedulesSection = lazy(() =>
  import('./pages/agents/SchedulesSection').then((m) => ({ default: m.SchedulesSection })),
)
const AgentSyncsSection = lazy(() =>
  import('./pages/agents/AgentSyncsSection').then((m) => ({ default: m.AgentSyncsSection })),
)
const AgentWorkProductsSection = lazy(() =>
  import('./pages/agents/AgentWorkProductsSection').then((m) => ({ default: m.AgentWorkProductsSection })),
)
const AgentSkillsSection = lazy(() =>
  import('./pages/agents/AgentSkillsSection').then((m) => ({ default: m.AgentSkillsSection })),
)

// The standalone live-chat route is lazy — it pulls in the canopy-ui/chat kit
// (WebSocket hook + presentational tree) and react-markdown only when opened.
const ChatPage = lazy(() =>
  import('./pages/ChatPage').then((m) => ({ default: m.ChatPage })),
)
const ChatListPage = lazy(() =>
  import('./pages/ChatListPage').then((m) => ({ default: m.ChatListPage })),
)

// Force a full remount of ChatPage per session id. Without this, navigating
// chat/A -> chat/B reuses the same component instance (react-router only
// re-renders on a param change) — its useState cells (scroll-back cursor,
// oldestTurn, etc.) and the WS socket from useSessionSocket all carry over,
// so an in-flight loadEarlier/loadFull response from session A can splice
// into session B's transcript. Keying on `id` makes React tear down and
// rebuild the whole subtree (state + attach/detach + WS effects all
// re-fire cleanly) on every session switch.
function ChatPageRoute() {
  const { id } = useParams()
  return <ChatPage key={id} />
}

// Each lazy section renders inside the workspace shell's scrolling <main>; this
// keeps a minimal fallback in that same content area while the chunk loads.
function LazySection({ children }: { children: React.ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="max-w-4xl px-6 py-8 text-[13px] text-muted-foreground">Loading…</div>
      }
    >
      {children}
    </Suspense>
  )
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// Legacy flat tenant surface (e.g. /agents, /ddd/foo) → the active workspace's
// scoped path. Waits for the workspace list so `active` is known.
function TenantRedirect({ to }: { to: string }) {
  const { active, loading } = useWorkspace()
  const { '*': tail } = useParams()
  if (loading) return null
  if (!active) return null // no membership yet; nothing to route to
  const suffix = tail ? `/${tail}` : ''
  return <Navigate to={`/w/${active}/${to}${suffix}`} replace />
}

// Bare "/" → the active workspace's workbench.
function RootRedirect() {
  const { active, loading } = useWorkspace()
  if (loading) return null
  if (!active) return null
  return <Navigate to={`/w/${active}`} replace />
}

// /w/:workspace index. Disambiguates a legacy /w/<uuid> walkthrough link
// (redirect to the new viewer) from a real workspace slug (render the workbench).
function WorkspaceIndex() {
  const { workspace } = useParams()
  const { search, hash } = useLocation()
  if (workspace && UUID_RE.test(workspace)) {
    // Preserve ?t=<share_token> and #t=<seconds> across the redirect.
    return <Navigate to={`/walkthrough/${workspace}${search}${hash}`} replace />
  }
  return <ProjectsPage />
}

/**
 * Hang an error boundary off every route, at every depth.
 *
 * A render throw anywhere used to take the whole app down: `LazySection` is
 * Suspense-only (pending states, not throws) and nothing else caught. React
 * Router's data router walks UP from the throwing route to the nearest
 * `errorElement`, so a boundary on every route means the throw is always
 * contained to the smallest surface that can be swapped out — a rail section
 * fails inside the agent workspace's <Outlet/> with the rail still navigable; a
 * page fails inside AppLayout's <main> with the header and nav still there. The
 * boundary on the layout route itself is the last resort (AppLayout's own
 * throws), NOT the design — a single root boundary catches identically and
 * blanks everything, which is the bug, not the fix.
 *
 * Applied here rather than spelled out per route so a new route can't be added
 * without one — routes that need a DIFFERENT boundary (see `/share/:token`
 * below) set their own `errorElement` explicitly; `guarded()` only fills in
 * the default where one isn't already present, it never overrides one.
 */
function guarded(routes: RouteObject[]): RouteObject[] {
  return routes.map((route) => ({
    ...route,
    errorElement: route.errorElement ?? <RouteErrorBoundary />,
    ...(route.children ? { children: guarded(route.children) } : {}),
  })) as RouteObject[]
}

export const router = createBrowserRouter(guarded([
  {
    element: <AppLayout />,
    children: [
      // --- Personal / global (not tenant-scoped) ---
      { path: '/system', element: <SystemPage /> },
      { path: '/insights', element: <InsightsPage /> },
      { path: '/sessions', element: <SessionsPage /> },
      { path: '/supervisor', element: <SupervisorPage /> },
      { path: '/schedules', element: <SchedulesPage /> },
      { path: '/activity', element: <ActivityPage /> },
      { path: '/settings', element: <SettingsPage /> },
      // --- Public viewers (root; self-enforce visibility) ---
      { path: '/walkthrough/:id', element: <WalkthroughViewerPage /> },
      { path: '/review/:id', element: <ReviewPage /> },
      // /invite/:token — the accept page. Deliberately OUTSIDE /w/:workspace:
      // an invitee has no workspace membership yet, so there is no tenant to
      // scope it under. Self-enforces via the preview/accept endpoints
      // (AuthProvider + client.v2 both allowlist this prefix so an
      // unauthenticated visitor reaches it instead of bouncing to login).
      { path: '/invite/:token', element: <InviteAcceptPage /> },

      // --- Tenant-scoped surfaces under /w/:workspace ---
      { path: '/w/:workspace', element: <WorkspaceIndex /> },
      { path: '/w/:workspace/members', element: <WorkspaceMembersPage /> },
      { path: '/w/:workspace/timeline', element: <TimelinePage /> },
      { path: '/w/:workspace/shareouts', element: <ShareoutsPage /> },
      { path: '/w/:workspace/shareouts/:period', element: <ShareoutsPage /> },
      { path: '/w/:workspace/walkthroughs', element: <WalkthroughsPage /> },
      { path: '/w/:workspace/agents', element: <AgentsPage /> },
      { path: '/w/:workspace/schedules', element: <SchedulesPage /> },
      {
        path: '/w/:workspace/chat',
        element: (
          <LazySection>
            <ChatListPage />
          </LazySection>
        ),
      },
      {
        path: '/w/:workspace/chat/:id',
        element: (
          <LazySection>
            <ChatPageRoute />
          </LazySection>
        ),
      },
      { path: '/w/:workspace/activity', element: <ActivityPage /> },
      {
        path: '/w/:workspace/agents/:slug',
        element: <AgentWorkspacePage />,
        children: [
          { index: true, element: <Navigate to="inbox" replace /> },
          { path: 'inbox', element: <LazySection><InboxSection /></LazySection> },
          // Legacy path from before the rename; keep the old link working.
          { path: 'needs-you', element: <Navigate to="../inbox" replace /> },
          { path: 'overview', element: <LazySection><AgentOverviewSection /></LazySection> },
          { path: 'tasks', element: <LazySection><AgentTasksSection /></LazySection> },
          { path: 'turns', element: <LazySection><AgentTurnsSection /></LazySection> },
          { path: 'items', element: <LazySection><ItemsSection /></LazySection> },
          { path: 'schedules', element: <LazySection><SchedulesSection /></LazySection> },
          { path: 'syncs', element: <LazySection><AgentSyncsSection /></LazySection> },
          { path: 'work-products', element: <LazySection><AgentWorkProductsSection /></LazySection> },
          { path: 'skills', element: <LazySection><AgentSkillsSection /></LazySection> },
        ],
      },
      { path: '/w/:workspace/ddd', element: <DddPage /> },
      { path: '/w/:workspace/ddd/:narrative', element: <DddPage /> },
      { path: '/w/:workspace/ddd/:narrative/:runId', element: <DddPage /> },

      // --- Legacy flat paths → active workspace (or new viewer) ---
      { path: '/', element: <RootRedirect /> },
      { path: '/timeline', element: <TenantRedirect to="timeline" /> },
      { path: '/shareouts/*', element: <TenantRedirect to="shareouts" /> },
      { path: '/walkthroughs', element: <TenantRedirect to="walkthroughs" /> },
      { path: '/agents/*', element: <TenantRedirect to="agents" /> },
      { path: '/ddd/*', element: <TenantRedirect to="ddd" /> },
      { path: '/ddd-plans', element: <Navigate to="/" replace /> },
      { path: '/reviews', element: <Navigate to="/" replace /> },

      // Catch-all (LAST): an unmatched path is a bad URL OR a browser still on
      // a pre-deploy bundle (PWA precache) whose router lacks a route this
      // deploy added. NotFound reloads once to self-heal the latter, then shows
      // a real 404 — so a new route never surfaces as the RouteErrorBoundary.
      { path: '*', element: <NotFound /> },
    ],
  },
  // Public, chrome-less route — mounted OUTSIDE AppLayout so anonymous
  // visitors aren't bounced to login by the app shell's authed calls. Carries
  // its own light-themed `errorElement` (see `ShareRouteErrorBoundary`) so
  // `guarded()` below leaves it alone instead of hanging the dark, "back to
  // Canopy"-linking app boundary off a page anonymous visitors can't log
  // into.
  { path: '/share/:token', element: <SessionSharePage />, errorElement: <ShareRouteErrorBoundary /> },
  // The clean, shareable DDD run RELEASE page — also mounted OUTSIDE AppLayout,
  // in a chrome-less PublicLayout, so a `?t=<share_token>` viewer with no Dimagi
  // login is served (the release API self-enforces token-or-member access). New
  // URL; the operator console at /w/:ws/ddd/:narrative/:runId is untouched.
  {
    element: <PublicLayout />,
    errorElement: <ShareRouteErrorBoundary />,
    children: [
      { path: '/ddd-release/:narrative/:runId', element: <DddReleasePage /> },
    ],
  },
]), {
  // "/" at root, "/canopy" as a labs tenant — keeps every route + <Link> under
  // the deployment's path prefix (from Vite's import.meta.env.BASE_URL).
  basename: import.meta.env.BASE_URL.replace(/\/$/, '') || '/',
})
