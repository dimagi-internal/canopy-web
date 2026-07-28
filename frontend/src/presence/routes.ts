import type { RouteRule } from 'canopy-ui/presence'

// Human labels for an Agent Workspace's nested sections (see router.tsx's
// `/w/:workspace/agents/:slug` children). Falls back to the raw segment for
// any section added to the router but not listed here.
const AGENT_SECTION_LABELS: Record<string, string> = {
  inbox: 'Inbox',
  overview: 'Overview',
  tasks: 'Tasks',
  turns: 'Turns',
  items: 'Items',
  schedules: 'Schedules',
  syncs: 'Syncs',
  'work-products': 'Work products',
  skills: 'Skills',
}

// Human labels for the single-segment tenant surfaces caught by the generic
// rule at the bottom of the tenant block below.
const TENANT_RESOURCE_LABELS: Record<string, string> = {
  members: 'Members',
  timeline: 'Timeline',
  shareouts: 'Shareouts',
  walkthroughs: 'Walkthroughs',
  agents: 'Agents',
  schedules: 'Schedules',
  chat: 'Chats',
  activity: 'Activity',
  ddd: 'DDD',
}

const GLOBAL_RESOURCE_LABELS: Record<string, string> = {
  system: 'System',
  insights: 'Insights',
  sessions: 'Sessions',
  supervisor: 'Supervisor',
  schedules: 'Schedules',
  activity: 'Activity',
  settings: 'Settings',
}

/**
 * canopy-web's route table for presence grouping.
 *
 * Built from `frontend/src/router.tsx`'s actual route list (2026-07-27), not
 * a guess — see task-8-brief.md's Step 6, which drafted a shape that didn't
 * match this repo's real routes (no `/w/:workspace/agents/:slug` nested
 * sections in the draft, no chat-list vs. chat-session distinction, no DDD
 * routes at all).
 *
 * Order matters — the first match wins, so more specific patterns (a chat
 * session, an agent's nested section, a DDD run) must be listed before the
 * looser single-segment catch-alls that would otherwise absorb them.
 *
 * `/invite/:token` has no rule here on purpose: a pending invitee has no
 * workspace membership yet, so pageKeyFor returns null and no badge renders
 * — the safe default rather than putting them in a stranger's roster.
 */
export const canopyPresenceRules: RouteRule[] = [
  // --- Tenant-scoped (/w/:workspace/...) --------------------------------

  // Each chat session is its own roster — two people in different chats are
  // not "on the same page" just because both are chatting.
  {
    pattern: /^\/w\/([^/]+)\/chat\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `session:${m[2]}`, subLocation: 'Chat' }),
  },

  // Agent Workspace: every nested section (inbox, overview, tasks, turns,
  // items, schedules, syncs, work-products, skills — see router.tsx's
  // `/w/:workspace/agents/:slug` children) is a VIEW of the same agent, not
  // a distinct object. Collapse them onto one `agent:<slug>` roster, exactly
  // as ace-web collapses a run's steps onto one run key. The section name
  // survives only as `subLocation`, for the expanded viewer list — not the
  // grouping key.
  {
    pattern: /^\/w\/([^/]+)\/agents\/([^/]+)\/([a-z-]+)/,
    build: (m) => ({
      workspace: m[1],
      resource: `agent:${m[2]}`,
      subLocation: AGENT_SECTION_LABELS[m[3]] ?? m[3],
    }),
  },
  // Same agent, no section segment yet (mid-redirect to its default `inbox`
  // section — see router.tsx's `index: <Navigate to="inbox" />` — or a bare
  // link). Same collapsed key, generic subLocation.
  {
    pattern: /^\/w\/([^/]+)\/agents\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `agent:${m[2]}`, subLocation: 'Agent' }),
  },

  // DDD: a specific run (`/ddd/:narrative/:runId`) is its own roster — a
  // distinct rendered/edited state, like a chat session. The narrative page
  // with no run id (`/ddd/:narrative`) is a separate "editor" roster; the
  // bare list (`/ddd`) is separate again.
  {
    pattern: /^\/w\/([^/]+)\/ddd\/([^/]+)\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `ddd:${m[2]}:${m[3]}`, subLocation: 'Run' }),
  },
  {
    pattern: /^\/w\/([^/]+)\/ddd\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `ddd:${m[2]}`, subLocation: 'Narrative' }),
  },

  // A specific shareouts period (`/shareouts/:period`) is its own roster —
  // distinct content per period, like a chat session or a DDD run.
  {
    pattern: /^\/w\/([^/]+)\/shareouts\/([^/]+)/,
    build: (m) => ({ workspace: m[1], resource: `shareouts:${m[2]}`, subLocation: 'Shareouts' }),
  },

  // Workspace index (the Projects dashboard) — no further path segment.
  {
    pattern: /^\/w\/([^/]+)\/?$/,
    build: (m) => ({ workspace: m[1], resource: 'projects', subLocation: 'Projects' }),
  },

  // Every other single-segment tenant surface (members, timeline,
  // walkthroughs, the agents LIST, schedules, the chat LIST, activity, the
  // ddd LIST): resource == the path segment, one roster per surface.
  {
    pattern: /^\/w\/([^/]+)\/([a-z-]+)/,
    build: (m) => ({
      workspace: m[1],
      resource: m[2],
      subLocation: TENANT_RESOURCE_LABELS[m[2]] ?? m[2],
    }),
  },

  // --- Global (not tenant-scoped) ---------------------------------------

  {
    pattern: /^\/walkthrough\/([^/]+)/,
    build: (m) => ({ workspace: 'global', resource: `walkthrough:${m[1]}`, subLocation: 'Walkthrough' }),
  },
  {
    pattern: /^\/review\/([^/]+)/,
    build: (m) => ({ workspace: 'global', resource: `review:${m[1]}`, subLocation: 'Review' }),
  },
  // The remaining top-level personal/global pages (see router.tsx's
  // non-tenant routes): one roster per page.
  {
    pattern: /^\/(system|insights|sessions|supervisor|schedules|activity|settings)\b/,
    build: (m) => ({
      workspace: 'global',
      resource: m[1],
      subLocation: GLOBAL_RESOURCE_LABELS[m[1]] ?? m[1],
    }),
  },
]
