# Pages: workspace settings, sessions, health and the rest

The tenant-scoped pages for workspace settings, sessions and chat, huddles, threads, schedules, activity, runner invitations, members, sign-offs, health, analytics and network, shareouts, walkthroughs, storyboards and DDD. Part of the [pages](pages.md) topic; the index there lists every part.

- **`/settings`** — the workspace's settings: one page, tabbed sections, each
  its own URL (`/settings/<section>`, index → `members`). Tabs because each
  section fetches its own data and one-way actions belong on a section opened
  on purpose. Gates per section follow `apps/workspaces/permissions.py` (see
  [`roles.md`](roles.md), [`access.md`](access.md)).
  - **members** — members + invites: remove, change role (admins manage
    viewers and editors; only owners make admins/owners), invite by email at a
    role (viewer default). The `/invite/:token` link is emailed via SES
    (`apps/common/email.py`, `DEFAULT_FROM_EMAIL`, configuration set
    `labs-jj-email`, on only with `CANOPY_EMAIL_ENABLED`); when it is not sent
    the page says so and leaves the link to copy. Pending + expired invites:
    copy, resend (a fresh link; the old one dies; revives an expired invite),
    revoke. Links to member-tree.
  - **member-tree** — "Members across workspaces": this workspace and every
    workspace below it the viewer is in, as a matrix (row per person, column
    per workspace in tree order, the role in each; `GET
    /api/workspaces/{slug}/member-tree`). Owners and admins of an ancestor show
    read-only as "inherited from <ancestor>"; editors and viewers do not
    inherit, so an empty cell is a person who is an outsider there. Where the
    viewer has `members.manage`, each cell is a role picker limited to what
    they may grant: a role in an empty cell adds someone already in the tree
    (`POST /api/workspaces/{slug}/members/`, no invite), a new role PATCHes, "—"
    removes after a confirm. Errors show on the cell. Filter by email, "only
    show gaps".
  - **connections** — the workspace's OAuth apps (one per provider, Salesforce and Google):
    client ID, a write-only secret, the extra permissions to ask for, and the callback URL to
    register (`apps/connections`, [`vault-connections.md`](vault-connections.md) § OAuth connections are sealed to their owner).
  - **access-requests** (+ `/:requestId`, the page the admins' notification
    links to) — requests to be invited; approve at a role or deny.
  - **slack** — the workspace's Slack connection (one Slack may serve several
    workspaces; a message's tenant is decided by its thread or agent —
    `docs/superpowers/specs/2026-09-22-slack-multi-tenant-design.md`), the app
    configuration token canopy uses to manage `/<agent>` slash commands, and the
    one-way, owner-only "declare the app an agent".
  - **inbound** — the Gmail push doorbell.
  - **email** — the workspace's own mail domain via SES: DNS records, per-address routes (agent / member forward / drop; the catch-all forwards or drops), daily send cap. Removing an address or the domain asks first and says existing threads are kept.
  - **host-sites** ("Host sites") — which websites may host one of
    this workspace's agents and which one its panel opens on ("Opens on"); per
    site, what it asks of agents (its signed
    `site.json`): per allowed agent, whether the site offers it, lets it work
    with its sensitive data, and — only then — whether it can handle that data,
    with why not and a link to fix it (marked only when an offered agent cannot);
    the site's pages + scopes sit collapsed, for reference.
  - **secrets** — the workspace's SHARED vaults (what every agent here
    reads, e.g. the Google OAuth clients), and the one screen that states the
    credential model whole: an agent's own vault is on the agent (Settings →
    Security), a box's Claude login is on the runner (`/supervisor` →
    Runners), GitHub is the owner's identity lent per agent.
  - **tasks** — how the workspace's tasks read: key prefix (changeable only
    while no task carries it), a name per status category, the project update
    cadence, and the labels (`WorkspaceTaskSettingsPage.tsx`). Editors change it.
  - **retention** — content-retention rules.
  - **admission-rules** — turns the workspace refuses before they run.
  - **roles** — the agent roles a line can give, in words: the built-ins (editable, never deletable) and the ones this workspace defines; owner and agent admin read-only.
  - **topology** — the fleet map (`frontend/src/pages/topology/`): this
    workspace and every one below as nested, collapsible boxes, a lane per
    owner with their runners and agents. Select an agent for its runners in
    order and arrows to every agent it can send work to; select a runner for
    who stops if it goes dark. With nothing selected, the side panel shows the
    workspace's alerts ("Alerts here", `workspace:<slug>`: an agent with no
    runner it can use, or none of whose routed runners declares its repo, is an alert —
    [`alerts.md`](alerts.md)) above two plain facts: agents confined by another
    (to read mode or host sites), and runners serving nobody. The side panel mounts the supervisor's
    `RunnerDetail` and the agent's `AgentRouting` — this is where runners are
    configured on the web; each lane's "Add a runner" offers the Canopy Runner
    downloads (as on `/get-started#runner`), with the developer command
    (`install-runner.sh --workspace <ws>`) folded under "Developers". Drawn from the same two endpoints as
    its table views, so the picture cannot disagree with them; an arrow to an
    agent in a collapsed box lands on the box. Routing semantics (cascade,
    workspace default order): [`runners.md`](runners.md).
    - **topology/runners** — read-only runner topology of the subtree
      (`GET /api/workspaces/{slug}/runner-topology`, `apps/harness/topology.py`):
      each agent's ordered runners and source rules, unrouted agents and agents
      with no live runner, what stops if a runner goes dark, and routes whose
      runner's owner is not in the agent's workspace (they can never claim).
      Its runner table is Health's (`components/runners/RunnersTable.tsx`);
      opening a row also highlights the agents that route to it.
      Routes are edited on the agent. Admin and above (`logs.read`), checked on
      every descendant.
    - **topology/agents** — agent-to-agent access matrix over the subtree
      (`GET /api/workspaces/{slug}/agent-topology`, `apps/agents/topology.py`):
      what each agent gets when it sends another work under its own login
      (`Agent.user`) and why. The receiver's owner or a workspace owner grants
      from the cell (`PUT /api/agents/{slug}/admins/{user_id}`, pinned to the
      receiver's workspace); the grant names who could then steer the receiver
      through the sender, since admin is transitive. Same gate as runners.
    - `settings/runners`, `settings/agent-access` redirect to these.
  - `/w/:ws/{members,inbound,slack,host-sites}` redirect to their section,
    keeping query and hash. Distinct from root `/settings` (your account).
- **`/sessions`** (header: Sessions) — this workspace's sessions (and those of the
  workspaces below it) to continue from any device + "New chat with `<agent>`"
  (`ChatSessionsPanel` on the pinned `/api/w/{ws}/canopy-sessions/`; the
  supervisor's Sessions screen embeds it across every workspace). `/chat` and
  `/chat/:id`, the old address, redirect here, keeping query and hash. Coming back to the list paints the tab's
  last copy at once and refreshes underneath; the rows never wait on the
  fleet or agent reads, which only feed "+ New chat". Listed rows open
  without the private-conversation read, as /ide's do. **Started by me**
  (remembered per browser) keeps only your own sessions — the ones you started
  and those found on your own box, `SessionOut.started_by_you`
  (`access.is_own_session`, the one-row form of `access.sessions_of_q`).
- **`/sessions/:id`** — live multiplayer chat with an agent on the `canopy-ui/chat`
  kit (`ChatPanel` + `useSessionSocket` over `ws/canopy-sessions/{id}/`;
  per-person drafts, presence, streamed reply). A send enqueues a session
  `Turn` that a session-capable runner drives in the agent's emdash session.
  `docs/superpowers/specs/2026-07-22-reusable-chat-kit-design.md`.
  The ⋯ menu offers the session's own person "Ask <coach> to review this" (`SessionReview.tsx`):
  what it does and the press, or plainly why not, or once asked, the chat where it replies and
  Withdraw (access-sessions-and-people.md § Asking a coach to review one session).
  A PRIVATE (sensitive) conversation opens behind an interstitial
  (`PrivateConversationGate`, also /ide's pane): one of its people presses
  "Open it" (remembered for the tab); anyone else who can see it may only ask
  its starter to add them. The request shows IN the conversation, never on Home:
  "<who> asked to join this conversation · [Add them] [Decline]" for its owner,
  read-only for its other people, on the chat page, /ide's pane and the embedded
  panel (`JoinRequests`, live over the socket's `session.join_requests`); Add
  them is the browser's re-wrap of the key, and the asker's interstitial turns
  into "Open it" when it lands. Every list names one "Private conversation ·
  <starter>" with a lock (`components/chat/privateConversation.tsx`).
  Opening a chat paints before its socket's snapshot (the slow part): from the
  tab's last copy of it (`pages/chat/sessionCache.ts`, kept for the life of the
  tab), else from the REST detail's tail; the snapshot then replaces either. One
  a list already named as ordinary (/ide's) skips the gate's read. A private
  conversation is never cached or painted early. The chat reuses a fleet read
  under a minute old (`recentRunners`) and shares one slash-command list per
  agent, runner and role.
  The ⋯ menu's "Delete this session's content" (shown when `can_delete_content`) asks in a plain confirm that it
  can't be undone, then goes back as Close does; a refusal shows beside the menu. A deleted session stays
  openable, empty, with its composer refused (tenancy-session-access-and-retention.md § Deleting your own session's content).
- **`/huddles`** (+ `/:id`) — agents syncing in rounds, led by one of them.
  The detail page is a grid (column per member, row per round; leader's
  critique above the round it shaped; co-sign arcs tying joint proposals), then
  the board tasks it produced. Derived, nothing stored (`apps/huddles`,
  `GET /api/huddles/`); content gated per round turn by `turn_access`. Also an
  agent rail section. `huddles/groups/:slug` (a work group's old page) redirects to the
  project check-in it became (matched on `legacy_slug`), else to `/huddles`.
- **`/threads/:id`** — a direct, bounded agent-to-agent conversation: purpose,
  participants, how it ended, each message. Frame + limits stored in
  `apps/threads`; messages are tagged turns, refused at enqueue by
  `apps/threads/guard.py` when the thread is closed/expired/out of budget, the
  speaker is wrong, or `n` is out of order. A huddle page shows its threads as
  "Settling changes" and counts a settled-agreed thread as changes agreed.
- **`/schedules`** — weekly calendar of the workspace's recurring schedules
  (tenant twin of root `/schedules`).
- **`/activity`** — the fleet turn log scoped to this workspace.
- **`/timeline`** — team activity feed across apps (link-out only).
- **`/runner-invitations/:invitationId`** — approve a new cloud runner: the link
  `runner/ec2/up.sh` prints and the invitation email carries. Shows the runner's
  name, the workspace, the EC2 instance role, who asked and when it expires (an
  hour), and one Approve button; the passkey step-up is the API client's
  (`auth/stepUp.ts`). Only the invitation's creator can open it (404 otherwise)
  or approve it, and a token cannot (`apps/harness/api_runner_invitations.py`,
  `docs/architecture/runner-credentials.md` § How a runner signs in).
- **`/members/:userId`** — one member: role, when they joined, person or agent, the agents they
  operate here and the other workspaces you share with them; admins and owners also see who
  invited them and when they last signed in.
- **`/sign-offs/:signOffId`** — a host site asking you to approve one action
  with your passkey (labs creating a full-access MCP token): the `approve_url` the
  site sends you to, also on Home. Shows the site, the action in its words, and the
  account it is for (an agent's request names the agent). Approve asks for your
  passkey (`auth/stepUp.ts`) and sends your browser back to the site with the signed
  approval; Decline sends it back without one. Only the person it is for, or the
  agent's owner and admins, can open it (404 otherwise); a token cannot approve
  (`apps/host_grants/sign_offs_api.py`).
- **`/health`** — admin+ (`logs.read`, per descendant). What is not set up right
  across this workspace and every one below it (`GET /api/workspaces/{slug}/health`,
  `apps/agents/health.py`): every agent with its setup checklist (the SAME steps
  as its Settings page), its vault state (canopy-web / moving / none,
  `apps/agents/credentials/vault_health.py`) and properties — active projects, open tasks
  (`AgentTask.LIVE_STATUSES`) and those waiting on a person (`services.waiting_q`),
  each linking to the agent's page; who can use it (its owner and agent admins only, or the
  read mode and host sites it offers) and its turn mode; how it is used over
  30 days beside its setup, since an agent with steps left can be in use — turns
  started and how many failed, the people who used it, the runners its turns ran
  on (`analytics.agent_turns`, from the fact log), and a "Used" tile; open sessions and
  last session activity (`staleness.active`, `transcript_read.with_activity`) with its
  latest sessions the viewer may open (`access.readable_sessions`); every
  workspace's shared vault; the runners its agents route to or that live in it
  (the topology's rows: status, whether it runs current code, its own failing
  checks, how many agents it serves, when it last sent its usage report, and how
  many of its local sessions canopy has no record of — its admins open the list,
  ask for a rescan and send chosen sessions up). That table is THE runner table,
  shared with Topology → Runners; its checks column counts failing and warning
  checks. A row opens a read-only summary that leads with "Manage runner" — the
  supervisor's runner page, the one place a runner is changed — then why the box
  is in its state in its own words, the fix for a known cause (`Runner.remedy`),
  its code against the expected sha, every check's detail with a "Fix →" (an
  agent's GitHub token on that agent, anything else on the runner page) and the
  agents it serves.
  Every figure opens its source: a tile filters the agents table, a count opens
  the agent's page for it, a cell opens the setting it shows.
  An agent's row opens its steps — linked when the viewer is its owner or an admin
  (`can_fix`), else an email to the owner listing what is left. Renders, never decides.
- **`/analytics`** — the second tab of Health (same gate). Turns, how they ended,
  chats, messages, active people, tokens (cache reads apart) per UTC day over
  7/30/90 days, where turns came from, and one table each for agents,
  repositories (work no agent ran) and people — no cost
  (`GET /api/workspaces/{slug}/analytics`, `apps/events/analytics.py`) —
  see `docs/architecture/analytics.md`.
- **`/network`** — the third tab of Health (same gate). Every person, contact and agent
  as a dot, linked to each thread they wrote in (a chat — a bound Slack thread is one —
  or an email thread) and each thread to its answering agent, over 7/30/90 days; totals
  of threads with 2+ people and of pairs who wrote in the same thread. Colour by who,
  channel or workspace; group by workspace. Click a dot for its threads in a side table;
  a thread opens (and shows its title) only if the viewer may open that chat
  (`GET /api/workspaces/{slug}/network`, `analytics.network`).
- **`/shareouts`** (+ `/:period`, a permalink to one briefing) — dated
  teammate-facing work briefings posted by `/canopy:shareout`.
- **`/walkthroughs`** — demos uploaded from `/canopy:walkthrough`.
- **`/storyboards`** — the workspace's storyboards: title, layout
  (review/reel), act count, and the token-bearing share link. The board opens
  at `/storyboard/:slug`.
- **`/ddd`** (+ `/:narrative`, `/:narrative/:runId`) — the DDD operator
  console: narrative → version → run → package (video + deck + narrative +
  links).
- **`/marketplace.json`** (+ `/plugins/…`) — retired server views: **410**
  naming the one personal catalog, `/plugins/marketplace.json`
  ([`plugin-marketplace.md`](plugin-marketplace.md)).
