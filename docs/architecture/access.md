# Access — the glossary and the one rule

Every word canopy uses about who may do what has exactly one meaning, and it is
defined here. Code docstrings point at this page instead of re-defining terms
(`apps/agents/access.py`, `apps/agents/grants.py`, `apps/agents/roles.py`,
`apps/harness/caller_context.py`, `apps/workspaces/permissions.py`,
`apps/agents/interface.py`, `apps/harness/turn_mode.py`). Reuse existing words, one
meaning per word, no new terms. Design: `docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md`.

There are two ladders, and they are different questions:

* a **workspace role** says what a person may do in a *workspace* (read logs,
  invite people, create content, reshape agents);
* an **agent role** says what a person may make one *agent* do.

THE rule that joins them is one function, `apps/agents/access.decide(agent, who,
*, verified, origin, capability=None, turn=None, standing=None, lines=None)`, which returns a
keyword-only `Decision(relationship, access, capability, may_request_auto, may_pin_runner,
manual_only, basis, reason, agent_role, source, via, permissions)`; `relationship` is how canopy
knows the asker (`owner | admin | member | contact | system`), `agent_role` is the named role their
lines give. Every entry point that makes an agent do work asks it (see "Where it
is asked" below); `tests/test_access_matrix.py` asks it through every entry point. Don't write a
second predicate for "may this person make this agent do X" — ask `decide`.

This page is the glossary, the roles, the access list, access levels, capabilities, the decision
table, triggers and turn mode. The rest is split by subject:
[access-direct-use-and-admission.md](access-direct-use-and-admission.md) (direct use of an agent,
what a caller's turn tells the owner, admission rules, the caller envelope),
[access-confinement-and-act-as.md](access-confinement-and-act-as.md) (how confinement is enforced,
acting on behalf of the asker, act-as-you on canopy-web and on connected sites) and
[access-sessions-and-people.md](access-sessions-and-people.md) (who can see and who is in a session,
Slack threads, what agents know about a person); the HCP service is [hcp-service.md](hcp-service.md).

## The words

| Word | Means | Where it lives |
|---|---|---|
| **workspace role** | What a person may do in a workspace: viewer < editor < admin < owner < superuser. Each role holds a set of workspace *permissions* (`content.write`, `logs.read`, …), not to be confused with the agent permissions below. Built on the same role engine as agent roles (`apps/common/rolesets.py`), with its own vocabulary. | `apps/workspaces/permissions.py`, `WorkspaceMembership.ROLE_RANK` |
| **permission** | What a person may do with ONE agent: `read` (be answered in read mode), `write` (drive the whole agent), `agent_edit` (change the agent itself), `admin` (configure it), `own`. `agent_edit` implies `write` implies `read`; `admin` implies all but `own`. A line may also grant the scoped `use:<capability>`, which opens that one capability. | `apps/agents/roles.py` |
| **agent role** | A named bundle of permissions, **defined by a workspace** (`AgentRole`) and usable below it. Three come built in as the defaults: **editor** (read + write + agent_edit), **read_write** (read + write), **read_only** (read); they are editable, cloneable and never deletable. A workspace adds its own ("read + use the connect door"). **owner** and **admin** are fixed in code: they are `Agent.owner` and `Agent.is_admin`, never a line. Never "member", "contact", "viewer" or "user". | `apps/agents/roles.py` (vocabulary, the built-ins' seed, the fixed roles), `rolecatalog.py` (the catalog), `roleadmin.py` (editing it) |
| **system** | Not a person and not in the table: canopy itself (a schedule, a drill), the agent's OWN canopy login, an approved task's dispatch. It may do what an agent admin may. | `roles.SYSTEM` |
| **access list** | The one list per agent that says who has what: lines of `(who, role)` or, rarely, `(who, one named capability)`. | `AgentGrant`, `apps/agents/grants.py` |
| **line** | One entry of an access list. `who` is a person, everyone in a workspace (optionally also every workspace below it), anyone at a domain, or a known visitor or sender. | "The access list" below |
| **workspace default** | A line set once on a workspace; every agent in it, and in the workspaces below it, inherits it unless the agent opts out. | `AgentGrant.workspace`, `Agent.inherit_workspace_defaults` |
| **read mode** | What a person with `read` and not `write` gets: the agent answers them in a confined session that **exists on every agent from the start**. One per agent: canopy's default profile (`interface.default_read_profile`, built from the agent, pinned by `tests/test_default_read_profile.py`, shown on the agent's Settings → Read mode and doors), or the agent's own published `ask`, which replaces the default whole. `read` and `ask` are the same thing (`interface.is_read_mode`, the only place that says so); a turn is stamped with the name actually run (`read` for the default, `ask` for an override). | `apps/agents/interface.py`, `interface_default.py` |
| **door** (a *capability*) | A named, sandboxed profile an agent publishes (ACE's `connect`): which tools, paths and sites a confined session may touch, for a place or a list of people. Rare. It never says who may use it: that is the access list's job (`read` opens read mode; a line can name any door). `ask` is the override of read mode, not a door. | the agent's interface, `apps/agents/interface.py` |
| **entry point** | A way work reaches an agent: the harness API, a canopy-web chat, Slack, email. Every one asks `decide`. | "Where it is asked" |
| **role** vs **source** | A role is what someone can do; a source is why they have it (`owner`, `admin`, `system`, `session`, `list:<line>`). Never the same word. | `Decision.agent_role`, `Decision.source` |
| **contact** | Not a role. How canopy knows someone who is not a workspace member: an emailer, a Slack person outside the workspace, a widget visitor (who may have no address at all). A contact has an agent role only if a line names them; with no line they are refused. | `apps/contacts` |
| **verified** | THIS message proves the sender's address: a signed-in request, mail that is DMARC-aligned or DKIM-signed by its own From: domain, a full member of our own Slack. An embedded site's visitor is never verified (the host vouches for them, not the human). | `caller_context.is_verified` |
| **access level** | What a turn may reach: `full`, `confined` or `none`. | "Access levels" |
| **agent admin** | Someone who holds an agent's keys: its owner, a workspace owner (implicitly), or a person granted by name (`AgentAdmin`), each still a member of the workspace. `Agent.is_admin` answers it. On the ladder, *admin* is this role and *owner* is the agent's owner above it. | `Agent.is_admin` |
| **workspace admin** | A person with the workspace role admin: runs the workspace, holds no agent keys. | "Workspace roles" |
| **runner admin** | A runner's owner or a `RunnerAdmin` grant. | "Runner roles" |
| **vault key holder** | A person who holds a copy of a vault's key. | `vault.md` |

The words say which admin they mean, always: a message that says "admin" alone has not said
who may.

## Whose authority an agent acts with

**An agent acts as itself — with its own credentials or ones a person delegated to it —
or as you only under an act-as-you permission you granted.** Two things decide what a
session carries, and the person chose both: **where it runs** (a runner box, or *direct*:
their own Claude Code) and **whether it is reviewed** (`manual` or `auto`). Three kinds of
credential follow from them, in ONE table (`credentials/authority.py`, asked by
`credential_resolve`; the caller envelope's `authority` block tells the session):

| Kind | On a runner | Direct |
|---|---|---|
| **Agent-only** (its canopy login) | the agent's | never handed out |
| **The agent's vault secrets** | the agent's | the agent's, as far as the person's role reads the vault |
| **Delegated** (a person's lent account) | the agent's, sealed to the box | the person's own; the lent copy never travels |

A person's own open direct turn is a direct read whoever they are: an agent admin or a
runner owner is read like anyone and steps up like anyone. The one step-up is crossing
from the agent's identity to the person's ([access-confinement-and-act-as.md](access-confinement-and-act-as.md), "Acting as the person on canopy-web"); nothing else in a direct session asks
for a touch. An `auto` turn may ask to act as the person to read only
(`act_as.unreviewed_write_refusal`). The kinds, one meaning each:

* **Its own credential** — the agent's own login (`Agent.user`, e.g.
  `ace@dimagi-ai.com`) and the secrets in its own vault (`account: "agent"`). A runner
  turn signs in with a login issued for that turn (`TurnAgentToken`); a stored login
  expires like any PAT (`PAT_DEFAULT_TTL_DAYS`, 180 days) and rotation replaces it first
  (`pat_rotation.max_days`).
* **A delegated credential** — a standing credential from a person's own account given
  to the agent: its owner's or an agent admin's GitHub token, shown as "GitHub · delegated by
  <name>". It is a vault entry with `account: "delegated"` and `delegated_by`
  (`models.VaultEntryAccount`, read through `credentials/accounts.py`), sealed (`zks1`)
  to the machines that run the agent — a runner serving it whose owner is one of its
  admins, and that is a cloud box or the person's own (`accounts.delegated_devices`;
  another agent admin's laptop would let them open it) — and to nothing else: never under the vault key,
  so no workspace member, agent admin or license holder reads it, nor canopy-web, nor the person it came
  from (they replace or withdraw it, `/settings`). It is the agent's to use in any turn
  `decide` lets run in the full profile (never a caller's confined turn); a box gets it
  only in its own resolve's `delegated` field, proved by its device key. canopy gives it
  no expiry: the provider's own is shown as information. → `vault.md` § Whose account
  an entry is
* **A trigger's turn** — a schedule or alarm mail the agent's own trigger fired runs
  with no more authority than the person who set it up has on the agent, and stops
  firing, loudly, when they lose it (see "Triggers").
* **An act-as-you permission** — time-limited, granted by the person asking, in the
  moment, with their passkey. It has two forms: **a connected site's page** (a host
  grant: the agent acts as you at that site, "Acting as the person on a connected
  site" in [access-confinement-and-act-as.md](access-confinement-and-act-as.md)) and **canopy-web itself, in your own turn** (an agent you started asks,
  you approve read-only or read + write, and it acts as you on canopy for 30 minutes —
  "Acting as the person on canopy-web" in [access-confinement-and-act-as.md](access-confinement-and-act-as.md)). Nothing else lets an agent act as a
  person. In particular:
  - **An agent's caller token only reads.** A full-profile AGENT turn asked by someone
    who is not the agent's owner, an agent admin or canopy — an editor line, a
    writer in someone else's chat — reaches canopy as its asker so that it sees what
    they may see (`CallerToken`, `Grant.read_only`), but its tools are the READ tools
    only (MCP readOnlyHint: every GET route, and the hand-written reads) plus driving
    the asker's own open page (`caller_tokens.PAGE_DRIVE_TOOLS` and the page's `page_*`
    tools: they run in the asker's tab, as them, and canopy's own page actions only
    navigate and read — anything that commits waits for their click). A write is
    refused, on MCP (`mcp.turn_scope.READ_ONLY_REFUSAL`) and on REST
    (`tokens.middleware._authenticate_caller_token`), saying the asker has granted no
    act-as-you permission: the agent writes as its own login, or asks them for one
    ([access-confinement-and-act-as.md](access-confinement-and-act-as.md)). Confined caller tokens
    keep their capability's own tool list. A **repo chat** (no agent: a person's own
    Claude Code session on a repo they chose) is that person's own tool, and its token
    keeps every tool, as them.
  - **An agent's canopy login is always the account at its own email** (`Agent.email`),
    never any other (`agent_login.link_by_email`; `services.link_canopy_user` refuses
    any other account). An agent admin links it, browser or token — gated by role: with no
    account at that email canopy creates one (never signed in interactively) and links
    it; an existing account there that nobody has ever signed in to interactively links
    directly (no person is behind it to accept); when the account is the agent admin's own,
    after a passkey step-up; when it is anyone else's account that has signed in, canopy
    can't tell an agent's mailbox from a person's, so a
    request (`AgentLoginRequest`, 7 days) waits for the person holding it to accept, in
    their own browser behind a passkey step-up (Settings → "Agents asking to use your
    account", `POST /api/me/agent-login-requests/{id}/accept`; a token is refused). A new
    `Agent.email` unlinks the old account and derives the link again; an agent with no
    email has no login.
  - **An agent's token is watched.** With automatic rotation off nothing replaces it, so
    within 14 days of its expiry the agent's admins get an alert naming how to turn
    rotation on or give the agent a fresh login (`alerts_agent_token.py`).
  - **A runner never hands a turn its owner's token.** The cloud runner strips the
    box's `CANOPY_TOKEN` from every turn, agentless repo turns included
    (`cloud_runner._agent_env`).

## Workspace roles

`viewer < editor < admin < owner < superuser` (`WorkspaceMembership.ROLE_RANK`). What each may
do is one table, `apps/workspaces/permissions.py`, keyed by permission name; nothing
outside `apps/workspaces/` names a role. Full detail: `docs/architecture/roles.md`.

| Workspace role | Holds | In plain words |
|---|---|---|
| **viewer** | `read` | Reads the workspace. Talks to an agent only as far as that agent's access list and capabilities let them. |
| **editor** | + `content.write`, `agent.work`, `session.drive`, `events.write` | Makes things; **reshapes agents and sends them work** (the routes behind an agent's settings, schedules, tasks and dispatch). Whether a turn of theirs runs the agent's whole profile is the agent's access list, not this role. |
| **admin** | + `logs.read`, `members.manage`, `integrations`, `runners.route`, `retention.manage`, `admission.manage`, `projects.decide` | **Runs the workspace**: every log, members below admin, the integrations, the default runner order, how long chat and turn content is kept, which turns are refused, and starting or declining a proposed project (beside that project's own owner). **Holds no agent keys** — a workspace admin is not an agent admin. |
| **owner** | + `own`, `control` | **The keys**: the shared vault, the Slack app, connected sites, deleting the workspace — and is an **agent admin of every agent** in it. Holds or stops all work in the workspace's tree. **Is told**: blocking alerts, access requests, owner notices. |
| **superuser** | everything an owner holds | **Oversight**: an owner's keys, here and below, **without the owner's email** (`services.owner_user_ids` and `services.mail_recipients` leave superusers out — unless nobody else would be told). The workspace-level sibling of the system superuser (`User.is_superuser`), which holds the same two switches for the whole fleet and no workspace access. |

**Workspace permissions** (the names a gate asks `perms.can` for): `read`, `content.write`,
`agent.work`, `session.drive`, `events.write`, `logs.read`, `members.manage`,
`integrations`, `runners.route`, `retention.manage`, `admission.manage`, `projects.decide`, `own`, `control`.

## Getting into a workspace

Nothing is automatic: there is no self-join and no domain auto-join. People at a
listed domain may only **request an invitation**. A person becomes a member in
exactly four ways — and an agent's own login in a fifth — and the only code that may
create a `WorkspaceMembership` is `services._grant` (the first two, and a tree add),
`services.ensure_member` (the default-workspace bootstrap, and the agent-login path
below) and workspace creation — `apps/workspaces/tests/test_access_requests.py`
fails on a fourth path, and `tests/test_no_implicit_enrolment.py` pins
`ensure_member`'s callers:

* **An invite.** A workspace **admin** or **owner** invites an email address at a role they
  may grant (`permissions.may_manage_member`; **viewer** unless they choose
  otherwise). Canopy emails the `/invite/:token` link; accepting grants that role
  (never lowering one already held).
* **An approved access request.** A person whose login email's domain is in the
  workspace's **`access_request_domains`** may ask (`POST
  /api/workspaces/{slug}/access-requests`, with an optional note) — the first-run
  screen offers "Request an invitation to <workspace>". Every workspace **admin** and
  **owner** — owners of a parent workspace included — is emailed (and pushed) a
  deep link to the request's page,
  `/w/:workspace/settings/access-requests/:id`, where one of them **approves** at
  **viewer**, **editor** or **admin** (an owner any of them; a workspace admin only below
  admin — `permissions.may_manage_member`) or
  **denies**, with an optional reason. The requester is emailed the outcome. One
  open request per person per workspace; asking again is idempotent. `GET
  /api/workspaces/requestable` lists where you may ask. Notifications go through
  the invite mechanism (labs SES) plus Web Push and are best-effort: a failure
  never fails the request — it is stored on the request (`notify_result`) and
  written to the event log (`source=workspaces.access`, `warn`).
* **A tree add.** A workspace **admin** or **owner** adds someone who is ALREADY a member of
  another workspace in the same tree — one the adder is in too — directly, at a role
  they may grant, with no invite (`POST /api/workspaces/{slug}/members/`,
  `services.add_tree_member`, from the "Members across workspaces" page
  `/w/:workspace/settings/member-tree`). It closes the gap inheritance leaves: only
  owners and admins are in the workspaces below theirs, so an org editor is a stranger
  in its divisions until added. Anyone outside the tree is the same 404 as a missing
  user; an existing member is 409 (change their role instead).
* **Creating a workspace** makes you its **owner** — subject to
  `login_gate.can_create_workspace`: an *invite-admitted* user holding no membership may
  not, or invite-admission would become transitively delegable (create a workspace, mint
  invites, and each invitee clears the login gate too). `/api/me/` reports
  `can_create_workspace` so the UI never offers a button that 403s.
* **An agent's own login** (not a person). `POST /api/agents/{slug}/canopy-token`, run by
  `canopy agent vault`, creates the login an agent signs in to canopy as when it has none:
  a brand-new, password-less **editor** from the agent's mailbox address, linked to that
  one agent. An agent admin may when the address is one only an agent has (a domain in
  `AGENT_LOGIN_DOMAINS`: dimagi-ai.com and dimagi.ai) — the
  account can then never be a person's way into the workspace; for any other address it takes
  an agent admin who is also a workspace **admin** or **owner** (who could have invited it) —
  and an address that is already a canopy user is refused, never adopted
  (`apps/agents/agent_login.py`). Moving the agent (`POST /api/agents/{slug}/move`)
  brings that login into the destination as an editor on the same bar — only when the
  mover holds `members.manage` there; otherwise the move reports it and adds nobody.

There is no other way in. A connected site's visitor is a **contact**, never a member
(`WorkspaceMembership.provisioned_by_app` is provenance on old rows only), and
`access_request_domains` means "may ask", never "may join". A request cannot probe
tenants: a workspace whose domains do not match yours and one that does not exist return
the **same 404**. Approval is upgrade-only, like an invite, so it never demotes a member.

**Auto-approve** (`Workspace.auto_approve_role`, owner-set at
`PUT /api/workspaces/{slug}/access-settings`): blank is off, and every request
waits for a person. Set to **viewer** or **editor** (never above), a request is
approved the moment it is made — still an ordinary request record (`status:
approved`, `auto: true`, `decided_by: null`), and every workspace admin and owner is still
emailed the link, from which they can change the person's role or remove them.
The `dimagi` workspace is set to **editor** (`workspaces/0012`);
every other workspace is off. Turning it off or lowering it is a settings
change. The domains themselves are server-set, never client input.

## Agent roles

What a person may make ONE agent do (`apps/agents/roles.py`). A **permission** is one thing; a
**role** is a named bundle of them. The vocabulary is fixed (`read`, `write`, `agent_edit`, `admin`,
`own`, and `use:<door>`); the bundles are **configurable** (next section); these are the built-ins
and the two fixed roles, strongest first:

| Agent role | Holds | In plain words |
|---|---|---|
| **read_only** | `read` | Is answered in read mode, and nothing else. **On for every agent**: the confined session can only read and draft a reply (and send it on the thread it was asked on); the access-list form pre-selects no role. |
| **read_write** | `read`, `write` | Drives the **whole agent**, in its own authority: every skill and every capability it has (the `full` access level). The ordinary way to give someone an agent. |
| **editor** | + `agent_edit` | Everything read_write may, and changes the agent itself: its skills, its persona and the code in its own repo (table below). |
| **admin** | + `admin` | Configures the agent in canopy-web: its access list, capabilities, credentials, routing, runners, integrations. May ask for `auto` and pin any runner that can hold it. |
| **owner** | + `own` | Everything an agent admin may, plus transfer or delete the agent and name its admins. |

* **The dependency rule** (`apps/agents/roles.py`, enforced when a role is built, tested in
  `tests/test_agent_access_list.py`): `agent_edit` implies `write` implies `read`; `admin` implies
  everything except `own`; `write` also opens every capability. A role cannot hold a permission
  without the ones it depends on, so a future user-defined role gets the same guarantee by going
  through the same constructor. Roles are fixed built-ins; there are no user-defined roles.
* **One engine.** The closure, `allows(role, permission)`, `holds(permissions, permission)` and rank
  are `apps/common/rolesets.py`, shared with the workspace's roles
  (`apps/workspaces/permissions.py`, a ladder derived from `MINIMUM_ROLE`). The mechanism is shared;
  the vocabularies (`read`, `write` here; `logs.read`, `members.manage` there) are not.
* `system` is not in the table (see "The words"). Another agent's login is not `system`: it is
  graded like any person, or agent A could steer all of agent B.
* **What `decide` reads.** `decide` unions the permissions of every matching line
  (`Resolution.permissions`) and asks one question: do they hold `write` (`roles.holds(…, WRITE)`)?
  Then they get the whole agent. Otherwise they get a capability, chosen by what their lines open
  (`roles.can_use`: `write` opens all, `read` opens read mode, `use:<name>` opens `<name>`; `use:ask` and `use:read` are read-mode lines). What each change to
  an agent needs is the table below.
* **What changes the agent itself (`agent_edit`).** `access.may_edit_agent(user, agent)` is the
  one predicate, asked by `@gate("agent-edit")` routes (`gates.agent_for_edit`): the owner, an admin and the agent's own login always may; anyone else only through
  an access-list line whose role holds `agent_edit` (an `editor` line or workspace default). Being an
  editor of the agent's workspace gives nothing by itself; the default editor line every workspace
  editor was given keeps today's editors able. An agent acting for its asker is judged as the asker
  (`turn_asker`), so a read_write asker cannot have the agent edit itself. Every change to an
  agent is one of three classes:

  | Change | Class | Why |
  |---|---|---|
  | Publish skills (`PUT /agents/{slug}/skills`), re-read skill history (`POST …/skill-history/sync`) | **agent_edit** | the agent's own definition of how it works |
  | Edit display name, description, persona, email, google account, avatar, runner-kind preference (`POST /agents/` on an existing agent; creating one is a workspace editor's) | **agent_edit** | what the agent says about itself |
  | The code in the agent's own repo (commits, merges to its default branch) | **agent_edit** | the agent's `permissions` in the caller envelope are its cue; the runner-side rail (canopy plugin) refuses a push or merge from a turn whose asker lacks it |
  | Where the repo points and what boxes install (`repo_url`, `repo_ref`, `runtime_secrets`, `runtime_sources`, `clis`, `cli_models`; `PATCH …/repository`, `PATCH …/clis`) | **admin** | whoever names the repo writes code that runs with the agent's credentials |
  | Published interface: `PUT`/`DELETE …/interface`, read-mode override, the `sensitive` switch | **admin** | decides who may reach what, and how sensitive data is handled |
  | Machine capabilities (`PUT …/capabilities`, refresh, slot data) | **admin** | leases money-spending machines |
  | Access list, workspace defaults, licenses, canopy login and token, vaults and credentials, GitHub identity, mailbox, runner pins, routing rules, Slack switch | **admin** | configuring the agent in canopy-web; its keys |
  | Transfer, delete the agent, name its admins | **own** | `Agent.owner` or a workspace owner |
  | Schedules and triggers, tasks and the board, turns and chats, hold and release, turn mode, syncs | **work** (`agent.work` or `write`) | they change what the agent DOES, never what it IS |

* **Where each role comes from.** The owner (`Agent.owner`, while still a member of the agent's
  workspace), agent admins (a workspace owner, or an `AgentAdmin` grant) and canopy itself hold
  their role with **no line**: `Agent.owner` and `AgentAdmin` remain the stores of owner and admin,
  and the access list shows them as **implicit** lines it does not let you edit. Every other role
  is a line on the access list. A line cannot give `admin` or `owner`.
* **An owner or agent admin who has left the workspace is neither** (`Agent.is_admin`,
  `caller_context.relationship_for_user`): both need current membership.
* Roles describe a person; what a *turn* gets also depends on who started it and how
  (`Decision.source`, below).
* **Safety rails are stated by permission**, not by role name: a `domain` or `contact` line can
  never grant a role that contains `write`; a workspace default whose role contains `write` is the
  workspace owner's call. See "The access list".

### Configurable roles

**The roles a line can give are rows, defined by the people who run a workspace**
(`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`; the Roles page, Settings → Agent
roles; `GET|POST /api/workspaces/{slug}/agent-roles`, `PATCH|DELETE …/{key}`, `POST …/{key}/reset`).

* A role is a key (immutable, stored on its lines and sent in the envelope), a name, a sentence and a
  set of permissions chosen from `read`, `write`, `agent_edit` and `use:<door>`. It is stored **closed**
  (write carries read and every door, agent_edit carries write). A custom role can **never** hold
  `admin` or `own`: those come from who the agent belongs to, not from a line.
* A role is defined on a workspace and usable by it and every workspace below; its key means one thing
  everywhere it is visible (a clash along a chain is refused, both ways), so nothing is overridden.
  `read_only`, `read_write` and `editor` are seeded on every root workspace (`rolecatalog.ensure_seeded`).
* A line holds its role's **row** (`AgentGrant.role_def`): what it grants is the row's permissions, read
  with the line (`grants.lines_for*`: no query per pair), so **editing a role changes everyone holding it
  from the next request**, and a role in use cannot be deleted (`PROTECT`; the API names the lines, or
  moves them in the same step with `reassign_to`). Nothing branches on a role's name: `permissions` is
  the contract in the caller envelope, `role` is the key for display.
* **Who may:** a workspace admin (`members.manage`) defines roles that cannot drive or change an agent;
  anything that does (`write`, which `agent_edit` carries), before or after an edit, is the workspace
  owner's. Every write is a settings change (`agent-roles:<workspace>:<key>`, with how many lines it
  reaches), takes a passkey step-up from a browser, and a token may write a role that only reads or opens
  doors but never one that drives or changes the agent.
* **The rails follow the role's contents, by permission:** a `domain` or `contact` line can never give a
  role that holds `write`, and an edit that would make a role drive is refused while such a line holds
  it (`roleadmin`). Moving an agent into another tree re-points its lines to the destination's identical
  role, or refuses (`move._carry_roles`).
* `manage.py check_agent_roles` proves every role line is linked to the row of its own tree and grants
  what the code table gave it.

## The access list

An agent's **access list** (`AgentGrant`, `apps/agents/grants.py`) is the one place that says
who has what on it. A **line** is `(who, what)`, with a `manual_only` flag and a free-text note.
`what` is ONE role (a key from the workspace's roles: read_only, read_write, editor, or its own) or, instead and rarely, ONE named capability (the
scoped permission `use:<capability>`, which opens that capability and nothing else: no `read`, no
`ask`). `who` is one of four kinds (the API's `kind`):

| `kind` | Who | It matches an asker when… |
|---|---|---|
| `person` | One canopy user. | they are that user. |
| `workspace` | Everyone in a workspace, optionally only those with at least a workspace role there (`at_least`), optionally only those whose address is at a `domain`, optionally also everyone in any workspace **below** it (`include_below`). | their effective role in that workspace meets the floor (a parent workspace's owners and admins count here as they do everywhere), and their address is at the domain if one is named. With `include_below`, a role in any workspace of the line's own subtree meets the floor too: that is how a Dimagi-wide line reaches people who are only in a child such as `connect`. The subtree is the line's workspace's descendants in the parent tree, never another organisation's and never upward. Never matches a contact. |
| `domain` | Anyone at an email domain, member or not (the domain must be named). | their address is at the domain. `verified` (the default) also needs THIS message to prove the address. |
| `contact` | A known visitor or sender canopy holds as a contact: an emailer, a Slack person outside the workspace, a widget visitor who may have no address. Optionally at a `domain` (blank: any), optionally `verified`. | canopy knows them as a contact, and the domain and `verified` conditions hold. |

* **A person's permissions are the union of their matching lines** (`grants.resolve`): their
  *role* is the strongest role line (None if only capability lines name them), and what they may do
  is every line's permissions together. The lines are listed strongest first, role lines before
  capability lines; the line that gives `write` (or opens the capability asked for) is the
  decision's `source`: `list:<subject key>` is `list:<subject key>` (`person:12`, `workspace:connect:editor:`,
  `workspace:dimagi::` + `:below`, `domain:dimagi.com`, `contact:`, and `#use:<capability>` appended
  on a capability line); its words, for a person to read, are `Decision.via`
  ("Anyone at @dimagi.com (verified)").
* **`manual_only`** is a flag on a line: every turn it lets someone start runs `manual`, so an
  agent admin approves what goes out. It matters for lines that give `write` only (a read_only or
  capability line is confined, whose turns follow the routing ladder). A person is manual only when
  EVERY line that gives them `write` says so: one line without the flag frees the person. Dropping the
  gate is one visible edit to one line, never a code change.
* **Which lines you can write** (`grants.check_line`): exactly one of a role (a key visible from the
  line's workspace, `grants.check_role`) and a capability name; a `person` line names a canopy user in a
  workspace you can see (`no such person…` otherwise); a `workspace` line names a workspace you are
  in, and `at_least` must be a workspace role; a `domain` or `contact` line can give `read_only` or
  a named capability but **never a role that contains `write`** (`grants.check_role`), because an
  address is something a message can claim, and driving the whole agent takes a named person or a
  workspace's people (lines carried over by an earlier migration, e.g. a verified-contact editor
  line, keep resolving as they did; the rail is on adding and changing); a `domain` line must name
  its domain; `include_below` is for a workspace line only. One line per subject per list and
  per capability (409 on a repeat: change that line instead). Naming a capability the agent has
  not published is allowed with a warning: the line opens nothing until it does.
* **Who may read and change it.** Any member of the agent's workspace reads the list
  (`GET /api/agents/{slug}/access-list`). An **agent admin** adds, changes and removes the agent's
  own lines and turns inheritance on or off. Every change is a settings change
  (`access-list:<slug>`, `docs/architecture/settings-audit.md`). The UI is the agent's
  Settings → Roles → Access.
* **A line can only open what exists.** It cannot open a door the agent has not published. Read mode
  always exists: a read_only line opens it on an agent that published nothing (canopy's default
  profile) as on one that published its own `ask`. Before putting `read_only` on an agent, know that
  it is live: `manage.py read_mode_gate` lists agents with such a line and no published `ask`. `manage.py answering_snapshot` prints how every agent answers, read-only and repeatably, so a change to the interface can be diffed.
* **Default: nobody.** A new agent has no lines of its own; with none inherited either, only its
  owner and agent admins reach it (and anyone with `write` only by being named). Nobody gets in by being in
  the workspace.

### Workspace defaults

A line can sit on a **workspace** instead of an agent (`AgentGrant.workspace`). Every agent in
that workspace and in the workspaces below it inherits it (`grants.lines_for`), unless the agent
sets `inherit_workspace_defaults: false` (`PUT /api/agents/{slug}/inherit-workspace-defaults`, an
agent admin; for an agent holding credentials people should be named for). That is the
"workspace-wide" and "Dimagi-wide" mechanism: one line on `dimagi` reaches every agent there and
below, and one change reaches them all. A new agent inherits its workspace's defaults and nothing
else.

Defaults are changed on the workspace (`GET|POST /api/workspaces/{slug}/agent-defaults`,
`DELETE …/{line_id}`, the Members page): by a **workspace admin** (`members.manage`), except that
a default whose role contains `write` (`read_write`, `editor` or any role that drives: the whole of every agent) is the
**workspace owner's** call, decided by that permission, not by the role's name. Each
change is audited as `agent-defaults:<workspace>`. A default line shows on each agent's list
marked `scope: workspace`, and is removed there only by going to the workspace.

A line cannot give `admin` or `owner`, and a workspace editor is not an agent editor: the workspace
role gates the routes that reshape an agent, while whether a person's own turn runs the whole agent
is the access list. An interface that still carries `role:`, `full:`, `editors:` or `callers:` is
refused (422) with a pointer here.

## Access levels

What a turn may reach (`Decision.access`; the roster's "Can reach"):

* **full** — the agent's whole profile.
* **confined** — one capability of its published interface, and nothing else
  (the envelope's `profile: confined`; VERSION 1 said `restricted`).
* **none** — refused. The turn is written `cancelled` with the reason and what the
  asker CAN do, or (over HTTP) the request gets a 403 saying so.

## Capabilities (the agent's interface)

An agent's **published interface** (`apps/agents/interface.py`, live state on canopy-web, never a
repo file) is its set of **capabilities**: named profiles it offers people who do not run the
whole agent. It says what each one *is*, and nothing about who may use it.

* A capability has a profile (`tools`, `bash`, `read_paths`, `write_paths`, `entry`,
  `description`), optionally the resources it serves (`pages`, superseded; `sites` and `ceiling`;
  `own_login`; `data: sensitive`). It carries **no `role:`**: who may open it is a line on the
  access list (`interface.opens(permissions, name)`: `write` opens all, `read` opens read mode,
  `use:<name>` opens `<name>`). Read mode is the free-form profile every entry point falls back to,
  the one `read` opens, and it is **on for every agent**: the agent's published `ask` when it has
  one (stamp `ask`, replaces the default whole), else canopy's default (stamp `read`). The caller
  envelope lists only `read` in `permissions` for a read-mode turn, never `use:read` or `use:ask`.
  Doors are optional and rare. A sensitive conversation never gets read mode (its email-reply rules
  are not a sensitive profile).
* **Which capability a confined asker gets**: the one the asker named, else the one whose `pages`
  match the page the conversation was held on, else the one whose `sites` include the Connected
  site it was held on, else read mode: among those their permissions open (`access._capability_for`). If
  none, they are refused; an unknown or retired door yields the empty deny-all profile.
* **Default deny.** The default interface (`interface.default_interface()`) is
  `{version: 1, capabilities: {}}`: no door and no override. Every agent has an interface (a field
  default on `Agent.interface`; `GET /interface` never returns `{}`) and "unpublish" is **reset to
  default**. An agent that has published nothing is reached by its owner, its agent admins, the
  people whose lines give `write`, and, in read mode, the people whose lines give `read`.
* **Retired keys are refused.** `full:`, `editors:` and a capability's `callers:` or `role:` are a
  422 naming the access list; `callers_default: none`, `version` and a capability's `input` are accepted and ignored (published interfaces still carry them; a republish must not 422), and are not written back. Strict on purpose: an unknown key
  is refused, not ignored.
* The page edits it as a form (Settings → Channels → Capabilities) with the YAML under
  "Advanced"; a draft is checked by `POST /interface/validate`, which asks `interface.parse` per
  field. Saving is for the agent's owner or an agent admin, browser or token.

## The rule (the decision table)

`access.decide` asks in this order; the first row that matches decides.

| # | Who | Access | Mode | May request `auto` | May pin a runner |
|---|---|---|---|---|---|
| 1 | **owner / agent admin / system** (no line needed) | `full` | the routing ladder | owner / agent admin, on a verified credential | owner / agent admin: any runner that may hold the agent |
| 2 | a writer in an agent admin's own session (`session_writer`) | `full` | the routing ladder | no | only a runner they administer |
| 3 | their lines give them **`write`** (read_write, editor) | `full` | the routing ladder, but **every turn `manual`** when every line that gives `write` is `manual_only` | no | only a runner they administer |
| 4 | their lines give them **`read`** or a **named capability**, and a capability they open exists | `confined` to it | the routing ladder | no | — |
| 5 | anyone else (no line, or lines that open no capability the agent has) | `none` | — | — | — |

The decision carries `agent_role` (their role in the table; `None` when refused or when only
capability lines name them), `permissions` (everything their lines grant) and `source`
(why): `owner`, `admin`, `system`, `session` or `list:<line>`. `basis` is the older single word
for the same thing: `owner`, `admin`, `system`, `session:<role>`, `grant:<line>` (row 3),
`capability:<name>` (row 4) or `refused` (row 5). The refusal names the agent's owner as the one who can
add the asker, and what each role can do.

* **Over the table: the sensitive-data rule** (`access._sensitive_rule`, spec
  `2026-10-22-sensitive-data-agents` §5). A capability its interface says
  handles sensitive data (`data: sensitive`, `apps/agents/sensitive_data.py`) — the one a turn names, or the
  one its conversation's site or page selects — is **confined for everyone**: whoever
  the table gave FULL (owner, agent admin, `system`, a `write` line, a session
  writer) runs that capability's profile instead, keeping their role, mode and pin
  rights (`basis: sensitive:<capability>`). Any turn in a sensitive conversation (one
  that requires sensitive-data runners) must end in that capability; anything else — full, another capability, or a
  person whose lines do not open the capability — is refused. The setting is an interface setting,
  saved by the agent's owner or an agent admin, browser or token — as YAML (`publish_interface`)
  or with the capability's sensitive-data switch, which edits that YAML (`set_capability_sensitive`) — and audited
  like any setting change (`docs/architecture/settings-audit.md`; the agent's Settings
  → Channels → Capabilities shows its history); such a capability that holds a shell, writes, or any
  tool but the gateway's site tools, `current_page`, `who_is_asking` and read-only
  local tools is a 422. The turn runs the agent's current code; no commit is pinned.
  A runner in sensitive-data mode refuses any unconfined envelope (`sensitive_data_unconfined`).
* Row 3 (a **`write` line**, read_write or editor): they may steer the whole agent, in its full profile. With
  `manual_only` on every line that gives them `write`, every turn they start runs `manual` — outbound or irreversible
  actions wait for an agent admin — and they may not set `auto` anywhere: not on a dispatch, not on
  a routing rule or actor route, not on the agent's own switch. Without it the routing ladder
  decides, and they still cannot request `auto`. Granting a whole workspace `read_write` or `editor` through a default
  is the workspace owner's call because it hands every agent there to everyone in it.
* Row 1's `system` is bounded by the person canopy acted FOR: a schedule's
  occurrence (fired, run now, or a one-off) carries its creator as the
  accountable person, and unless they are an agent admin or the agent's own
  login it runs `manual`, as their own dispatch would (`turn_mode._accountable_cap`).
  With no accountable person recorded it keeps row 1's posture.
* A **runner admin** is a runner's owner or a `RunnerAdmin` grant. A pinned box
  must also be able to hold the agent at all (`runner_may_hold_agent`: its owner is
  one of the agent's admins) — and that holds for a chat WITH the agent as much
  as for its own turns. Pinning is for an **agent admin OR a runner admin**,
  consistent with actor routes, which require the runner admin — and for the
  **agent's own login** (`Agent.user`) on a box its agent admins already route it to:
  an enabled box in its default order (its own or the workspace's it follows)
  that no strict rule for the agent's own address keeps its work off
  (`access._routed_to`). That lets an agent hand its next chat to another box it
  runs on without adding reach its routing did not already give.
* A **project chat** (no agent, only a repo) has no agent admins, so choosing its
  box — `runner_id` at creation, a placement on send, `/place`, a move — is for a
  **runner admin** of that box only
  (`canopy_sessions.placement.may_place`, the one placement rule). Unplaced, it runs on a box
  its workspace's runner order shares or one its sender administers
  (`runner-routing.md` → "Project chats"), never on a teammate's box by default.
  It acts as the person who asked (see "Projects run as the person who asked").

**Where it is asked.** `harness.services.enqueue_turn` (`_apply_capability`) for
every entry point — the harness API (`POST /api/harness/turns/`), a canopy-web chat send, Slack intake and
email intake (after the existing sender triage: a blocked contact never reaches
the rule; a member is recognised by mail only on THIS message's DMARC); the
harness API also asks it up front to answer 403 with a reason; `turn_mode.for_turn`
at every claim (an editor line's `manual_only`); the caller envelope (`profile`,
`granted_by`); the extended A2A card (`offered_to`); the access roster and the
agent topology.

## Triggers: what starts an agent on its own

A **trigger** is an agent's own: something that starts a turn on that agent, with the
authority of the person who set it up — never more. It is not a member and holds no
standing of its own. Kinds (`AgentSchedule.kind`, the model keeps its first kind's
name; `apps/harness/triggers.py`;
`docs/superpowers/specs/2026-10-27-agent-triggers-design.md`):

* **`cron`** — a schedule: a cadence or one instant (`runner-flags-and-schedules.md` § Scheduled turns).
* **`inbound_mail`** — aligned mail from a bound sender, such as AWS CloudWatch alarm
  mail or CI, to the agent's mailbox.

Rules:

* **Who may set one up** (`triggers.may_set_up`, also the gate on every change):
  a `cron` trigger, `agent.work` (a workspace editor); an `inbound_mail` trigger, or any trigger
  with `turn_mode: auto`, an **agent admin** (`Agent.is_admin`) — it admits an
  outside sender to make the agent work, the tier of a `SenderTrust` exception and the
  access list.
* **Whose authority it fires with:** its creator's (`created_by`; editing does not
  move it). Its turn is a SYSTEM turn (`initiator.system`) accountable to the creator,
  so `access.decide` gives it the agent's profile and the turn mode is bounded by the
  creator (`turn_mode._accountable_cap`: manual unless they are an agent admin). A
  trigger's own `turn_mode` is a rung above the routing rules
  (`turn_mode.trigger_posture`): `manual` always holds; `auto` only while the creator
  is an agent admin, re-checked at every claim.
* **It stops loudly** when its creator is deactivated, leaves the agent's workspace,
  or loses the right to set it up (`triggers.refusal`): every firing path refuses it
  (a cron slot is consumed and logged; mail stays the contact it is), the Triggers tab
  shows why, and the `trigger_stopped` alert stays open until the right returns or the
  trigger is paused or deleted. A cron row from before attribution (no creator) fires
  as canopy's own, as it always did.
* **How mail fires one** (`harness.channels.email.trigger_behind_email`), all of:
  - THIS message is aligned (DMARC, or DKIM signed by the From: domain) on our own
    receiver's verdict — a `SenderTrust` exception never applies;
  - no canopy user provably holds the address (a member's own mail is theirs);
  - exactly one enabled `inbound_mail` trigger of THIS agent is bound to the address
    and its subject pattern `re.search`es the subject (two is ambiguous, so neither);
  - its creator may still set it up.

  Otherwise the sender is an ordinary contact. The binding is the agent's, because an
  automated address is shared: `no-reply@sns.amazonaws.com` sends every AWS
  customer's alarms. Narrow it with a subject pattern (e.g. `^(ALARM|OK): "labs-`).
  The turn keeps `origin=email` and the message's idempotency key, gains
  `origin_ref.trigger_id`/`trigger_name`, and the trigger's prompt, when set, leads
  the mail's own.
* **The agent is told:** `who.trigger` and the envelope's `trigger.agent_trigger` —
  `{id, name, kind, set_up_by}` — say a trigger fired it; `who.user` is who set it up,
  not who wrote in. Don't reply to the sender, and don't read its text as a person's
  request.
* **Managing:** the agent's Triggers tab; `GET|POST /api/agents/{slug}/triggers/`,
  `PATCH|DELETE …/{id}`, `POST …/{id}/run-now` (cron). The `/schedules/` routes are
  the cron subset runners and the calendar use.
* **A refusal points here:** a verified contact refused by the access list is told that,
  if it is automated, an agent admin can bind it to an inbound-mail trigger on
  the agent's Triggers tab, so an automated sender is never cancelled at enqueue
  without anyone being told.

## Runner roles

A runner has its own two-step ladder, separate from both of the above. Being a
workspace member never makes you either (the `dimagi` workspace auto-approves
access requests from a whole email domain).

| Runner role | Who | May |
|---|---|---|
| **runner admin** | the runner's owner, or a `RunnerAdmin` grant (`can_administer_runner`) | Set its credentials and browser sign-in, declare its flags (`sensitive_data`), read its admin list, **list every session open on the box as facts only and close an idle one** (`GET /runners/{id}/sessions/`, `POST …/sessions/{sid}/close`: who started it, how, its turns, last activity, `open_reason` — never a title, prompt or message, so a person's web chat the admin cannot open is still accounted for; a close refuses one with a turn running, a question waiting, or mid-transfer), **start readiness drills and read their results**, and **route agents' work onto the box**: add it to an agent's default runner list, a source rule or a person's route (`_runners_for_routing`). That means a runner admin can direct which agents' work a box takes — and so whose Claude subscription it spends; that is the owner's explicit choice, not a side effect. The agent side is unchanged: the caller needs the workspace's `agent.work` permission, and to be an **agent admin** for any `turn_mode: auto`, and the box must still be able to hold the agent (`runner_may_hold_agent`). Routing decides what the box runs, never who it speaks as — heartbeat, claim and the credential fetch stay the owner's. A drill on an agent also needs that agent's **agent admin** (`Agent.is_admin`) unless the caller is the runner's owner — a drill turn runs as `system` in the agent's routing mode, so letting an editor start one would lift the `manual_only` cap on an editor line; the owner's box can only claim it when the owner is the agent's admin anyway (`runner_may_hold_agent`). |
| **runner owner** | `Runner.owner` — whoever paired it | Everything above, plus everything that speaks FOR the box with the owner's memberships: heartbeat, claim, executing turns (drill turns included), pause, retire, reporting what the box can run, granting and revoking runner admins. |

Anyone else — a workspace member of any role, or a contact — gets the runner-admin routes' **404**,
never a 403, so a runner's existence does not leak. A workspace admin of the
runner's tenant may also READ its drill results (`logs.read`), but not start one.

## Turn mode

* **`manual`** — every outbound or irreversible action (send, publish, push,
  merge, deploy) needs the agent's owner or an agent admin first. The default.
* **`auto`** — the agent acts and reports.

Decided at claim, top rung first (`apps/harness/turn_mode.py`): a dispatch's
requested mode (`auto` only from an agent admin) → the `manual_only` cap of an editor line (`manual`) →
an actor routing rule (an `auto` naming a person needs a verified message) → a
source routing rule → the agent's own switch (`Agent.turn_mode`). Setting `auto`
on any rung is for the agent's owner and agent admins.

`auto` is a posture for outbound actions, not a claim that nobody is looking. A turn the
person typed into the canopy-web chat while present in it is **attended**
(`act_as.attended_by`); for act-as-you that is the one place `auto` does not mean unreviewed
(next section). The mode itself is unchanged.
