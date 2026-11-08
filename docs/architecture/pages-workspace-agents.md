# Pages: workspace agents

The tenant-scoped pages for agents: the agents table, creating an agent and the agent workspace with its sub-routes. Part of the [pages](pages.md) topic; the index there lists every part.

- **`/agents`** — the workspace's agents as a table (`GET /api/agent-roster/`): an **About** column (the description, shown whole, minus the factory's `<Name> — ` lead and operating-model sentence; full text on hover), last turn and whether it failed, turns and failures in 7 days, open and waiting tasks, active projects, mode, owner; editors get **New agent**.
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
  An agent's Settings ends with **Delete** (the agent's owner or a workspace owner, `DELETE /api/agents/{slug}/`):
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
  - **Mail** (`mail`) — the agent's mailbox in the SAME client as a person's own mail
    (`components/mail/MailClient`, below), replying and composing *as the agent* for someone who may
    act as it (`canEdit`; the server checks FULL). No address → points to Settings → Email.
  - **Mail** (`/w/:ws/mail`, Workspace menu) — the signed-in person's own mail client for
    forward routes (Keep a copy is on unless unchecked), private to the person (`MailThread.owner`).
    `MailClient` is one implementation for both: they differ only in the source (`forwarded` vs
    `agent`) and who the server lets send. The open thread and folder are in the URL
    (`?thread=<uuid>&folder=`). Folders: Inbox, Sent, Drafts, Archived, Spam, Trash; 30-second refresh
    and on tab focus; list and reader scroll apart, one pane at a time below `lg`; select several; older
    messages and quoted text collapse; a failed sender check warns on the message. Drafts save as you
    type and return when their thread opens. Send waits 5 seconds with Undo (the draft stays saved until
    it has gone); moving a thread offers Undo. Sends plain text with Cc, Bcc and attachments; an address
    that does not parse blocks Send. Its keys are in the app's `?` list (`components/work/keys.ts`). HTML renders only in the sandbox
    in `components/mail/MessageView` (`docs/architecture/mail.md` § Safety invariants). Settings → Email
    shows each domain's inbound ledger (admins).
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
    **Roles** (owner; the canopy login; **Access**, the agent's access list and the
    workspace defaults it inherits — `GET /api/agents/{slug}/access-list` — beside a
    roster of every workspace member's role on the agent, why, and what they can
    reach — `GET /api/agents/{slug}/access`; agent admins granted/revoked on the row;
    **Direct use**); **Channels** (read mode and host sites, Slack, email, Google
    account); **External accounts** (GitHub delegation, the
    Google mailbox); **Security** — first **Vault**: the shared `VaultView`
    (`components/vault/`, adapter `pages/agents/AgentSecretsPanel.tsx`) — a summary
    line, then **Secrets** (open; every secret in `VaultEditor`
    (`components/vault/VaultEditor.tsx`, a table you Change / Add / Reveal /
    Delete in place (a value can be a file from disk, e.g. a service-account
    JSON), owner-agnostic so other vault pages reuse it; Reveal decrypts locally with the person's vault key, and Save encrypts
    locally once the agent has one; under it, **Also from shared vaults**: each
    workspace vault its turns read, the names in it and why), **Access** (admins; tabs
    People — the vault's people with why each can, `docs/architecture/vault.md` —
    Agents — only itself — and Machines, with approve / revoke), **Encryption key**
    (the Canopy vault, key copies, zero-knowledge grant / rotate / recover
    with code, every encryption in the browser, the one-time recovery-code
    dialog, the stated trust caveat) and **Log** (admins: unusual reads; each
    secret's read history opens from its row). Sections other than Secrets start closed, open by themselves when
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
