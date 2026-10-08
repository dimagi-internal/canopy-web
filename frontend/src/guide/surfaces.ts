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
    path: '/sessions',
    title: 'Shared transcripts',
    audience: 'Anyone who wants to hand someone a transcript link',
    what: 'Claude Code transcripts you (or your agents/teammates) have shared as read-only web pages — one row per transcript with its visibility and link. Not in the menus: a shared transcript is opened by its own /share/<token> link, and this page is where you manage the ones you shared. Live conversations with agents are Chats.',
    needsFirst: 'canopy:share-session run once to upload a transcript.',
    actions: ['Copy or open a share link', 'Rotate a link', 'Toggle private/link visibility', 'Delete a shared session'],
  },
  {
    path: '/people/me',
    title: 'What agents know about me',
    audience: 'Anyone who talks to an agent',
    what: 'Everything canopy holds about you (the fleet brain): the work-context facts agents have recorded — role, projects, instances, preferences, corrections, terminology — grouped by the workspace they were written in, the digest agents are handed when you talk to them, and every recent time an agent or a person read it. Linked from Settings and from the `see_all` an agent can quote back to you.',
    needsFirst: 'An agent to have recorded something about you (it happens after you talk to one).',
    actions: ['Read every fact and digest held about you', 'Retract a fact that is wrong', 'See who read it, and when'],
  },
  {
    path: '/supervisor',
    title: 'Supervisor',
    audience: 'Someone who owns agents',
    what: 'The cross-fleet Waiting on you queue (every task with an open ask or parked on you, actionable in place), agent KPI cards and runner status. Spans every workspace, which is why it is not tenant-scoped — and why it lives in your account menu (under your name) rather than the workspace nav.',
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
    title: 'Your settings',
    audience: 'Everyone',
    what: 'Your own account and this deployment (the workspace\'s own configuration is at /w/:workspace/settings): which AI backend is in use, your Claude subscription connection, presence visibility, and your personal access tokens.',
    actions: ['Mint a personal access token', 'Revoke a token', 'Switch AI backend', 'Toggle presence'],
  },
  {
    path: '/new-workspace',
    title: 'Create a workspace',
    audience: 'A brand-new user with no workspace yet',
    what: 'The first-run screen: explains what a workspace is, then either offers a create form (address + optional display name) or explains you need an invite, depending on your account\'s create eligibility.',
    actions: ['Create a workspace (if eligible)'],
  },

  // --- Public viewers (under their workspace; self-enforce visibility) ---
  {
    path: '/w/:workspace/walkthrough/:id',
    title: 'Walkthrough viewer',
    audience: 'Whoever was sent the link',
    what: 'Plays a single shared walkthrough — an HTML slideshow or a video — with owner-only controls to toggle public/private, rotate the share link, or delete it. Deep-links to a scene (`#scene-N`) or a video timestamp (`#t=`) are supported. A signed-out visitor on a private link is routed to sign in rather than shown a bare error.',
    actions: ['Watch the walkthrough', 'Copy or rotate the public link (owner)', 'Toggle visibility (owner)', 'Delete it (owner)'],
  },
  {
    path: '/w/:workspace/review/:id',
    title: 'Narrative review',
    audience: 'Whoever the review request was addressed to',
    what: "The DDD narrative-review surface behind a review request: watch the cut, edit any scene's narration/features/personas inline, see grounding and gaps inline per scene, reorder the build sequence, and resolve the gate (approve / send back to redraft, or answer a findings review). External guests land here with a share token and can only suggest edits, never resolve the gate.",
    actions: ['Watch the cut', 'Edit narration, features or personas', 'Reorder the build sequence', 'Approve, redraft, or submit findings decisions'],
  },
  {
    path: '/beta-requests',
    title: 'Requests for access to Canopy',
    audience: 'The person access requests are emailed to',
    what: "Everyone who asked for access through the public site's form, newest first, with whether each was invited or declined. Nobody else can see them.",
    actions: ['Open a request'],
  },
  {
    path: '/beta-requests/:requestId',
    title: 'Request for access to Canopy',
    audience: 'The person access requests are emailed to (the link in the notification email)',
    what: 'One request: who asked and why. Approve by picking a workspace you administer and a role — Canopy emails them the ordinary workspace invite — or decline, which emails nobody.',
    actions: ['Approve and invite to a workspace', 'Decline'],
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
    path: '/w/:workspace/settings',
    title: 'Workspace settings',
    audience: 'Any workspace member (owner-only actions)',
    what: 'One page for how this workspace is configured, in four sections: Members, Slack, Inbound email and Connected sites. Your own account settings (AI backend, tokens, theme) are separate, at /settings.',
    actions: ['Open a settings section'],
  },
  {
    path: '/w/:workspace/settings/members',
    title: 'Members',
    audience: 'Any workspace member (owner-only actions)',
    what: 'Admin surface for who is in the workspace: the member list with roles, and pending invites — each invite is emailed and is also a copy-linkable `/invite/:token` URL. An invite grants the role chosen when it was sent (viewer unless you pick otherwise).',
    actions: ['Invite someone by email', 'Change a member\'s role', 'Remove a member', 'Revoke a pending invite'],
  },
  {
    path: '/w/:workspace/settings/access-requests',
    title: 'Access requests',
    audience: 'Workspace admins and owners',
    what: "People asking to be invited: anyone signed in with an address at one of the workspace's request domains can ask from the welcome screen, and every admin and owner is emailed a link to the request. Owners set whether requests are approved automatically (and at which role, viewer or editor) or each waits for a person.",
    actions: ['Review a request', 'Turn auto-approve on or off (owners)'],
  },
  {
    path: '/w/:workspace/settings/access-requests/:requestId',
    title: 'Access request',
    audience: 'Workspace admins and owners (the link in the notification email)',
    what: 'One request: who asked, their address and domain, their note, and when. Approve at a role you may grant (viewer by default) or deny with an optional reason — they are emailed either way. A decided request is read-only; if it was approved, their current role can be changed or they can be removed.',
    actions: ['Approve at a role', 'Deny with a reason', "Change an approved person's role", 'Remove them'],
  },
  {
    path: '/w/:workspace/settings/connected-apps',
    title: 'Connected sites',
    audience: 'Workspace owner embedding an agent in a website',
    what: "Which websites may host one of this workspace's agents: the URLs allowed to frame the widget, the agents each site may offer, and the keys a site signs its visitors with. A visitor who is a member of this workspace arrives as themselves; anyone else as a contact. Also the switch that puts the agent panel on canopy's own pages.",
    actions: ["Turn on the agent panel on canopy's own pages", 'Connect another site by URL', 'Choose which agents a site may offer', 'Disconnect a site'],
  },
  {
    path: '/w/:workspace/settings/secrets',
    title: 'Secrets',
    audience: 'Workspace owner',
    what: "Where this workspace's secrets live, stated whole: 1Password holds the secrets, a service account opens a vault, and canopy-web stores the service account so a runner can fetch it. This page owns the SHARED vault every agent here reads (the Google OAuth clients, the GitHub token); an agent's own vault is on that agent, under Settings → Credentials, and a box's own login is on the runner.",
    actions: ["Name the workspace's shared vault", 'Store or rotate its service account', "Jump to an agent's own vault"],
  },
  {
    path: '/w/:workspace/settings/retention',
    title: 'Retention',
    audience: 'Workspace admin deciding how long conversation content is kept',
    what: "How long this workspace keeps what was said: chat messages, attachments, and each turn's prompt, event log and raw transcript. Rules filter by kind (chat or turn), source (email, Slack, ace-web…), who started it (member, contact, agent, canopy) and agent, and keep matching content for N days or forever. This workspace's rules override those inherited from above. Any member can read the policy; admins change it, and every change is in the event log. A preview counts what the rules would delete without deleting it.",
    actions: ['Add, edit or remove a retention rule', 'Preview what the rules would delete', 'See the rules inherited from above'],
  },
  {
    path: '/w/:workspace/settings/inbound',
    title: 'Inbound push',
    audience: 'Workspace owner setting up email-triggered agents',
    what: "Configure the doorbell that lets an inbound Gmail message wake an agent: manage which mailboxes are watched, and generate the exact gcloud setup commands (project, Pub/Sub topic, service account) for the Gmail watch this depends on.",
    actions: ['Add or remove a watched mailbox', 'Enable/disable a mailbox', 'Copy the GCP setup commands'],
  },
  {
    path: '/w/:workspace/settings/topology',
    title: 'Fleet map',
    audience: 'Workspace admin or owner',
    what: "The fleet as one picture: this workspace and every one below it as nested boxes, each holding a lane per owner with that owner's runners and agents. Select an agent to see which runners it runs on, in order, and arrows to every agent it can send work to, coloured by whether it gets the whole agent or only some capabilities; select a runner to see which agents stop if it goes dark. Each workspace collapses. The panel says why an agent is confined and grants it from there. It is also where runners are configured: select a runner for its Claude login, admins, declared flags, drills, pause and retire; select an agent to change its runner order and source rules; each owner lane's Add a runner gives the command that pairs a new box into that workspace. Each workspace's Default runners line opens its default order: every agent there without runners of its own follows it live (after its own rules), as does every workspace below without its own; a laptop missing an agent's repo is skipped for that agent and offers the commands to get it there.",
    actions: ['Collapse or expand a workspace', "Select an agent to see its runners and who it can reach", "Change an agent's runner order and source rules", 'Select a runner to see who depends on it', "Set a runner's login, admins and flags; pause or retire it", 'Copy the command that pairs a new runner', "Set a workspace's default runner order", "Get an agent's repo onto a laptop that lacks it", 'Make one agent an admin of another', 'Switch to the Runners or Agent access table'],
  },
  {
    path: '/w/:workspace/settings/topology/runners',
    title: 'Runner topology',
    audience: 'Workspace admin or owner',
    what: "Which runner each agent's turns land on, across this workspace and every one below it: each agent's runners in order and its source rules, every runner they use with its status and who owns it, and which agents are unrouted or have no live runner. Flags a route whose runner can never claim (its owner is not in the agent's workspace). Read-only — routes are changed on each agent.",
    actions: ['See what each workspace runs on', 'Select a runner to see which agents stop if it goes dark', "Open a runner's supervisor page"],
  },
  {
    path: '/w/:workspace/settings/topology/agents',
    title: 'Agent topology',
    audience: 'Workspace admin or owner',
    what: "Which agent can send which other agent work, across this workspace and every one below it. An agent sends work with its own canopy login, and to the receiving agent that login is just a person — so each cell says what the sender gets (the whole agent, the capabilities its interface allows members, or nothing) and why. The receiving agent's owner or a workspace owner can make the sender's login an admin from the cell, one at a time or all at once; the grant says who could then steer the receiver through the sender.",
    actions: ['See which agents can reach which', 'Find out why a dispatch is confined or refused', "Make one agent an admin of another", 'Make every agent an admin of the others it can reach'],
  },
  {
    path: '/w/:workspace/settings/slack',
    title: 'Slack',
    audience: 'Workspace owner connecting Slack',
    what: "This workspace's Slack connection, and whether canopy manages the Slack app's slash commands — so that switching an agent on for Slack adds its /<agent> command, and switching it off removes it. Holds the app configuration token that allows that, encrypted and never shown.",
    actions: ['Connect or reinstall Slack', 'Let canopy manage the slash commands', 'Sync the commands now', 'Declare the app an agent (one-way)'],
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
    what: 'The list of first-class AI agents registered in this workspace, each card showing its status-report and skill counts.',
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
    path: '/w/:workspace/huddles',
    title: 'Huddles',
    audience: 'Anyone following how the fleet works as a team',
    what: 'Every huddle in this workspace — a team of agents syncing in rounds, led by one of them — with its type, team, leader, members, rounds dispatched and outcomes filed. Derived from turns; nothing is stored for it.',
    needsFirst: 'A team leader running `canopy huddle plan`.',
    actions: ['Open a huddle', 'See which huddles are still in flight'],
  },
  {
    path: '/w/:workspace/huddles/:id',
    title: 'Huddle',
    audience: 'Anyone following how the fleet works as a team',
    what: "One huddle as a conversation: a column per member, a row per round, each reply rendered as a bubble with the leader's critique above the round it shaped, co-sign arcs tying joint proposals to each partner's answer, and the board tasks it produced with their live status.",
    needsFirst: 'A huddle whose rounds have been dispatched.',
    actions: ["Read each member's reply", 'Open a round prompt or transcript', 'Follow an outcome to its board'],
  },
  {
    path: '/w/:workspace/agents/:slug/projects',
    title: 'Agent projects',
    audience: 'Anyone in the workspace',
    what: "Where an agent opens: the pieces of work with an end that it is working toward, one row per project (P1, P2 …) with its outcome, owner, open and waiting-on-you task counts, and when it last moved. Active projects first; done and archived collapse below.",
    actions: ['Open a project', 'Add a project (a name and the outcome that means done)', 'Show done and archived projects'],
  },
  {
    path: '/w/:workspace/agents/:slug/projects/:ref',
    title: 'Agent project',
    audience: 'Anyone in the workspace',
    what: "One project, always in four sections: its header (outcome, status, owner, Drive folder, repo), its tasks on the same card as the Tasks page, the recent turns that touched those tasks, and the links the agent recorded as its deliverables.",
    actions: ['Change the status, owner or outcome', 'Act on a task in place', 'Open a turn that touched the project', 'Follow a recorded link'],
  },
  {
    path: '/w/:workspace/agents/:slug/tasks',
    title: 'Agent tasks',
    audience: 'Anyone in the workspace',
    what: "Every task this agent has (T1, T2 …), filtered to Waiting on you (an open ask, or parked on you), Open, or Done, and optionally to one project or grouped by project. A task that asks you something says so on its card and offers exactly the actions that apply: approve or decline a review, answer a question, reply, and for editors dispatch or mark done. `?batch=` opens one sitting, e.g. a fleet audit.",
    actions: ['Approve or decline a review', 'Answer a question', 'Reply on a task', 'Dispatch or complete a task', 'Filter by project or group by project'],
  },
  {
    path: '/w/:workspace/agents/:slug/turns',
    title: 'Agent turns',
    audience: 'The person(s) who own this agent',
    what: "The agent's full turn ledger — every packaged unit of work it has done, with what it advanced, what it did, its deliverables, and optionally a transcript link.",
    actions: ['Browse past turns', 'Open a turn\'s transcript'],
  },
  {
    path: '/w/:workspace/agents/:slug/huddles',
    title: 'Agent huddles',
    audience: 'The person(s) who own this agent',
    what: 'The huddles this agent led or was a member of — each with its rounds, members and how many outcomes it filed.',
    needsFirst: 'A huddle this agent took part in.',
    actions: ['Open a huddle to read the conversation'],
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
    path: '/w/:workspace/agents/:slug/skills',
    title: 'Agent skills',
    audience: 'The person(s) who own this agent',
    what: "The catalog of skills this agent has available, as cards.",
    actions: ['Browse the agent\'s skill catalog'],
  },
  {
    path: '/w/:workspace/agents/:slug/skills/history',
    title: 'Agent skill history',
    audience: 'The person(s) who own this agent',
    what: "How each skill in the agent's repository changed over time, read from its git history. Drill from all skills, to a group, to one skill, to one commit.",
    actions: ['Scrub the timeline or play it back', 'Open a group, skill or commit', 'Sync from GitHub'],
  },
  {
    path: '/w/:workspace/agents/:slug/settings',
    title: 'Agent settings',
    audience: "The agent's owner and admins (some controls are workspace-owner only)",
    what: "Everything that configures this agent, grouped by the question it answers: who operates it (owner, admins), who can reach it (callers, Slack), how it runs (turn mode, runners and their source rules), and its credentials — the secrets it needs plus the 1Password vault and service account they are read from. The workspace's SHARED vault is elsewhere, under the workspace's own Settings → Secrets.",
    actions: ['Transfer the owner', 'Grant or revoke an admin', 'Publish what callers may ask for', 'Change turn mode', 'Turn Slack access on or off', 'Order the runners that execute its turns', "Set or rotate a secret", "Point the agent at its 1Password vault"],
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
    path: '/w/:workspace/share/:token',
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
  {
    path: '/about',
    title: 'About Canopy (public)',
    audience: 'Anyone, including people with no account',
    what: 'The public explainer — what Canopy is, the five components it is made of, live counts, and the five ways in. No login required, so this is the link to send someone outside the team.',
    actions: ['Send it to someone', 'See the live fleet counts'],
  },
]

export function describedPaths(): Set<string> {
  return new Set(SURFACES.map((s) => s.path))
}
