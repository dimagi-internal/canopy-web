# Pages: scoped public viewers and root (personal, global, pre-tenant) pages

The anonymous token-scoped viewers, and the root pages: the public site, getting started, pairing, connections, act-as, home, supervisor, IDE, personal settings, people and invites. Part of the [pages](pages.md) topic; the index there lists every part.

## Scoped public viewers (anonymous with a token)

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
- **`/app`** — the way into the app: → the active workspace (signed out,
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
- **`/w/:workspace/connections/:id/connected`** — "Connected": where the OAuth callback
  lands (`?just_connected=1` is added, and this page strips it once read, so a reload or a
  shared link never prompts). It reads the share state, then:
  - *A person's connection* (the callback already sealed it to their vault key): no prompt.
    "Connected as X and stored in your vault", then either "Nothing uses it yet" or, when
    `server.reachable` and canopy-web calls the provider, that project screens and apps can
    use it now; Done goes to `return_to`, a quiet link goes to Share. A reconnect over an
    earlier share says how many agents or computers need it shared again ("Share again"
    goes to Share with the earlier choices ticked, or Later).
  - *An agent's unshared connection*: on arrival it opens the owner's vault key (passkey
    only, never the passphrase dialog) and stores it for that agent with the same
    functions as Share (`vault/ownerShare.ts`), with no chooser. It signs what the owner
    signed before (same agents, computers, canopy-web) and the owner agent alone on a first
    connect. It does not sign, and links to Share, when something earlier is no longer
    choosable, canopy-web is on but can't be named, a key changed, a reconnect would sign
    something different, or a voucher is undecided. With no machine ready it says so and
    offers Try again. If the browser needs a click, or it fails, the page is headed "Not
    connected" and says the connect didn't finish; Try again does the same on press, reading
    the row again first (a stale statement is read again and retried once), and Cancel leaves.
    When it finishes the page names the machines it is on. The agent's accounts page never
    lists an unfinished first connect: it shows Connect, and the sweep removes the row after
    an hour. A reconnect that didn't finish shows "Reconnect needed".
  - A reload, or an agent row that is already shared, says "<agent> can use it." The lists
    show "Used by canopy-web" for those providers: a plain switch to turn off an unshared
    connection (`POST /api/connections/{id}/server-use`), a link to Share for anything else.
- **`/w/:workspace/connections/:id/share`** — "Share this connection": where Connected and
  Settings' Share on a not-shared connection lead. It loads with no prompt, headed "Who can use <account>",
  and says what happens next; "Unlock with your passkey" is the only prompt on the way to the
  choice (`useVaultUnlock`). Opened, it shows
  the account from the opened envelope; the owner then ticks agents they administer
  and their own computers (name plus fingerprint) and shares
  (`vault/connectionShare.ts` signs the statement and re-seals). Agent-owned: the
  owner agent is fixed on. Never shared and nobody chosen: Share is off and Not now
  leaves it unshared ("Not shared yet: no agent or computer can use it."); once shared,
  unticking everyone withdraws it. Machines left out are named with why; a chosen machine
  vouched for only by someone this browser has not trusted offers "Trust <name>'s
  vouching" with their fingerprint; a changed endorser key stops the share; an envelope
  that fails verification says "can't be opened safely" and offers Reconnect;
  a reconnect over an earlier share warns first; opened by someone whose key is not the
  one it is sealed to, it says why and to disconnect and connect again. Afterwards it goes to `return_to`
  (a same-site path) or Settings. The key lives in a ref for the flow, never state
  (`pages/connections/ShareConnectionPage.tsx`).
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
  `ChatPage` on the right (`onLeave` keeps a close or a move inside `/ide`). Closing the open session, from its row or its header, opens the row below it at once (the pane goes empty if there is none) rather than waiting on a runner; the closed row stays marked until its runner reports. **Add repo** (in the tree's ⋯ menu) asks one of your laptops to add a repo as an emdash project.
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
  started by me; archived) whose button says how many are off, and `⋯` (collapse all, hide
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
    who may review them; and, only for someone who still has one, the project
    teams they share laptop sessions with, to stop (`#session-sharing`; starting
    a new share is retired).
  - **Security** — passkeys and your vault key (`#vault-key`: set up with a PRF
    passkey or a passphrase + Secret Key with a one-time Emergency Kit),
    recovery, runner device keys (`#device-keys`, `#your-runners`; a box's
    `approve_url` lands with `?device_key=<id>`, which marks that row; approving
    one offers the vault-key grant for zero-knowledge agents waiting on it), and
    where each of your agents stands.
  - **Agents acting for you** — credentials you delegated, act-as-you
    permissions in both forms: on canopy (an agent in a turn you started that you
    let act as you, read or read + write, waiting or until a time —
    `/api/me/act-as-requests/`) and on host sites (each site whose panel you
    sent from and confirmed with your passkey — `/api/me/host-grants/`). Stop ends
    either.
  - **Runners** — runners you administer (each linking to its page, where a
    box's Claude login and admins live), opted-in repos and the workspaces each may be worked in from (`#canopy-projects`,
    `ProjectOptIn`), Runner app (the page the Canopy Runner desktop window opens
    on; it loads `/runner-app/`, which redirects to the choice,
    `RunnerAppPreference`).
  - **Connections** — GitHub, authorized apps (OAuth MCP clients, disconnectable,
    plus the MCP address and `claude mcp add` line), personal access tokens.
- **`/sessions`** — your shared Claude Code transcripts (`/canopy:share-session`).
- **`/system`** — capability catalog + Workflows view, read live from the
  canopy plugin.
- **`/guide`** — the in-app guide, generated from `frontend/src/guide/surfaces.ts`.
- **`/people/me`** — what agents know about you, and your agent-memory settings (a section of `/settings`).
- **`/people/:personId/page`** — a person's memory page, read-only, for an agent they let see it from their own `/people/me`: what is remembered, the conversations that used it, who can read it; each look is logged. Anyone not let in sees nothing.
- **`/hcp-clients`** — the HCP service's client registry (superusers).
- **`/beta-requests`** (+ `/:requestId`) — requests for access to Canopy from
  the public site; only their reviewer or a superuser sees them.
- **`/new-workspace`** — the first-run page with the create-workspace form; a
  member also gets a link to creating an agent in their active workspace. A
  workspace that holds the coach (`COACHING_AGENT_SLUG`) shows an unticked "Have
  <coach> coach me" checkbox and its "What does this mean?" panel beside its request button; ticked on a request that
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
