# Pages: every SPA/server page, what it is for, and how it behaves

Every browser-facing address canopy-web serves. The SPA route table is
`frontend/src/router.tsx` (`routeTable`); server-side pages are in
`config/urls.py`. API routes are in [`api-surface.md`](api-surface.md).

## Rules for every page

- **Tenant data lives under `/w/:workspace/`.** Personal/global surfaces, the
  pre-tenant pages and redirects stay at root. Every route outside
  `/w/:workspace` carries ITS REASON — a page's `flat` in
  `frontend/src/guide/surfaces.ts`, a redirect's entry in `guide/coverage.ts`
  `NOT_PAGES` — checked by `frontend/src/routeTenancy.test.ts`; a flat route
  holding tenant data (a "FOLLOW-UP") fails it.
- **One URL per artifact, nothing forwarded.** The flat `/walkthrough/…`,
  `/review/…`, `/share/…`, `/storyboard/…`, `/narrative/…`, `/ddd-release/…`
  and the pre-tenancy `/w/<uuid>/…` are a plain server 404, reachable signed
  out, saying links now carry the workspace (`config/views.py::flat_artifact_gone`, matched by
  `FLAT_ARTIFACT_PATH` before the SPA catch-all). Build links with
  `scoped_path` / `scoped_path_or_none` (`apps/workspaces/links.py`) or
  `scopedPath` / `useScopedPath` (`frontend/src/lib/scopedLinks.ts`; a hand-built
  `/w/${…}` fails `frontend/src/scopedPathLiterals.test.ts`); neither has a flat
  fallback — a row with no workspace has no address (`Unhomed`). Every URL
  field the API returns is under `/w/<ws>/`
  (`tests/test_api_urls_are_workspace_scoped.py`). `manage.py
  rewrite_flat_links` (`apps/walkthroughs/flat_links.py`) rewrites a stored
  flat link to its scoped address.
- **Anonymous visitors reach only the scoped viewers**: `/w/<ws>/{walkthrough,
  review,share,storyboard,narrative,ddd-release}/…` plus the walkthrough
  content stream (`apps/common/middleware.py::_SCOPED_VIEWER` — the trailing
  slash keeps the plural list pages `/walkthroughs`, `/storyboards` behind
  login), the plugin catalog (self-enforced 401; old per-workspace URLs 410), and the flat-artifact 404.
  Every other `/w/` path is the authed tenant shell. Each viewer passes `ws` so
  the API 404s a row from another workspace.
- **One registry of pages: `frontend/src/guide/surfaces.ts`.** Every route is
  a descriptor there or a non-page (redirect, catch-all) in `guide/coverage.ts`
  `NOT_PAGES` with its reason; `guide/coverage.test.ts` fails on either missing.
  A descriptor says where the page lives — `home` (the page it is reached from:
  the guide files it under that page's menu, and the workspace switcher lands
  there when its section has no page), `group` (the guide heading for a page in
  no menu), `flat` (why it is outside `/w/:workspace`). The guide's grouping,
  the switcher's fallback (`nav.ts` `LIST_FOR`) and the tenancy test read these
  fields; the menus themselves are `NAV`, `HELP_ITEMS` and `ACCOUNT_ITEMS` in
  `components/AppLayout/nav.ts`, and a page in neither a menu nor a group fails
  `guide/grouping.test.ts`.
- **Adding a page:** add it to `routeTable` and give it a descriptor (with
  `flat` if it is not under `/w/:workspace`, and `home` or `group` if no menu
  links it). Every route gets an error boundary
  automatically (`guarded()` in `router.tsx`); a route needing a different one
  sets its own `errorElement`.
- **One editor per object.** A core object people configure — an agent, a
  workspace's settings, a runner, a vault — has ONE page at ONE address, built
  from the shared kit in `frontend/src/components/objectPage/`: `ObjectPage`
  (name, status, one line, actions), `ObjectTabs` (the one routed tab row; a tab
  that needs attention carries a dot) and `Section`/`Setting`
  (`settingsLayout.tsx`). Each tab is a route (`…/:tab`), so a deep link names a
  place and a tab loads only its own data. Every other surface LINKS to that page
  (`runnerHref`, `vaultHref` in `lib/scopedLinks.ts`; `alerts_runner_credential.runner_path`
  server-side) or embeds one of its read-only pieces (the fleet map shows
  `RunnerFacts`) — it never rebuilds the object's controls. An object page in
  the agent rail (Agent settings) renders `ObjectTabs` inside the rail's section.
- **Retired pages redirect, keeping query and hash** where a link may carry
  one (`SettingsRedirect`, `KeepQueryRedirect`, `CredentialsRedirect`). Retired
  ARTIFACT addresses do not — see above.
- A header workspace switcher appears when you belong to more than one
  workspace. A user with no workspace sees `FirstRunPage` instead of a blank
  redirect.

## Tenant-scoped (`/w/:workspace/…`)

- **`runners/:runnerId/:tab`** — a runner's one page (`pages/runner/`): Overview
  (status, facts, alerts, health, readiness checks), Credentials (a cloud box's
  Claude logins, tokens and API keys and its vault — only for its administrators),
  Routing (workspaces it serves, agents it takes, a laptop's repos — add one, see runners.md § Adding a repo to a laptop — each agent's routing), Access
  (administrators, private), Settings (name, pause, sessions, flags, egress, runtime,
  retire). Reads `GET /api/harness/runners/{id}/`; a link naming another workspace
  forwards to the runner's own. `/supervisor?tab=runners&runner=<id>` (old mail and
  alerts) forwards here.
- **`vaults/:vaultId`** (`/:tab` kept for old links) — a workspace vault's one page
  (`pages/vault/`), drawn by the shared **`VaultView`**
  (`components/vault/VaultView.tsx`, the same frame as an agent's vault): a summary
  line (where it lives, secrets / people / machines, what needs you), then
  collapsible sections — **Secrets** (names only; a value is sealed in the browser),
  **Who can open them** (people with why each can, and the runners that read it) and
  **Storage & key** (its key, made with a passkey touch, copies for whoever waits,
  and what it serves). The old tab addresses open the section that tab became.
  Settings → Secrets lists the workspace's vaults and links here.

- **The header** (`frontend/src/components/AppLayout/nav.ts`): **Home** and
  **Apps** (installed apps and the store, `/w/:workspace/apps`) are links;
  **Agents** (All agents, Sessions, New agent, Huddles, Schedules, Turn log,
  Runners), **Projects** (All projects, Tasks, Triage, and a **Demos**
  sub-menu: Storyboards, Walkthroughs, DDD runs, Shareouts) and **Workspace**
  (Members, Health, Timeline, Views, Settings) are menus; a project's views and
  MCP server are on its own page, and the MCP list is linked from Projects;
  every menu entry is scoped to the active workspace; a sub-menu is a `group` on its
  items; Guide, Get started and
  System sit behind `?`; personal pages (account settings, `/people/me`,
  `/sessions`, `/supervisor`, `/ide`) in the account menu. The active entry is
  the longest matching href, so Runners (`settings/topology`) and not Settings
  lights on the topology pages.
- **`/w/:workspace`** → redirects to `/w/:workspace/agents`. Tasks and projects
  are the workspace's (`docs/architecture/work.md`).
- **Anywhere in the app**: **⌘K** opens the command palette (pages, projects,
  agents, tasks by title or key, New task / New project / My tasks;
  `components/work/CommandPalette.tsx`), and **?** lists every keyboard shortcut.
- **`/tasks`** — every task in the workspace as one dense table
  (`components/work/TasksView.tsx`, one `TaskTable` row everywhere): tabs My tasks
  (`assignee=me`), Waiting on me (`waiting=me`), Created by me, All; filters for
  category, assignee, project, label, priority, kind, due and text, group and
  sort, and layout Table / Board (by status, drag to change status or order) /
  Calendar (by due date) — all of it in the URL, and saveable as a view (yours,
  or shared by an editor). Status, priority, assignee and due are edited in the
  row. `X` or a checkbox selects; the bulk bar changes many at once. `J`/`K` move,
  `Space` opens the task beside the list (`?peek=<KEY>`), `C` makes a task.
- **`/tasks/:key`** — a task's one page (`TaskDetailView`; the peek is the same
  component beside a list): properties edited in place, what it asks you (Start
  now · Accept · Backlog · Snooze · Decline), the people's own words, the plan,
  subtasks, relations, the assigned agent's current turn, and its history with
  the reply box.
- **`/triage`** — what waits for a decision (`GET /api/triage/`): suggested tasks,
  ideas and proposed projects, oldest first; `A` accept, `S` start now, `B`
  backlog, `D` decline, `H` snooze, `M` duplicate on the focused row.
- **`/projects`** — every project in the workspace as a row (status, health or
  Update missing, lead, lead agent, agents, progress, open and waiting, target,
  updated; `GET /api/projects/`). Open by default; **New project** for editors.
- **`/projects/:handle`** — a project's ONE page, for teammates and editors alike
  (`pages/work/ProjectPage.tsx`): properties (status, health, lead, lead agent,
  agents, dates, priority), its latest update with Post update and drafts to
  publish, milestones with progress, the scope-versus-done graph, its tasks, its
  dynamic views drawn in their sandboxes (the first three; hidden while there are
  none and the viewer cannot make one), then its proposal card, proposed tasks,
  ideas and people, check-ins and recent turns.
- **`/views`** (+ `/:id`) — the workspace's dynamic views, generated screens over
  its data, each drawn in a sandbox (`DynamicViewsPage.tsx`, `DynamicViewPage.tsx`;
  [`dynamic-views.md`](dynamic-views.md)). The list (`?project=<handle>` narrows,
  `?new=1` opens New view); one view, with History (restore) and, for editors, the
  code and sources beside a live preview of the draft (`?edit=1`).
- **`/projects/:handle/kb`** — a knowledge base's sources and version log
  (`GET …/kb/sources`, `GET …/kb/versions`, `KnowledgeBasePage.tsx`), read-only:
  each version its steward published, its counts, size, publisher and which one is
  served. Anyone who may read the project sees it. Linked from a KB project's page.
- **`/projects/:agent/:ref`** and **`/agents/:agent/projects/:ref`** → redirect
  to the project's one page, `/projects/:handle` (`/people/:person` is the
  contributor page).
  **An idea is a task with `kind: "idea"`** — never inferred; `work` is the
  default — and it must carry a person's own words (at least one `human_why`,
  enforced on create and patch). The work toward it is its subtasks. Its stage
  is its status (suggested → proposed, in progress → started, done → shipped);
  subtasks finishing never ship it — its owner or its person marks it done (the
  person may, on `act_on_task`). Every tally on every page is a count over
  `ideas.of_project` (top-level idea tasks), so the views cannot disagree.
- **`/agents`** — the workspace's agents as a table (`GET /api/agent-roster/`): last turn and whether it failed, turns and failures in 7 days, open and waiting tasks, active projects, mode, owner; editors get **New agent**.
- **`/agents/new`** — create an agent (`NewAgentPage`). Name, slug, mandate,
  optional mailbox; then the GitHub owner + repo name and a fine-grained token,
  with links to GitHub's new-repo and new-token forms filled in (the token steps
  are `GitHubTokenSteps`, shared with Settings → External accounts → GitHub). Checked live
  (`POST /api/agents/scaffold/check`) and on tab focus. A repo with an earlier
  attempt at this agent (or just GitHub's README) is completed, not refused: only
  missing template files are added. Another agent's repo offers its slug ("Use 'x'"). **Create** pushes the
  `canopy-agent-factory` scaffold with that token, registers the agent through the
  ordinary upsert (the creator becomes owner), saves the same token as its GitHub
  identity, and lands on Settings `?created=<repo>#setup` with a confirmation.
  Persona and first skill are Claude Code work, handed over as a command by the
  checklist. Viewers see a note instead of the form.
  An agent's Settings ends with **Delete** (editors and above, `DELETE /api/agents/{slug}/`):
  type the slug to confirm; it removes the agent from canopy and leaves its repo alone.
- **`/agents/:slug`** — the agent workspace: full-bleed rail + scrolling main
  on `canopy-ui`. Above every section, the open alerts about this agent
  (`AlertList scope="agent:<slug>"`, nothing when there are none —
  [`alerts.md`](alerts.md)). Rail sections, in order:
  - **Projects** (landing; index redirects here) — the projects table, filtered
    to the projects this agent leads or works on; "Add project" makes one through
    the agent's route (it carries the agent's `P<N>`). A project opens on its one
    page, `/projects/:handle`. **Contributor** (`projects/:ref/people/:person`) —
    one person's words and ideas on the project.
  - **Tasks** — the Tasks list pinned to this agent (`agent=<slug>`: the tasks it
    is assigned, and the ones it filed that no agent is), tabs All and Waiting on
    me; `?batch=<key>` for one sitting.
  - **Turns** — packaged units of work + optional transcript, and the
    **Status reports** section (`#status-reports`).
  - **Triggers** (`triggers`) — what starts the agent on its own: schedules (cron
    or one instant) and inbound mail (aligned mail from a bound sender, e.g.
    CloudWatch alarms), one table with who set each up (it fires with their
    authority) and, when that person lost the right, that it stopped and why;
    create, edit, pause, delete, run a schedule now (access.md § Triggers).
  - **Huddles**.
  - **Mail** (`mail`) — read-only email threads for the agent
    (`GET /api/mail/threads/{workspace}?agent=`): thread table beside the open
    thread; HTML bodies render only in a `sandbox=""` iframe behind a no-network
    CSP, meta refreshes stripped; attachments are
    presigned download links. No address → points to Settings → Email.
  - **Skills** — catalog, and **History** (`skills/history`): how each skill
    changed, read from the agent repo's git history with the owner's GitHub grant.
  - **Capabilities** (`capabilities`) — the cloud capabilities the agent
    declares (`GET /api/agents/{slug}/capabilities`): kind, description,
    region/stack or pool slots, effective limits (manifest lowered by the
    grant), the grant (who, when, its limits) and live leases; retired names
    collapse below. Workspace admins (`integrations`) get Grant / Limits /
    Revoke (`PUT /api/capabilities/grants`, `DELETE /api/capabilities/grants/{id}`);
    a grant may only lower the manifest's limits and the server's refusal is
    shown verbatim. Everyone else reads it with a line saying an admin grants.
  - **Settings** (`settings/<tab>`) — everything that configures the agent,
    one tab per question (`pages/agents/settingsTabs.ts`); bare `settings`
    and every old `settings#<anchor>` link forward to the tab holding the
    anchor, and a tab with an open setup step carries a dot. **Overview**
    (setup checklist, persona, where it runs — turn mode + runners and source
    rules, see [`runners.md`](runners.md), and the repository — delete);
    **Roles** (owner + one roster of every member's role on the agent, why,
    and what they can reach — `GET /api/agents/{slug}/access`; admins
    granted/revoked on the row); **Channels** (callers, Slack, email, Google
    account); **External accounts** (GitHub delegation, the
    Google mailbox); **Security** — first **Vault**: the shared `VaultView`
    (`components/vault/`, adapter `pages/agents/AgentSecretsPanel.tsx`) — a summary
    line, then **Secrets** (open; every secret in `VaultEditor`
    (`components/vault/VaultEditor.tsx`, a table you Change / Add / Reveal /
    Delete in place (a value can be a file from disk, e.g. a service-account
    JSON), owner-agnostic so other vault pages reuse it; Reveal decrypts locally with the person's vault key, and Save encrypts
    locally once the agent has one), **Who can open them** (admins: the vault's
    people with why each can — `docs/architecture/vault.md` — and the machines
    with approve / revoke), **Storage & key** (the Canopy vault, key copies, zero-knowledge grant / rotate / recover
    with code, every encryption in the browser, the one-time recovery-code
    dialog, the stated trust caveat) and, only when there are any, **Read
    alerts**. Sections other than Secrets start closed, open by themselves when
    something needs the reader, and open for an old `settings#cred-…` address.
    Then **Canopy login**, what is about the agent and not the vault: its own
    sign-in to canopy (made in one click, stored in the vault; renews itself, with
    one button to make a new one now) and, closed, the test vault key
    (`pages/agents/AgentCanopyLogin.tsx`, `AgentSecurityPanel.tsx`). The Google mint (`Connect Google mailbox`)
    asks for Gmail only when the agent's address is a Google mailbox; for a
    canopy-hosted address it asks for Drive, Docs, Sheets, Slides, Forms and
    Calendar. The consent must come from the agent's `google_account`, else
    from `agent.email` when that is a Google mailbox (else
    `?google=wrong-account&account=<x>&expected=<y>`, nothing stored); a
    `zk-no-recipients` result links to `/settings#vault-key`.
  - **Connect your Google account** (`connect/google`, `?services=gmail,calendar`
    preselects, `?account=` prefills) — one of the agent's admins connects their
    OWN Google account (a personal Gmail) for the agent to use on their behalf:
    they name the account, choose the services, press Connect, sign in at Google
    and come back here (`?google=<status>&mint=<id>`, followed to its end). The
    runner seals it as a credential they delegate (`google-delegated-<account>`,
    `POST|GET /api/agents/{slug}/google/delegate`, `vault.md` § Connect your own
    Google account); it is listed and withdrawn at `/settings/agents` → Your credentials
    delegated to agents. A non-admin is refused 403 `not_an_admin`.
  - The setup checklist (`apps/agents/setup.py`, `GET …/setup`) on Settings: a
    zero-knowledge agent's vault step comes before its mailbox, and is todo
    while nobody can open the vault. When no runner can take the agent, the
    runner step also offers its admins "Pair your laptop" (`/get-started#runner`)
    and "Stand up a cloud box" (`runner/ec2/README.md`).
  - Redirects (query kept): `work`, `items` → `tasks`; `inbox`, `needs-you` →
    `tasks?waiting=me`; `overview`, `work-products` → `projects`; `syncs` →
    `turns#status-reports`; `history` → `skills/history`; `credentials` →
    `settings#credentials` (the Google mailbox mint returns with
    `?google=ok#credentials`).
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
  - **access-requests** (+ `/:requestId`, the page the admins' notification
    links to) — requests to be invited; approve at a role or deny.
  - **slack** — the workspace's Slack connection (one Slack may serve several
    workspaces; a message's tenant is decided by its thread or agent —
    `docs/superpowers/specs/2026-09-22-slack-multi-tenant-design.md`), the app
    configuration token canopy uses to manage `/<agent>` slash commands, and the
    one-way, owner-only "declare the app an agent".
  - **inbound** — the Gmail push doorbell.
  - **email** — the workspace's own mail domain via SES: DNS records, per-address routes (agent / member forward / drop; the catch-all forwards or drops), daily send cap. Removing an address or the domain asks first and says existing threads are kept.
  - **connected-apps** ("Connected sites") — which websites may host one of
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
  - **topology** — the fleet map (`frontend/src/pages/topology/`): this
    workspace and every one below as nested, collapsible boxes, a lane per
    owner with their runners and agents. Select an agent for its runners in
    order and arrows to every agent it can send work to; select a runner for
    who stops if it goes dark. With nothing selected, the side panel shows the
    workspace's alerts ("Alerts here", `workspace:<slug>`: an agent with no
    runner it can use, or none of whose routed runners declares its repo, is an alert —
    [`alerts.md`](alerts.md)) above two plain facts: agents held to some
    capabilities by another, and runners serving nobody. The side panel mounts the supervisor's
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
  - `/w/:ws/{members,inbound,slack,connected-apps}` redirect to their section,
    keeping query and hash. Distinct from root `/settings` (your account).
- **`/sessions`** (header: Sessions) — this workspace's sessions (and those of the
  workspaces below it) to continue from any device + "New chat with `<agent>`"
  (`ChatSessionsPanel` on the pinned `/api/w/{ws}/canopy-sessions/`; the
  supervisor's Sessions screen embeds it across every workspace). `/chat` and
  `/chat/:id`, the old address, redirect here, keeping query and hash. Coming back to the list paints the tab's
  last copy at once and refreshes underneath; the rows never wait on the
  fleet or agent reads, which only feed "+ New chat". Listed rows open
  without the private-conversation read, as /ide's do.
- **`/sessions/:id`** — live multiplayer chat with an agent on the `canopy-ui/chat`
  kit (`ChatPanel` + `useSessionSocket` over `ws/canopy-sessions/{id}/`;
  per-person drafts, presence, streamed reply). A send enqueues a session
  `Turn` that a session-capable runner drives in the agent's emdash session.
  `docs/superpowers/specs/2026-07-22-reusable-chat-kit-design.md`.
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
  `docs/architecture/runners.md` § How a runner signs in).
- **`/sign-offs/:signOffId`** — a connected site asking you to approve one action
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
  each linking to the agent's page; who can use it (admins only, or the
  capabilities its interface publishes) and its turn mode; how it is used over
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

### Scoped public viewers (anonymous with a token)

- **`/walkthrough/:id`** — one walkthrough (HTML iframe or video). `?t=` opens
  it without login. Bytes at `/walkthrough/:id/content`
  (`apps/walkthroughs/streaming.py`, a bare Django view — Range support,
  sandbox CSP).
- **`/review/:id`** — not a narrative surface: a narrative review is
  read, commented on and decided on its **storyboard**, so this address
  forwards there (`apps/reviews/views.py`, a bare 302 before the SPA; the SPA
  route asks `GET /api/reviews/{rid}/destination` for in-app navigation) to
  `#entry-<narrative>`. The review's own valid `?t=` lands on the board WITH
  the board's share token; a member needs none; signed out with no valid token
  → sign in; anyone else → 404 (no board token ever reaches someone without
  the review's). A findings review (`product_findings`, no narrative) still
  renders here.
- **`/share/:token`** — chrome-less, light-themed read-only shared session
  (mounted outside `AppLayout`, own `ShareRouteErrorBoundary`). Shared sessions
  carry a workspace FK (`session_sharing/0005_workspace`).
- **`/storyboard/:slug`** — the shared arc: several narratives as one link,
  public via `?t=<share_token>` in `PublicLayout`. Readers see narratives,
  never "acts" (storage groups entries under an `Act` for curated title, prose
  and order; one composer per narrative). Follows each narrative's current
  release, so an emailed link does not go stale; acts are numbered; a narrative
  with nothing published shows "Being built". Ends with the set's decision
  log: what must be answered before sharing, what to confirm, and what the
  agent assumed — one row per decision, each with a recommended option, who
  decided, and undo. Members' edits apply immediately.
  Every narrative review has a board (`apps/storyboards/review_gate.py`): the
  first board carrying the narrative, else a one-narrative `comment` board made
  when the review is created (`manage.py backfill_review_storyboards` for older
  ones). Its gate decisions (`request_json.decisions`, e.g. approve / redraft)
  are server-owned decision rows — a publish never drops or 409s on them — and
  an editor's ruling (or a round closing with it ruled) resolves the review
  with the `response_json` `submit_review` writes, so the DDD loop's
  `await_resolution` is unchanged. Review-page suggestions land in the board's
  feedback pool / open round.
  When a feedback round is open, every reader sees every round note (names,
  never emails) and a banner with the counts; editors open and close rounds.
  Closing hands the round to the board project's agent as one task (one turn).
  Decision edits during a round show as pending until close. After the agent
  consumes a round, each person sees what happened to their notes.
- **`/narrative/:slug?b=<storyboard>`** — the reviewer surface, one narrative
  scene by scene, gated by the storyboard's token (`b=`). Clean by default; a
  toggle by the version pill shows before/after on changed scenes and brings
  cut scenes back. Commenting is a blank box; editing opens the scene's own
  text. A signed-in editor is not asked who they are (`NoteComposer` reads
  `useAuth`). Shows the story only — no gates, features, provenance or
  findings; the operator console (`/ddd/:narrative`) sits behind a link.
- **`/ddd-release/:narrative/:runId`** — chrome-less release page for one run
  (`?t=`), backed by `GET /api/ddd/release/{run_id}/`; same `PublicHeader` as
  storyboard and narrative.

## Root (personal, global, pre-tenant)

- **`/`** — the public product site for everyone (signed in or not): static
  Astro pages from `site/`, built to `site/dist`, served by
  `config/public_site.py` (assets at `/site/…`); `/how-it-works` and every other
  page in the build too. Only paths that exist in the build are the site's, so
  no app route is shadowed. WhiteNoise is subclassed
  (`config/static_middleware.py`) to leave `/` to Django. Styled by the shared
  Dimagi product-site sheet `site/public/site/styles.css`, vendored unchanged
  from `dimagi-internal/open-chat-studio-prelogin` — don't edit it; theme in
  `site/public/site/canopy-theme.css` (colour only). Its Request access form
  posts to `POST /api/beta-requests` (`apps/beta_requests`, anonymous): stored
  and emailed to `CANOPY_BETA_REQUESTS_TO` with Reply-To the requester; grants
  nothing. `docs/superpowers/specs/2026-10-05-public-site-and-mark-design.md`.
  An in-app (client-side) link to `/` still reaches the app via `RootRedirect`.
- **`/app`** — the door into the app: → the active workspace (signed out,
  Google sign-in first). The site's Sign in and `LOGIN_REDIRECT_URL`.
- **`/about`** — the public explainer, chrome-less, live counts.
- **`/get-started`** — public (no login): the three steps to use a canopy agent
  in your own Claude Code; signed in, it lists your workspaces' agents with
  their install and setup commands, and links to creating an agent in the
  active workspace (`/w/<ws>/agents/new`). `#runner` is **Download Canopy
  Runner**: a table of the deployed runner version's installers (macOS Apple
  silicon, macOS Intel, Windows; `GET /api/harness/runner-releases/downloads`),
  each saying whether it is signed — an unsigned build says the OS will warn
  (Windows SmartScreen: More info → Run anyway). Download → open → approve in
  the browser (`/pair/:code`). Signed out it says "Sign in to download"; with
  none built it says so. The developer path (`install-runner.sh --workspace
  <ws>` from a checkout) is a collapsed "Developers" note; the cloud runner
  guide is linked below.
- **`/download/runner/<platform>`** — server view (`apps/harness/views.py`), not
  the SPA: signed in → 302 to a 15-minute presigned S3 URL for the deployed
  runner version's installer (`macos-arm64`, `macos-x86_64`,
  `windows-x86_64`); signed out → sign in and back; an unknown platform → 404;
  nothing built (or downloads off) → a small 404 page saying no download exists
  yet, linking back to `/get-started`.
- **`/pair/:code`** — approve a computer asking to become your runner: the page
  Canopy Runner opens after install. Shows the code ("check it matches the code
  Canopy Runner shows on your computer"), the hostname, account, system and
  key kind the box reports, a workspace picker (your workspaces where you may
  run work) and an editable runner name (suggested `<account>-<hostname>`).
  Approve needs a passkey step-up, then sends the browser to the box's own
  `http://127.0.0.1:<port>/paired?…` with the one-time secret; Decline refuses
  it. Signed-in only; root because a pairing has no workspace until you pick
  one (`apps/harness/api_runner_pairings.py`).
- **`/act-as/:requestId`** — an agent, in a turn you started, asking to act as you
  on canopy (the push and the supervisor feed card link here): the agent, its reason
  verbatim, the conversation, what it asked for. "Allow read-only (30 min)" (first),
  "Allow read + write (30 min)" (as asked) and Decline; approving runs a passkey
  assertion made for this request. Approved, it shows until when, with Stop. Only the
  person it is for can open it; root because it is about you, not a workspace
  (`apps/harness/api_act_as.py`). In the Canopy Runner app the buttons are one
  "Decide in your browser", and the page follows the decision made there.
- **`/confirm/page-permission/:grantId`** — a page's panel asking "let this agent act
  as you on this page for 30 minutes?", answered in your browser: the Runner app's
  panel opens it here because its frame cannot use a passkey, and any panel offers it
  after a passkey fails. Confirm with passkey confirms the panel's own waiting grant
  (it closes its prompt on its next read); Not now stops it. Yours only; root, as
  `/act-as`.
- **Passkeys in the Canopy Runner app.** Its web view can use none (WebKit gives an
  app without Apple's associated-domains entitlement no WebAuthn, and its session is
  minted from a token, which canopy never steps up), so every passkey moment there
  opens in the browser instead (`frontend/src/auth/passkeyHere.ts`): the step-up
  dialog opens the same page there, `/act-as` and the panel's prompt above follow the
  decision made there, and registering a passkey or a vault's passkey unlock says to
  open the page in the browser.
- **`/home`** — what is waiting on you across every workspace you are in.
  At the top, in their own section, **Alerts** (`GET /api/me/alerts/`): system
  problems you have an action on, one line each (severity dot, source chip,
  title, actions inline, × to dismiss for everyone), severity count chips that
  filter them, and a folded "Dismissed (n)" with who, when and Undo; no quiet
  fold and no personal hide, absent when there are none
  ([`alerts.md`](alerts.md)). Then (`GET /api/me/attention/`, one user-level read): **Sessions**
  waiting on your next message (the Supervisor feed's rule), held behind a
  paused or offline runner (an open dot, naming the box), and yours running
  now (green, "live"), first and open,
  grouped and one line each like `/ide`'s tree (one group per emdash project —
  a repo or an agent — newest group first, each foldable and remembered per
  browser; a row opens in `/ide` on a computer,
  the chat on a phone);
  **Tasks and requests** (approvals, questions, tasks parked on you, access
  requests), open by default; a task's title opens its page, Start now /
  Decline / Reply act on it by key; **Gone quiet** (nothing for 3+ days, folded,
  "Hide all"); **Hidden** (folded, "Show again"). Tasks are approved, declined
  or answered in place. **Hide** only takes a thing off your own Home — the task
  stays open, the chat stays as it is, nobody is told — until something new
  happens on it. Beside it, the active projects you own.
  `/app` (and an in-app link to `/`) lands here.
- **`/supervisor`** — cross-fleet screens: Feed (home), **Waiting on you**
  (`GET /api/me/tasks/?waiting=me`, the shared task table, a row opening beside
  it; `?tab=inbox` lands here), Sessions, Agents, Runners
  (live status; links each runner to the topology map; a runner's detail leads
  with its open alerts, `runner:<id>`, above the raw health list). Root because the fleet
  spans workspaces. Loaded by the phone PWA, desktop browsers and the menubar's
  WKWebView (unless you chose another page for it in `/settings` → Runner app); installable as a PWA; pushes only about the Feed
  (`apps/canopy_sessions/feed.py`). The Feed's top carries what waits on you to approve:
  a box's AWS sign-in, and an agent asking to act as you on canopy (each → its page).
- **`/ide`** (+ `/:id`) — the desktop session IDE: one `WorkbenchRail` tree of
  emdash projects (repos and agents, by the row's `emdash_project`) with every
  session you can see, each row showing reported state (running / input /
  idle), runner, mode and time since last activity; the open session is
  `ChatPage` on the right (`onLeave` keeps a close or a move inside `/ide`). **Add repo** (in the tree's ⋯ menu) asks one of your laptops to add a repo as an emdash project.
  Objective by design: every filter is an on/off toggle (each runner, state,
  mode, archived), kept in `localStorage`, and nothing is ranked or labelled
  "stuck"; the foot of the list says how many sessions the filters hide, with
  **Show all**. **New session** (the shared `NewChatMenu`: an agent, or a repo a
  runner reports) opens the new chat in place, its header title already being
  named; each project row's **+** is the
  same menu opened straight on "Run on" for that agent or repo (shown only when
  the name is an agent you can see or a repo an online runner holds). A delete
  the list called unconfirmed that then lands takes its error back. The rail header is two lines: the state totals (which are the
  state toggles), search, a **Filters** menu (runners, Alt-click to solo; mode;
  archived) whose button says how many are off, and `⋯` (collapse all, hide
  the list, hide the app bar). The ☰ folds the app bar away
  (`components/AppLayout/appBarStore.ts`, `/ide` only); `⌘B` hides the list
  and `/` focuses search. The chat runs `compact` (one-line header) inside
  `.ide-dense`, which sets its text to 13px (`index.css`). Right-clicking a row offers the existing session routes (open in a
  new tab, copy link, rename — also a double-click on the row, or a click on the
  chat header's title — stop, transfer to another runner, archive/unarchive,
  delete = close — its confirm names an emdash task only when a runner reports
  one, `SessionOut.runner_reported`; a cloud session's turns are cancelled and it is archived); the rail's right edge drags to resize (double-click
  resets). In the agent panel, "close the ones that are done" runs the close
  review: the agent reads the visible sessions with their last replies (page
  action `reviewCloseCandidates`, answered by this tab as you), proposes with
  `propose_session_closes`, and the panel shows those rows with its quotes and
  a Close button only you press (`useCloseReview.ts`; spec
  `2026-10-16-ide-close-review-design.md`). Refetches when `ws/supervisor/`
  rings (any frame), polls every 15s otherwise. Root because it spans workspaces; desktop only (phones use
  `/supervisor`). `frontend/src/pages/ide/`.
- **`/activity`** — read-only fleet turn log: time, agent, origin, runner,
  status, client-side filters, inline event-ledger drill-down. "What did fire"
  beside `/schedules` ("what will fire").
- **`/schedules`** — every recurring schedule across your workspaces,
  filterable by workspace/agent. Same `ScheduleCalendar` as the tenant route;
  edits reuse the agent rail's `ScheduleEditor`.
- **`/settings/<section>`** — Settings: everything about you, the same in every
  workspace, one frame (`SettingsLayout`) with who you are and one rail entry
  per section (`frontend/src/pages/settingsSections.ts`); no section has tabs.
  The account menu's Settings opens it. Bare `/settings` opens Profile; every
  old `/settings#anchor` or `?device_key=` / `?github=` link forwards to the
  section holding it, keeping query and hash; `/me` forwards to Profile; Memory
  keeps `/people/me` and is in the rail only for people it is on for. An agent
  asking to sign in as you shows above every section.
  - **Profile** — every workspace you are in, your role, and how members there
    see you.
  - **Account** — contact details, presence, where your sessions are filed,
    who may review them.
  - **Security** — passkeys and your vault key (`#vault-key`: set up with a PRF
    passkey or a passphrase + Secret Key with a one-time Emergency Kit),
    recovery, runner device keys (`#device-keys`, `#your-runners`; a box's
    `approve_url` lands with `?device_key=<id>`, which marks that row; approving
    one offers the vault-key grant for zero-knowledge agents waiting on it), and
    where each of your agents stands.
  - **Agents acting for you** — credentials you delegated, act-as-you
    permissions in both forms: on canopy (an agent in a turn you started that you
    let act as you, read or read + write, waiting or until a time —
    `/api/me/act-as-requests/`) and on connected sites (each site whose panel you
    sent from and confirmed with your passkey — `/api/me/host-grants/`). Stop ends
    either.
  - **Runners** — runners you administer (each linking to its page, where a
    box's Claude login and admins live), opted-in repos (`#canopy-projects`,
    `ProjectOptIn`), Runner app (the page the Canopy Runner desktop window opens
    on; it loads `/runner-app/`, which redirects to the choice,
    `RunnerAppPreference`).
  - **Connections** — GitHub, connected apps (OAuth MCP clients, disconnectable,
    plus the MCP address and `claude mcp add` line), personal access tokens.
- **`/sessions`** — your shared Claude Code transcripts (`/canopy:share-session`).
- **`/system`** — capability catalog + Workflows view, read live from the
  canopy plugin.
- **`/guide`** — the in-app guide, generated from `frontend/src/guide/surfaces.ts`.
- **`/people/me`** — what agents know about you, and your agent-memory settings (a section of `/settings`).
- **`/hcp-clients`** — the HCP service's client registry (superusers).
- **`/beta-requests`** (+ `/:requestId`) — requests for access to Canopy from
  the public site; only their reviewer or a superuser sees them.
- **`/new-workspace`** — the first-run page with the create-workspace form; a
  member also gets a link to creating an agent in their active workspace. A
  workspace that holds the coach (`COACHING_AGENT_SLUG`) shows an unticked "Have
  <coach> coach me" checkbox beside its request button; ticked on a request that
  is approved at once, it grants that agent a session review (tenancy.md § The
  chat-session ACL); on a pending one it grants nothing and points to the switch
  on account settings (`/settings`, "Who can review your sessions").
- **`/invite/:token`** — accept an invite. Outside `/w/` because an invitee has
  no membership yet. Previews pre-auth (masked email, workspace, role —
  `GET /api/workspaces/invites/{token}/preview`); accepting needs a login.
- **Legacy flat redirects** to the active workspace: `/timeline`,
  `/shareouts/*`, `/walkthroughs`, `/storyboards`, `/huddles/*`, `/agents/*`,
  `/ddd/*`. `/ddd-plans`, `/reviews`, `/insights`, `/projects` → `/`.
- Unmatched paths render `NotFound`, which reloads once (a browser on a
  pre-deploy bundle may lack a new route) before showing a real 404.

## Server pages (not the SPA)

`config/urls.py`; the SPA catch-all (`spa_view`) excludes `api/`, `admin/`,
`accounts/`, `health/`, `static/`, `auth/`, `assets/`, `oauth/`, `.well-known/`.

- `/api/` — REST (Ninja); `/api/docs/` Scalar, `/api/redoc/` Redoc.
- `/admin/` — Django admin. `/accounts/` — allauth. `/health/` — health check.
- `/auth/cli/authorize/`, `/auth/github/{start,callback}/`,
  `/auth/slack/{install,callback,link}/` — browser legs of sign-in/connect flows.
- `/oauth/*`, `/.well-known/*`, `/api/hcp/oauth/*` — OAuth/HCP documents and
  endpoints ([`mcp-surface.md`](mcp-surface.md), [`hcp-service.md`](hcp-service.md),
  [host grant contract](../../sdk/python/README.md#host-grant-contract-v1)).
- `/embed/chat`, `/embed/widget.js` — the one framable page and the widget
  loader ([`embedding-a-canopy-agent.md`](embedding-a-canopy-agent.md)).
- `/w/<ws>/walkthrough/<id>/content`, `/w/<ws>/marketplace.json`,
  `/w/<ws>/plugins/…` (410) — see above.
- `/plugins/marketplace.json` — your agent catalog for Claude Code, marketplace
  `canopy-agents` (`apps/plugins`, bearer canopy token, 401 never a sign-in
  redirect). [`plugin-marketplace.md`](plugin-marketplace.md).
- `assets/` is deliberately NOT served by the SPA catch-all: a missing hashed
  bundle must 404, not return `index.html` as `200 text/html`.
