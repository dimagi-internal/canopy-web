import { lazy, Suspense } from 'react'
import type { RouteObject } from 'react-router-dom'
import { createBrowserRouter, Navigate, useLocation, useParams } from 'react-router-dom'
import { useWorkspace } from './workspace/WorkspaceProvider'
import { FirstRunPage } from './pages/FirstRunPage'
import { AppLayout } from './components/AppLayout/AppLayout'
import { RouteErrorBoundary } from './components/RouteErrorBoundary'
import { NotFound } from './components/NotFound'
import { GuidePage } from './pages/GuidePage'
import { ShareRouteErrorBoundary } from './components/ShareRouteErrorBoundary'
import { lazyRoute } from './pwa/staleChunk'
import { CredentialsRedirect } from './pages/agents/CredentialsRedirect'
import { KeepQueryRedirect } from './pages/agents/KeepQueryRedirect'
import { AgentSkillsPage } from './pages/agents/AgentSkillsPage'
import { ShareoutsPage } from './pages/ShareoutsPage'
import { SettingsPage } from './pages/SettingsPage'
import { WalkthroughsPage } from './pages/WalkthroughsPage'
import { WalkthroughViewerPage } from './pages/WalkthroughViewerPage'
import { ReviewPage } from './pages/ReviewPage'
import { FlatArtifactRedirect } from './pages/FlatArtifactRedirect'
import { InviteAcceptPage } from './pages/InviteAcceptPage'
import { ConnectedAppsPage } from './pages/ConnectedAppsPage'
import { WorkspaceSecretsPage } from './pages/WorkspaceSecretsPage'
import { WorkspaceRetentionPage } from './pages/WorkspaceRetentionPage'
import { WorkspaceRunnersPage } from './pages/WorkspaceRunnersPage'
import { WorkspaceAgentAccessPage } from './pages/WorkspaceAgentAccessPage'
import { TopologyMapPage } from './pages/topology/TopologyMapPage'
import { WorkspaceMembersPage } from './pages/WorkspaceMembersPage'
import { WorkspaceAccessRequestsPage } from './pages/WorkspaceAccessRequestsPage'
import { AccessRequestPage } from './pages/AccessRequestPage'
import { BetaRequestPage, BetaRequestsPage } from './pages/BetaRequestsPage'
import { WorkspaceSettingsPage } from './pages/WorkspaceSettingsPage'
import { InboundPushPage } from '@/pages/InboundPushPage'
import { SlackSettingsPage } from '@/pages/SlackSettingsPage'
import { DddPage } from './pages/DddPage'
import { TimelinePage } from './pages/TimelinePage'
import { SystemPage } from './pages/SystemPage'
import { SessionsPage } from './pages/SessionsPage'
import { PeopleMePage } from './pages/PeopleMePage'
import { AgentsPage } from './pages/AgentsPage'
import { AgentWorkspacePage } from './pages/AgentWorkspacePage'
import SessionSharePage from './pages/SessionSharePage'
import DddReleasePage from './pages/DddReleasePage'
import StoryboardPage from './pages/StoryboardPage'
import StoryboardsPage from './pages/StoryboardsPage'
import NarrativeReviewPage from './pages/NarrativeReviewPage'
import { PublicLayout } from './components/PublicLayout'
import { AboutPage } from './pages/AboutPage'
import SupervisorPage from '@/pages/SupervisorPage'
import ActivityPage from '@/pages/ActivityPage'
import SchedulesPage from './pages/SchedulesPage'

/**
 * `lazy()`, plus recovery when the chunk is gone.
 *
 * Every route below is fetched on first use, which can be long after the page
 * loaded — and a deploy in between deletes the filename this page would ask for.
 * `lazyRoute` reloads once in that case (see src/pwa/staleChunk.ts) instead of
 * leaving the section broken behind a Try again button. Use this, not bare
 * `lazy()`, for anything route-shaped.
 */
const lazySection: typeof lazy = (load) => lazy(lazyRoute(load))

// Agent Workspace sections are lazy-loaded — each owns its data fetch and only
// the active section's bundle is pulled in.
const AgentProjectsSection = lazySection(() =>
  import('./pages/agents/AgentProjectsSection').then((m) => ({ default: m.AgentProjectsSection })),
)
const AgentProjectPage = lazySection(() =>
  import('./pages/agents/AgentProjectPage').then((m) => ({ default: m.AgentProjectPage })),
)
const AgentTasksSection = lazySection(() =>
  import('./pages/agents/AgentTasksSection').then((m) => ({ default: m.AgentTasksSection })),
)
const AgentTurnsSection = lazySection(() =>
  import('./pages/agents/AgentTurnsSection').then((m) => ({ default: m.AgentTurnsSection })),
)
const SchedulesSection = lazySection(() =>
  import('./pages/agents/SchedulesSection').then((m) => ({ default: m.SchedulesSection })),
)
const AgentSettingsSection = lazySection(() =>
  import('./pages/agents/AgentSettingsSection').then((m) => ({ default: m.AgentSettingsSection })),
)
const AgentSkillsSection = lazySection(() =>
  import('./pages/agents/AgentSkillsSection').then((m) => ({ default: m.AgentSkillsSection })),
)
const AgentHuddlesSection = lazySection(() =>
  import('./pages/agents/AgentHuddlesSection').then((m) => ({ default: m.AgentHuddlesSection })),
)
// Huddles: the conversation grid is its own chunk (it is the heaviest page here).
const HuddlesPage = lazySection(() =>
  import('./pages/huddles/HuddlesPage').then((m) => ({ default: m.HuddlesPage })),
)
const HuddlePage = lazySection(() =>
  import('./pages/huddles/HuddlePage').then((m) => ({ default: m.HuddlePage })),
)
// Agent threads: a direct, bounded conversation between agents (apps/threads).
const ThreadPage = lazySection(() =>
  import('./pages/threads/ThreadPage').then((m) => ({ default: m.ThreadPage })),
)
const AgentHistorySection = lazySection(() =>
  import('./pages/agents/AgentHistorySection').then((m) => ({ default: m.AgentHistorySection })),
)

// The standalone live-chat route is lazy — it pulls in the canopy-ui/chat kit
// (WebSocket hook + presentational tree) and react-markdown only when opened.
const ChatPage = lazySection(() =>
  import('./pages/ChatPage').then((m) => ({ default: m.ChatPage })),
)
const ChatListPage = lazySection(() =>
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
export function TenantRedirect({ to }: { to: string }) {
  const { active, loading } = useWorkspace()
  const { '*': tail } = useParams()
  if (loading) return null
  if (!active) return <FirstRunPage /> // no membership yet — explain, don't blank
  const suffix = tail ? `/${tail}` : ''
  return <Navigate to={`/w/${active}/${to}${suffix}`} replace />
}

// Bare "/" → the active workspace's index (which lands on its agents).
export function RootRedirect() {
  const { active, loading } = useWorkspace()
  if (loading) return null
  if (!active) return <FirstRunPage />
  return <Navigate to={`/w/${active}`} replace />
}

// A page that became a SECTION of /w/:workspace/settings. The workspace is
// already in the URL, so this needs no workspace list — unlike TenantRedirect
// above, which is resolving which workspace you meant. Query and hash are
// carried over: /w/x/inbound is where the gcloud runbook sends people, and an
// inbound link can arrive with one.
export function SettingsRedirect({ to }: { to: string }) {
  const { workspace } = useParams()
  const { search, hash } = useLocation()
  return <Navigate to={`/w/${workspace}/settings/${to}${search}${hash}`} replace />
}

// /w/:workspace index. Disambiguates a legacy /w/<uuid> walkthrough link
// (redirect to the new viewer) from a real workspace slug, which lands on the
// workspace's agents: their projects and tasks are canopy's one project system
// (the workbench Projects page that used to live here was retired 2026-10-06).
function WorkspaceIndex() {
  const { workspace } = useParams()
  const { search, hash } = useLocation()
  if (workspace && UUID_RE.test(workspace)) {
    // Preserve ?t=<share_token> and #t=<seconds> across the redirect.
    return <Navigate to={`/walkthrough/${workspace}${search}${hash}`} replace />
  }
  return <Navigate to={`/w/${workspace}/agents`} replace />
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

/**
 * The route table, as data.
 *
 * Exported so the guide's coverage test can read it: every documented surface
 * must have a descriptor in guide/surfaces.ts, and every descriptor must name a
 * real route. Extracting paths from a BUILT router is awkward; reading them from
 * this array is three lines.
 */
export const routeTable: RouteObject[] = [
  {
    element: <AppLayout />,
    children: [
      // --- Personal / global (not tenant-scoped) ---
      { path: '/system', element: <SystemPage /> },
      { path: '/guide', element: <GuidePage /> },
      { path: '/sessions', element: <SessionsPage /> },
      { path: '/people/me', element: <PeopleMePage /> },
      { path: '/supervisor', element: <SupervisorPage /> },
      { path: '/schedules', element: <SchedulesPage /> },
      { path: '/activity', element: <ActivityPage /> },
      { path: '/settings', element: <SettingsPage /> },
      { path: '/new-workspace', element: <FirstRunPage alwaysOfferForm /> },
      // Requests for access to Canopy itself (the public site's form) — they
      // name no workspace, so they live outside /w/:workspace.
      { path: '/beta-requests', element: <BetaRequestsPage /> },
      { path: '/beta-requests/:requestId', element: <BetaRequestPage /> },
      // --- Old flat viewer links → the same page under its workspace ---
      // Every link sent before canopy-web#1289 points here, so these resolve the
      // row's workspace and replace the URL; if they cannot, the page renders in
      // place and says what it always said (not found, sign in).
      {
        path: '/walkthrough/:id',
        element: <FlatArtifactRedirect kind="walkthrough"><WalkthroughViewerPage /></FlatArtifactRedirect>,
      },
      {
        path: '/review/:id',
        element: <FlatArtifactRedirect kind="review"><ReviewPage /></FlatArtifactRedirect>,
      },
      // /invite/:token — the accept page. Deliberately OUTSIDE /w/:workspace:
      // an invitee has no workspace membership yet, so there is no tenant to
      // scope it under. Self-enforces via the preview/accept endpoints
      // (AuthProvider + client.v2 both allowlist this prefix so an
      // unauthenticated visitor reaches it instead of bouncing to login).
      { path: '/invite/:token', element: <InviteAcceptPage /> },

      // --- Tenant-scoped surfaces under /w/:workspace ---
      { path: '/w/:workspace', element: <WorkspaceIndex /> },
      // Workspace settings — ONE surface, four sections. Each section keeps its
      // own URL so a deep link still names a place ("the Slack settings"), and
      // still loads only its own data.
      {
        path: '/w/:workspace/settings',
        element: <WorkspaceSettingsPage />,
        children: [
          { index: true, element: <Navigate to="members" replace /> },
          { path: 'members', element: <WorkspaceMembersPage /> },
          // People asking to be invited. `:requestId` is the page the admins'
          // notification email links to (services.access_request_link).
          { path: 'access-requests', element: <WorkspaceAccessRequestsPage /> },
          { path: 'access-requests/:requestId', element: <AccessRequestPage /> },
          { path: 'slack', element: <SlackSettingsPage /> },
          { path: 'inbound', element: <InboundPushPage /> },
          { path: 'connected-apps', element: <ConnectedAppsPage /> },
          { path: 'secrets', element: <WorkspaceSecretsPage /> },
          { path: 'retention', element: <WorkspaceRetentionPage /> },
          { path: 'topology', element: <TopologyMapPage /> },
          { path: 'topology/runners', element: <WorkspaceRunnersPage /> },
          { path: 'topology/agents', element: <WorkspaceAgentAccessPage /> },
          // Where the two tables lived before the map joined them under Topology.
          { path: 'runners', element: <Navigate to="../topology/runners" replace /> },
          { path: 'agent-access', element: <Navigate to="../topology/agents" replace /> },
        ],
      },
      // The four pages these sections used to be. Live links were handed to
      // people, so they redirect rather than 404.
      { path: '/w/:workspace/members', element: <SettingsRedirect to="members" /> },
      { path: '/w/:workspace/connected-apps', element: <SettingsRedirect to="connected-apps" /> },
      { path: '/w/:workspace/inbound', element: <SettingsRedirect to="inbound" /> },
      { path: '/w/:workspace/slack', element: <SettingsRedirect to="slack" /> },
      { path: '/w/:workspace/timeline', element: <TimelinePage /> },
      { path: '/w/:workspace/shareouts', element: <ShareoutsPage /> },
      { path: '/w/:workspace/shareouts/:period', element: <ShareoutsPage /> },
      { path: '/w/:workspace/walkthroughs', element: <WalkthroughsPage /> },
      { path: '/w/:workspace/storyboards', element: <StoryboardsPage /> },
      { path: '/w/:workspace/agents', element: <AgentsPage /> },
      // The retired workbench Projects page. Agent projects/tasks replaced it.
      { path: '/w/:workspace/projects', element: <WorkspaceIndex /> },
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
      // A team of agents syncing in rounds (canopy `huddle`), derived from turns.
      { path: '/w/:workspace/huddles', element: <LazySection><HuddlesPage /></LazySection> },
      { path: '/w/:workspace/huddles/:id', element: <LazySection><HuddlePage /></LazySection> },
      { path: '/w/:workspace/threads/:id', element: <LazySection><ThreadPage /></LazySection> },
      {
        path: '/w/:workspace/agents/:slug',
        element: <AgentWorkspacePage />,
        children: [
          // Projects is where an agent opens: the outcomes it is working toward.
          { index: true, element: <Navigate to="projects" replace /> },
          { path: 'projects', element: <LazySection><AgentProjectsSection /></LazySection> },
          { path: 'projects/:ref', element: <LazySection><AgentProjectPage /></LazySection> },
          { path: 'tasks', element: <LazySection><AgentTasksSection /></LazySection> },
          // Old addresses for the same things — bookmarks and links in old emails.
          // Those that took a query keep it (an old `/items?batch=` link).
          { path: 'work', element: <KeepQueryRedirect to="../tasks" /> },
          { path: 'inbox', element: <KeepQueryRedirect to="../tasks" add={{ waiting: 'me' }} /> },
          { path: 'needs-you', element: <KeepQueryRedirect to="../tasks" add={{ waiting: 'me' }} /> },
          { path: 'items', element: <KeepQueryRedirect to="../tasks" /> },
          { path: 'overview', element: <Navigate to="../projects" replace /> },
          { path: 'work-products', element: <Navigate to="../projects" replace /> },
          { path: 'syncs', element: <Navigate to="../turns#status-reports" replace /> },
          { path: 'turns', element: <LazySection><AgentTurnsSection /></LazySection> },
          { path: 'schedules', element: <LazySection><SchedulesSection /></LazySection> },
          { path: 'huddles', element: <LazySection><AgentHuddlesSection /></LazySection> },
          { path: 'settings', element: <LazySection><AgentSettingsSection /></LazySection> },
          // Credentials is a section of Settings now. Old links (and bookmarks)
          // keep working and keep their query, e.g. `?google=ok`.
          { path: 'credentials', element: <CredentialsRedirect /> },
          {
            // Skills and their history are one subject, two views.
            path: 'skills',
            element: <AgentSkillsPage />,
            children: [
              { index: true, element: <LazySection><AgentSkillsSection /></LazySection> },
              { path: 'history', element: <LazySection><AgentHistorySection /></LazySection> },
            ],
          },
          // History was its own rail entry, named after neither skills nor the
          // repo it reads. Old links keep working.
          { path: 'history', element: <Navigate to="../skills/history" replace /> },
        ],
      },
      // Public viewers, under their workspace: a share token still opens one
      // artifact without a login (they self-enforce visibility, and the server
      // and client both let an anonymous visitor reach exactly these three —
      // lib/scopedLinks.ts), and the API confirms the row lives HERE.
      { path: '/w/:workspace/walkthrough/:id', element: <WalkthroughViewerPage /> },
      { path: '/w/:workspace/review/:id', element: <ReviewPage /> },
      { path: '/w/:workspace/ddd', element: <DddPage /> },
      { path: '/w/:workspace/ddd/:narrative', element: <DddPage /> },
      { path: '/w/:workspace/ddd/:narrative/:runId', element: <DddPage /> },

      // --- Legacy flat paths → active workspace (or new viewer) ---
      { path: '/', element: <RootRedirect /> },
      // The public site owns a full page load of `/` (config/public_site.py); `/app`
      // is the server-loadable door into the app — the site's Sign in, and where a
      // login with no `next` lands.
      { path: '/app', element: <RootRedirect /> },
      { path: '/timeline', element: <TenantRedirect to="timeline" /> },
      { path: '/shareouts/*', element: <TenantRedirect to="shareouts" /> },
      { path: '/walkthroughs', element: <TenantRedirect to="walkthroughs" /> },
      { path: '/storyboards', element: <TenantRedirect to="storyboards" /> },
      { path: '/huddles/*', element: <TenantRedirect to="huddles" /> },
      { path: '/agents/*', element: <TenantRedirect to="agents" /> },
      { path: '/ddd/*', element: <TenantRedirect to="ddd" /> },
      { path: '/ddd-plans', element: <Navigate to="/" replace /> },
      { path: '/reviews', element: <Navigate to="/" replace /> },
      // The Insights feed was retired (superseded by agent tasks/items); old
      // bookmarks land on the home page rather than the not-found screen.
      { path: '/insights', element: <Navigate to="/" replace /> },
      // Same for the retired workbench Projects page (it was the workspace index).
      { path: '/projects', element: <Navigate to="/" replace /> },

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
  { path: '/w/:workspace/share/:token', element: <SessionSharePage />, errorElement: <ShareRouteErrorBoundary /> },
  // The old flat link: resolves the share's workspace and moves there.
  {
    path: '/share/:token',
    element: <FlatArtifactRedirect kind="share"><SessionSharePage /></FlatArtifactRedirect>,
    errorElement: <ShareRouteErrorBoundary />,
  },
  // The clean, shareable DDD run RELEASE page — also mounted OUTSIDE AppLayout,
  // in a chrome-less PublicLayout, so a `?t=<share_token>` viewer with no Dimagi
  // login is served (the release API self-enforces token-or-member access). New
  // URL; the operator console at /w/:ws/ddd/:narrative/:runId is untouched.
  {
    element: <PublicLayout />,
    errorElement: <ShareRouteErrorBoundary />,
    children: [
      { path: '/ddd-release/:narrative/:runId', element: <DddReleasePage /> },
      // The shared ARC — several narratives as one link. Same public shape as
      // the release page above: the API self-enforces token-or-member access.
      { path: '/storyboard/:slug', element: <StoryboardPage /> },
      // One narrative, scene by scene, for an outsider. `?b=<board>` carries the
      // storyboard whose token gates it — the narrative itself has no token.
      { path: '/narrative/:slug', element: <NarrativeReviewPage /> },
      // The public explainer — anyone, no login. Mounted here (not AppLayout)
      // for the same reason as its siblings above.
      { path: '/about', element: <AboutPage /> },
    ],
  },
]

export const router = createBrowserRouter(guarded(routeTable), {
  // "/" at root, "/canopy" as a labs tenant — keeps every route + <Link> under
  // the deployment's path prefix (from Vite's import.meta.env.BASE_URL).
  basename: import.meta.env.BASE_URL.replace(/\/$/, '') || '/',
})
