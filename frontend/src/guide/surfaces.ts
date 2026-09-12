/**
 * One descriptor per documented surface — the in-app guide's content, as DATA.
 *
 * Data, not prose in a wiki, for the same reason systemWorkflows.ts is data: a
 * test can read this and a page can render it; prose in another repo can do
 * neither. guide/coverage.test.ts asserts this file covers every route and
 * names no route that does not exist.
 *
 * `path` MUST match the route path in router.tsx character for character.
 */
export interface SurfaceDescriptor {
  /** Exactly the route path from routeTable. */
  path: string
  title: string
  /** Who this is for, in a few words. */
  audience: string
  /** What it is, one line. */
  what: string
  /** What you need before it does anything useful. */
  needsFirst?: string
  /** What you can actually do here. */
  actions?: string[]
}

export const SURFACES: SurfaceDescriptor[] = [
  // --- Personal / global (not tenant-scoped) ---
  {
    path: '/system',
    title: 'Capabilities',
    audience: 'Anyone deciding what canopy can do',
    what: "Every skill, agent and command the canopy plugin ships, read live from the plugin's own files, plus curated chains showing how they compose.",
    actions: ['Browse the catalog', 'Read a capability in full'],
  },
  {
    path: '/guide',
    title: 'Guide',
    audience: 'Anyone finding their way around',
    what: "What every surface in Canopy is for, generated from the app's own route table.",
    actions: ['Find the page you need', 'See what a surface needs before it works'],
  },
  {
    path: '/insights',
    title: 'Insights',
    audience: 'Anyone watching the portfolio',
    what: 'A cross-portfolio feed of AI-generated findings (ship gaps, hygiene, patterns, stale work, opportunities, alignment), filterable by category and project, with per-item dismiss and a bulk clear.',
    needsFirst: 'An insight-generating skill (e.g. canopy:portfolio-review) run at least once.',
    actions: ['Filter by category or project', 'Dismiss an insight', 'Clear everything in view'],
  },
  {
    path: '/sessions',
    title: 'Shared sessions',
    audience: 'Anyone who wants to hand someone a transcript link',
    what: 'Claude Code transcripts you (or your agents/teammates) have shared as read-only web pages — one row per session with its visibility and link.',
    needsFirst: 'canopy:share-session run once to upload a transcript.',
    actions: ['Copy or open a share link', 'Rotate a link', 'Toggle private/link visibility', 'Delete a shared session'],
  },
  {
    path: '/supervisor',
    title: 'Supervisor',
    audience: 'Someone who owns agents',
    what: 'The cross-fleet "waiting on you" inbox, agent KPI cards and runner status. Spans every workspace, which is why it is not tenant-scoped.',
    needsFirst: 'At least one agent you own.',
    actions: ['Answer a blocked agent', 'See which runners are online', 'Install it as a phone app and get pushed when an agent needs you'],
  },
  {
    path: '/schedules',
    title: 'Schedules (all workspaces)',
    audience: 'Anyone with recurring agent work',
    what: 'A personal weekly calendar of every recurring schedule across every workspace you belong to, filterable by workspace and agent. Same calendar component as the per-workspace and per-agent views.',
    needsFirst: 'An agent with at least one schedule configured.',
    actions: ['See what fires this week', 'Filter by workspace or agent', 'Jump into a schedule to edit or run it'],
  },
  {
    path: '/activity',
    title: 'Activity (all workspaces)',
    audience: 'Anyone checking "did that actually run"',
    what: 'A read-only log of the last 20 triggered harness turns across your workspaces — time, agent, trigger, runner, status — with filters and a per-turn event-ledger drill-down. The "what DID fire" counterpart to Schedules ("what WILL fire").',
    needsFirst: 'A turn triggered by a schedule, an inbound event, or a manual run.',
    actions: ['Filter by agent, trigger or status', 'Expand a turn to see its raw event ledger'],
  },
  {
    path: '/settings',
    title: 'Settings',
    audience: 'Everyone',
    what: 'Your account and this deployment: which AI backend is in use, your Claude subscription connection, presence visibility, and your personal access tokens.',
    actions: ['Mint a personal access token', 'Revoke a token', 'Switch AI backend', 'Toggle presence'],
  },
  {
    path: '/new-workspace',
    title: 'Create a workspace',
    audience: 'A brand-new user with no workspace yet',
    what: 'The first-run screen: explains what a workspace is, then either offers a create form (address + optional display name) or explains you need an invite, depending on your account\'s create eligibility.',
    actions: ['Create a workspace (if eligible)'],
  },

  // --- Public viewers (root; self-enforce visibility) ---
  {
    path: '/walkthrough/:id',
    title: 'Walkthrough viewer',
    audience: 'Whoever was sent the link',
    what: 'Plays a single shared walkthrough — an HTML slideshow or a video — with owner-only controls to toggle public/private, rotate the share link, or delete it. Deep-links to a scene (`#scene-N`) or a video timestamp (`#t=`) are supported. A signed-out visitor on a private link is routed to sign in rather than shown a bare error.',
    actions: ['Watch the walkthrough', 'Copy or rotate the public link (owner)', 'Toggle visibility (owner)', 'Delete it (owner)'],
  },
  {
    path: '/review/:id',
    title: 'Narrative review',
    audience: 'Whoever the review request was addressed to',
    what: "The DDD narrative-review surface behind a review request: watch the cut, edit any scene's narration/features/personas inline, see grounding and gaps inline per scene, reorder the build sequence, and resolve the gate (approve / send back to redraft, or answer a findings review). External guests land here with a share token and can only suggest edits, never resolve the gate.",
    actions: ['Watch the cut', 'Edit narration, features or personas', 'Reorder the build sequence', 'Approve, redraft, or submit findings decisions'],
  },
  {
    path: '/invite/:token',
    title: 'Accept invite',
    audience: 'Someone invited to a workspace',
    what: "The accept-invite landing page: previews the inviting workspace and role (masked email), then lets a signed-in matching account accept. Deliberately outside /w/:workspace — there's no tenant to scope into until the invite is accepted.",
    actions: ['Sign in with the invited address', 'Accept the invite'],
  },

  // --- Tenant-scoped surfaces under /w/:workspace ---
  {
    path: '/w/:workspace',
    title: 'Project workbench',
    audience: 'Anyone in a workspace',
    what: 'The workspace home — a tile per project, ranked by how many insights are waiting on it, with the top three surfaced as a hero.',
    actions: ['Triage an insight inline', 'Open a project'],
  },
  {
    path: '/w/:workspace/members',
    title: 'Members',
    audience: 'Any workspace member (owner-only actions)',
    what: 'Admin surface for who is in the workspace: the member list with roles, and pending invites — each invite is a copy-linkable `/invite/:token` URL (canopy sends no email itself).',
    actions: ['Invite someone by email', 'Change a member\'s role', 'Remove a member', 'Revoke a pending invite'],
  },
  {
    path: '/w/:workspace/inbound',
    title: 'Inbound push',
    audience: 'Workspace owner setting up email-triggered agents',
    what: "Configure the doorbell that lets an inbound Gmail message wake an agent: manage which mailboxes are watched, and generate the exact gcloud setup commands (project, Pub/Sub topic, service account) for the Gmail watch this depends on.",
    actions: ['Add or remove a watched mailbox', 'Enable/disable a mailbox', 'Copy the GCP setup commands'],
  },
  {
    path: '/w/:workspace/timeline',
    title: 'Timeline',
    audience: 'Anyone tracking cross-app activity',
    what: 'A team activity feed of what happened across the workspace\'s apps — link-out only (each row opens the real surface it came from).',
    actions: ['Scan recent activity', 'Open the underlying item'],
  },
  {
    path: '/w/:workspace/shareouts',
    title: 'Shareouts',
    audience: 'Teammates catching up on what shipped',
    what: 'Dated, teammate-facing work briefings — what shipped, why it matters, how to leverage it — posted by /canopy:shareout.',
    needsFirst: 'canopy:shareout posted at least once.',
    actions: ['Read a briefing', 'Open a specific dated period'],
  },
  {
    path: '/w/:workspace/shareouts/:period',
    title: 'Shareout (one period)',
    audience: 'Teammates catching up on what shipped',
    what: 'A single dated briefing, at a copy-linkable permalink — same content as the Shareouts feed, scoped to one period.',
    actions: ['Read this briefing', 'Copy its permalink'],
  },
  {
    path: '/w/:workspace/walkthroughs',
    title: 'Walkthroughs',
    audience: 'Anyone looking for a recorded demo',
    what: 'The list of every walkthrough (HTML slideshow or video) uploaded from /canopy:walkthrough, filterable by project, kind, and "mine only".',
    needsFirst: 'A walkthrough uploaded via canopy:walkthrough or a DDD run.',
    actions: ['Filter by project or kind', 'Open a walkthrough'],
  },
  {
    path: '/w/:workspace/storyboards',
    title: 'Storyboards',
    audience: 'Anyone assembling several demos into one shareable link',
    what: 'The index of storyboards — the shared arcs — one row each: title, layout (review/reel), act count, and the token-bearing share link.',
    needsFirst: 'A storyboard assembled from DDD narratives.',
    actions: ['Copy a storyboard\'s share link', 'Open a storyboard'],
  },
  {
    path: '/w/:workspace/agents',
    title: 'Agents',
    audience: 'Anyone working with the fleet',
    what: 'The list of first-class AI agents registered in this workspace, each card showing its sync/work-product/skill counts.',
    needsFirst: 'At least one agent registered in this workspace.',
    actions: ['Open an agent\'s workspace'],
  },
  {
    path: '/w/:workspace/schedules',
    title: 'Schedules',
    audience: 'Anyone with recurring agent work in this workspace',
    what: 'The weekly calendar of recurring schedules scoped to this workspace. Same component as the personal /schedules view, without the cross-workspace filter (this route is already one workspace).',
    needsFirst: 'An agent with at least one schedule configured.',
    actions: ['See what fires this week', 'Jump into a schedule to edit or run it'],
  },
  {
    path: '/w/:workspace/chat',
    title: 'Chats',
    audience: 'An operator who wants work done',
    what: 'Your chat sessions with agents, so you can pick one up from any device.',
    needsFirst: 'An agent in this workspace, and a runner online to execute its turns.',
    actions: ['Start a new chat with an agent', 'Continue an existing session'],
  },
  {
    path: '/w/:workspace/chat/:id',
    title: 'One chat',
    audience: 'An operator',
    what: 'A live multiplayer chat with an agent. Sending enqueues a turn; the reply streams back as the agent works.',
    needsFirst: 'A runner online and assigned to this agent.',
    actions: ['Send a message', 'Answer a question the agent is blocked on', 'Move the session to another runner'],
  },
  {
    path: '/w/:workspace/activity',
    title: 'Activity',
    audience: 'Anyone checking "did that actually run"',
    what: 'The same turn log as /activity, scoped to this workspace: time, agent, trigger, runner, status, with a per-turn event-ledger drill-down.',
    needsFirst: 'A turn triggered by a schedule, an inbound event, or a manual run.',
    actions: ['Filter by agent, trigger or status', 'Expand a turn to see its raw event ledger'],
  },
  {
    path: '/w/:workspace/agents/:slug/inbox',
    title: 'Agent inbox',
    audience: 'The person(s) who own this agent',
    what: "The agent's OPEN items — reviews, then questions — each decidable right where it sits. This is the default landing section of the agent workspace, and the per-agent counterpart to Supervisor's fleet-wide inbox.",
    actions: ['Decide an item in place (implement / skip / defer)'],
  },
  {
    path: '/w/:workspace/agents/:slug/overview',
    title: 'Agent overview',
    audience: 'The person(s) who own this agent',
    what: "The agent's control surface: dispatch a one-off prompt straight to it, manage its ordered runner assignment list, toggle its turn mode, and see recent syncs/tasks at a glance.",
    actions: ['Send a quick prompt to the agent', 'Reorder or edit runner assignments', 'Toggle turn mode'],
  },
  {
    path: '/w/:workspace/agents/:slug/tasks',
    title: 'Agent tasks',
    audience: 'The person(s) who own this agent',
    what: 'The "who has the ball" board for this agent — its open tasks plus the command queue/history that drives them (accept / decline / dispatch / done).',
    actions: ['Accept, decline or dispatch a task', 'Mark a task done'],
  },
  {
    path: '/w/:workspace/agents/:slug/turns',
    title: 'Agent turns',
    audience: 'The person(s) who own this agent',
    what: "The agent's full turn ledger — every packaged unit of work it has done, with what it advanced, what it did, its deliverables, and optionally a transcript link.",
    actions: ['Browse past turns', 'Open a turn\'s transcript'],
  },
  {
    path: '/w/:workspace/agents/:slug/items',
    title: 'Agent items',
    audience: 'The person(s) who own this agent',
    what: "The full item ledger for this agent — including decided and dismissed items, not just the open ones the inbox shows. `?batch=<key>` renders one sitting (e.g. a fleet audit) as a single reviewable set.",
    actions: ['Review a decided or dismissed item', 'Decide items from one batch in a single sitting'],
  },
  {
    path: '/w/:workspace/agents/:slug/schedules',
    title: 'Agent schedules',
    audience: 'The person(s) who own this agent',
    what: "A dense table of this agent's recurring activities — each row's server-computed next fire time and last outcome, plus the controls to run it off-cycle, pause it, or edit it.",
    needsFirst: 'At least one schedule configured for this agent.',
    actions: ['Run a schedule now', 'Pause or edit a schedule', 'Create a new schedule'],
  },
  {
    path: '/w/:workspace/agents/:slug/syncs',
    title: 'Agent syncs',
    audience: 'The person(s) who own this agent',
    what: "The agent's sync log — its periodic check-ins (e.g. a manager sync) recorded as cards.",
    actions: ['Browse past syncs'],
  },
  {
    path: '/w/:workspace/agents/:slug/credentials',
    title: 'Agent credentials',
    audience: 'The person(s) who own this agent',
    what: "What is set and what is missing for this agent to actually run — write-only status rows (set/unset + when, never the secret itself) for each required credential, plus a vault of less-common ones collapsed by default. Answers \"what is stopping this agent from running\" without an SSH session.",
    actions: ['Set or clear a credential', 'Mint a Google credential', 'See when each credential was last set'],
  },
  {
    path: '/w/:workspace/agents/:slug/work-products',
    title: 'Agent work products',
    audience: 'The person(s) who own this agent, or anyone consuming its output',
    what: "The agent's produced deliverables — the artifacts it has shipped, as cards.",
    actions: ['Browse work products'],
  },
  {
    path: '/w/:workspace/agents/:slug/skills',
    title: 'Agent skills',
    audience: 'The person(s) who own this agent',
    what: "The catalog of skills this agent has available, as cards.",
    actions: ['Browse the agent\'s skill catalog'],
  },
  {
    path: '/w/:workspace/ddd',
    title: 'DDD narratives',
    audience: 'Anyone building or reviewing a demo',
    what: 'The narrative gallery — pick a demo-driven-development narrative to open its landing page (story + runs).',
    needsFirst: 'A narrative run through the canopy:ddd loop.',
    actions: ['Browse narratives', 'Open one'],
  },
  {
    path: '/w/:workspace/ddd/:narrative',
    title: 'DDD narrative',
    audience: 'Anyone building or reviewing a demo',
    what: 'One narrative\'s landing page: its story plus every run against it, so you can pick a specific run\'s package.',
    actions: ['Read the narrative\'s story', 'Open a specific run'],
  },
  {
    path: '/w/:workspace/ddd/:narrative/:runId',
    title: 'DDD run package',
    audience: 'Anyone building or reviewing a demo',
    what: 'One run\'s full package: rendered video, deck, narrative and links, grouped together — the operator console for that run.',
    actions: ['Watch the rendered cut', 'Open the deck', 'Follow linked artifacts'],
  },

  // --- Public, chrome-less routes (mounted outside the app shell) ---
  {
    path: '/share/:token',
    title: 'Shared session (public)',
    audience: 'Whoever was sent the link',
    what: 'A public, chrome-less, read-only viewer for a shared Claude Code transcript — or for several grouped as one arc, which lands on a clickable list of the member sessions. No login required. Best-effort secret-scrubbed before sharing.',
    actions: ['Read the transcript', 'Open a session from a shared arc'],
  },
  {
    path: '/ddd-release/:narrative/:runId',
    title: 'DDD run release (public)',
    audience: 'An external stakeholder sent the link',
    what: "The clean, shareable face of a DDD run: title, final video and the narrative story, with no phase/gate jargon or artifact dump. A member additionally sees a link into the operator console.",
    actions: ['Watch the release cut', 'Read the story'],
  },
  {
    path: '/storyboard/:slug',
    title: 'Storyboard (public)',
    audience: 'An external stakeholder sent the link',
    what: 'The shared arc — several DDD narratives shown as one link, chrome-less and token-gated, following each narrative\'s current release so the link never goes stale.',
    actions: ['Read the arc', 'Leave a note (if the board allows comments)'],
  },
  {
    path: '/narrative/:slug',
    title: 'Narrative (public)',
    audience: 'An external reviewer sent the link',
    what: "One narrative, scene by scene, for someone meeting the story for the first time — clean by default, with an optional toggle to see what changed since the last version. Editing (if allowed) opens the scene's own text directly.",
    actions: ['Read the narrative', 'Toggle "what changed"', 'Comment or suggest an edit (if the board allows it)'],
  },
]

export function describedPaths(): Set<string> {
  return new Set(SURFACES.map((s) => s.path))
}
