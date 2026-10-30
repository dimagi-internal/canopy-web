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
`Decision(role, access, capability, may_request_auto, may_pin_runner, manual_only, basis, reason,
agent_role, source, via)`. Every entry point that makes an agent do work asks it (see "Where it
is asked" below); `tests/test_access_matrix.py` asks it through every entry point. Don't write a
second predicate for "may this person make this agent do X" — ask `decide`.

## The words

| Word | Means | Where it lives |
|---|---|---|
| **workspace role** | What a person may do in a workspace: viewer < editor < admin < owner < superuser. | `apps/workspaces/permissions.py`, `WorkspaceMembership.ROLE_RANK` |
| **agent role** | What a person may do with ONE agent, one ladder, strongest first: **owner** > **admin** > **editor** > **user** > **viewer**. Never "member" or "contact": those words describe how canopy knows someone, not what they may do. | `apps/agents/roles.py`, the only place that names them |
| **system** | Not a person and not on the ladder: canopy itself (a schedule, a drill), the agent's OWN canopy login, an approved task's dispatch. It may do what an agent admin may. | `roles.SYSTEM` |
| **permission** | What an agent role is made of: `read` (use a capability published for viewers), `write` (use one published for users, including one that acts), `run` (steer the whole agent), `edit`, `admin`, `own`. | `roles.py` |
| **access list** | The one list per agent that says who has which role: lines of `(who, role)`. | `AgentGrant`, `apps/agents/grants.py` |
| **line** | One entry of an access list. `who` is a person, everyone in a workspace, anyone at a domain, or a known visitor or sender. | "The access list" below |
| **workspace default** | A line set once on a workspace; every agent in it, and in the workspaces below it, inherits it unless the agent opts out. | `AgentGrant.workspace`, `Agent.inherit_workspace_defaults` |
| **capability** | A named, sandboxed profile an agent publishes (`ask`, ACE's `connect`): which tools, paths and sites a confined session may touch, and the lowest role (`viewer` or `user`) that may use it. It never says who: that is the access list's job. | the agent's interface, `apps/agents/interface.py` |
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
from the agent's identity to the person's (below); nothing else in a direct session asks
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
  site" below) and **canopy-web itself, in your own turn** (an agent you started asks,
  you approve read-only or read + write, and it acts as you on canopy for 30 minutes —
  "Acting as the person on canopy-web" below). Nothing else lets an agent act as a
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
    (below). Confined caller tokens
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
`dimagi` is set to **editor** for now while it bootstraps (`workspaces/0012`);
every other workspace is off. Turning it off or lowering it is a settings
change. The domains themselves are server-set, never client input.

## Agent roles

What a person may make ONE agent do, one ladder (`apps/agents/roles.py`), strongest first:

| Agent role | Holds | In plain words |
|---|---|---|
| **viewer** | `read` | Asks. Uses a capability whose lowest role is `viewer` (usually `ask`), and nothing else. |
| **user** | + `write` | Uses any capability the agent publishes for users, including one that acts. Never the whole agent. |
| **editor** | + `run`, `edit` | Steers the **whole agent**, in its own authority: every skill it has (the `full` access level). |
| **admin** | + `admin` | Configures the agent in canopy-web: its access list, capabilities, credentials, routing, runners, integrations. May ask for `auto` and pin any runner that can hold it. |
| **owner** | + `own` | Everything an agent admin may, plus transfer or delete the agent and name its admins. |

* `system` is not on the ladder (see "The words"). Another agent's login is not `system`: it is
  graded like any person, or agent A could steer all of agent B.
* **What `decide` reads.** `decide` asks one permission of a line's role: `run`
  (`roles.allows(role, roles.RUN)`): editor and above get the whole agent; user and viewer get a
  capability, chosen by `role:` on the capability. The routes that change an agent still gate on
  the workspace role (`agent.work`, an editor or above in the agent's workspace) or on
  `Agent.is_admin`; they do not read the agent role.
* **Where each role comes from.** The owner (`Agent.owner`, while still a member of the agent's
  workspace), agent admins (a workspace owner, or an `AgentAdmin` grant) and canopy itself hold
  their role with **no line**: `Agent.owner` and `AgentAdmin` remain the stores of owner and admin,
  and the access list shows them as **implicit** lines it does not let you edit. Every other role
  is a line on the access list. A line cannot give `admin` or `owner`.
* **An owner or agent admin who has left the workspace is neither** (`Agent.is_admin`,
  `caller_context.relationship_for_user`): both need current membership.
* Roles describe a person; what a *turn* gets also depends on who started it and how
  (`Decision.source`, below).

## The access list

An agent's **access list** (`AgentGrant`, `apps/agents/grants.py`) is the one place that says
who has which role on it. A **line** is `(who, role)`, with a `manual_only` flag and a free-text note. `who` is one of
four kinds (the API's `kind`):

| `kind` | Who | It matches an asker when… |
|---|---|---|
| `person` | One canopy user. | they are that user. |
| `workspace` | Everyone in a workspace, optionally only those with at least a workspace role there (`at_least`), optionally only those whose address is at a `domain`. | their effective role in that workspace meets the floor (a parent workspace's owners and admins count here as they do everywhere), and their address is at the domain if one is named. Never matches a contact. |
| `domain` | Anyone at an email domain, member or not (the domain must be named). | their address is at the domain. `verified` (the default) also needs THIS message to prove the address. |
| `contact` | A known visitor or sender canopy holds as a contact: an emailer, a Slack person outside the workspace, a widget visitor who may have no address. Optionally at a `domain` (blank: any), optionally `verified`. | canopy knows them as a contact, and the domain and `verified` conditions hold. |

* **The highest role among a person's matching lines wins** (`grants.resolve`). The winning
  line's `source` is `list:<subject key>` (`person:12`, `workspace:connect:editor:`,
  `domain:dimagi.com`, `contact:`); its words, for a person to read, are `Decision.via`
  ("Anyone at @dimagi.com (verified)").
* **`manual_only`** is a flag on a line: every turn it lets someone start runs `manual`, so an
  agent admin approves what goes out. It matters for `editor` lines only (a user or viewer is in a
  capability, whose turns follow the routing ladder). Several lines at the winning role count as
  manual only when ALL of them say so: one line without the flag frees the person. Dropping the
  gate is one visible edit to one line, never a code change.
* **Which lines you can write** (`grants.check_line`): roles `viewer`, `user` or `editor`; a
  `person` line names a canopy user in a workspace you can see (`no such person…` otherwise); a
  `workspace` line names a workspace you are in, and `at_least` must be a workspace role; a
  `domain` or `contact` line can give `viewer` or `user` but **not `editor`**, because an address
  is something a message can claim, and running the whole agent takes a named person or a
  workspace's people (lines the migration below carried over as they were are the exception); a
  `domain` line must name its domain. One line per subject per list (409 on a repeat: change that line instead).
* **Who may read and change it.** Any member of the agent's workspace reads the list
  (`GET /api/agents/{slug}/access-list`). An **agent admin** adds, changes and removes the agent's
  own lines and turns inheritance on or off. Every change is a settings change
  (`access-list:<slug>`, `docs/architecture/settings-audit.md`). The UI is the agent's
  Settings → Roles → Access.
* **A line can only open what exists.** It cannot open a capability the agent has not published:
  an agent with no `ask` capability cannot be asked by a viewer even if the list names them.
* **Default: nobody.** A new agent has no lines of its own; with none inherited either, only its
  owner and agent admins reach it (and editors only by being named). Nobody gets in by being in
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
a default giving `editor` (the whole of every agent) is the **workspace owner's** call. Each
change is audited as `agent-defaults:<workspace>`. A default line shows on each agent's list
marked `scope: workspace`, and is removed there only by going to the workspace.

### Where the old rules went

Before this list, access was spread over the interface's `full:` rules, each capability's
`callers`, `editors: confined` and the implicit rule that a workspace editor is an agent editor
whose every turn is manual. Migration `agents/0112` moved every agent's rules onto its list
(`full: member` → everyone in the agent's workspace as `editor`; contact classes → `contact`
lines; a capability's callers → lines at `viewer` if it was open to contacts, else `user`; the
implicit editor rule → "everyone in the workspace, at least editor → `editor`, `manual_only`",
unless the agent said `editors: confined`), gave each capability its `role`, stripped the old keys
and kept each agent's old interface and source in the settings audit. The old rules are gone from
the code: an interface that still carries `full:`, `editors:` or a capability's `callers:` is
refused (422) with a pointer to the access list. One consequence worth knowing: **a workspace
editor is no longer an agent editor.** The workspace role still gates the routes that reshape an
agent; whether a person's own turn runs the whole agent is the access list.

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

* A capability has a profile (`tools`, `bash`, `read_paths`, `write_paths`, `entry`, `input`,
  `description`), optionally the resources it serves (`pages`, superseded; `sites` and `ceiling`;
  `own_login`; `data: sensitive`), and **`role: viewer | user`**: the lowest agent role that may
  use it (`user` when it says nothing). `ask` is the free-form capability every entry point falls
  back to; the Capabilities form can add a standard read-only `ask`.
* **Which capability a viewer or user gets**: the one the asker named, else the one whose `pages`
  match the page the conversation was held on, else the one whose `sites` include the Connected
  site it was held on, else `ask`: among those their role may open (`access._capability_for`). If
  none, they are refused.
* **Default deny.** The default interface (`interface.default_interface()`) is
  `{version: 1, capabilities: {}}`: nothing published. Every agent has an interface (a field
  default on `Agent.interface`; `GET /interface` never returns `{}`) and "unpublish" is **reset to
  default**. An agent that has published nothing is reached by its owner, its agent admins and the
  editors its access list names, and by nobody else.
* **Retired keys are refused.** `full:`, `editors:` and a capability's `callers:` are a 422
  naming the access list; `callers_default` may only be `none`. Strict on purpose: an unknown key
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
| 3 | a line gives them **editor** | `full` | the routing ladder, but **every turn `manual`** when the winning line is `manual_only` | no | only a runner they administer |
| 4 | a line gives them **user** or **viewer**, and a capability their role opens exists | `confined` to it | the routing ladder | no | — |
| 5 | anyone else (no line, or a line whose role opens no capability) | `none` | — | — | — |

The decision carries `agent_role` (their place on the ladder; `None` when refused) and `source`
(why): `owner`, `admin`, `system`, `session` or `list:<line>`. `basis` is the older single word
for the same thing: `owner`, `admin`, `system`, `session:<role>`, `grant:<line>` (row 3),
`capability:<name>` (row 4) or `refused` (row 5). The refusal names the agent's owner as the one who can
add the asker, and what each role can do.

* **Over the table: the sensitive-data rule** (`access._sensitive_rule`, spec
  `2026-10-22-sensitive-data-agents` §5). A capability its interface says
  handles sensitive data (`data: sensitive`, `apps/agents/sensitive_data.py`) — the one a turn names, or the
  one its conversation's site or page selects — is **confined for everyone**: whoever
  the table gave FULL (owner, agent admin, `system`, an editor line, a session
  writer) runs that capability's profile instead, keeping their role, mode and pin
  rights (`basis: sensitive:<capability>`). Any turn in a sensitive conversation (one
  that requires sensitive-data runners) must end in that capability; anything else — full, another capability, or a
  person the capability's role does not admit — is refused. The setting is an interface setting,
  saved by the agent's owner or an agent admin, browser or token — as YAML (`publish_interface`)
  or with the capability's sensitive-data switch, which edits that YAML (`set_capability_sensitive`) — and audited
  like any setting change (`docs/architecture/settings-audit.md`; the agent's Settings
  → Channels → Capabilities shows its history); such a capability that holds a shell, writes, or any
  tool but the gateway's site tools, `current_page`, `who_is_asking` and read-only
  local tools is a 422. The turn runs the agent's current code; no commit is pinned.
  A runner in sensitive-data mode refuses any unconfined envelope (`sensitive_data_unconfined`).
* Row 3 (the **editor line**): an editor may steer the whole agent, in its full profile. With
  `manual_only` on the winning line every turn they start runs `manual` — outbound or irreversible
  actions wait for an agent admin — and they may not set `auto` anywhere: not on a dispatch, not on
  a routing rule or actor route, not on the agent's own switch. Without it the routing ladder
  decides, and they still cannot request `auto`. Granting a whole workspace `editor` through a default
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
  (`runners.md` → "Project chats"), never on a teammate's box by default.
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

* **`cron`** — a schedule: a cadence or one instant (`runners.md` § Scheduled turns).
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

## Direct use of an agent

A person may run an agent in their **own** Claude Code, with the agent's plugin and
skills, and canopy keeps the record (`apps/harness/local.py`). The word for it is
**direct** (against a session a runner box claims). The stored names stay `local`:
`Turn.origin = local`, `LocalTurnIn`, `/api/harness/local-turns/`; prose and screens say
direct (a direct turn).

* A **private runner** (`PrivateRunner`) is a person's machine: plain Claude Code with
  the canopy plugin, no runner app. It is registered once with the person's own
  token (`canopy private-runner register`) and belongs to the PERSON — one serves
  every agent they use. On its own it never claims a turn and nothing routes to it.
* When the machine also runs the runner app, the two are **one private runner**:
  `PrivateRunner.runner` links the registration to that `Runner`, and the runner is
  marked **`private`** (`PUT /api/harness/runners/{id}/private`, owner only). A
  private runner takes no fleet work — no routed turn, no workspace order, no source
  rule (`Runner.directed_only`) — only what its owner pins or binds to it, plus the
  direct sessions on it. Its device key is what those sessions prove themselves
  with, and it may read an agent's secrets while its owner holds an active license
  (`device_keys.private_runner_licensed`), for that owner's own turns only.
* A machine **without** the runner app still gets its own key for reading the
  agent's vault: registering it (`POST /api/private-runners/`) gives it a
  `computer` runner (`Runner.COMPUTER`, `local.ensure_computer_runner`, returned as
  `runner_id`) to enrol a device key on. The key is owner-approved like a private
  runner's and serves exactly the agents its owner administers or holds an active
  license for; its token reaches only the owner's direct turns and license reads
  (`runner_auth._COMPUTER_RULES`), and the computer appears in no fleet runner list.
* **Acting as a lower role.** `acts_as` (`editor` or `viewer`, workspace roles) is a property of
  ONE turn or of one client's requests — never a setting left on a machine:
  - a turn started with it (`TurnIn.acts_as`, `canopy agent dispatch --as editor`;
    `LocalTurnIn.acts_as` for a direct session) is judged at that role from start to
    finish — whether it may start at all, its mode (`auto` becomes `manual`), its
    envelope, and every read made during it (`X-Canopy-Turn-Id` by its initiator,
    or the turn of the caller token it calls MCP with — REST and MCP alike).
    The server stamps `origin_ref.acts_as`; a client's own `origin_ref` cannot.
  - a client puts ITSELF in a lower mode with `X-Canopy-Acts-As` on each request —
    the canopy CLI and the runner send it when started with `CANOPY_ACTS_AS`. A
    runner claiming in that mode stamps the cap on its owner's turns it claims
    (never loosening one a turn was started with); someone else's turn keeps its own.
  The cap rides on the user object (`apps/workspaces/role_cap.py`) and is honoured
  where roles are read — `member_role`/`member_roles`/`membership`,
  `Agent.is_admin`, `relationship_for_user`, `caller_runs_agent` — so a capped owner or agent
  admin is neither, and `decide` gives them only what the access list does (their own lines,
  and any workspace line at the lowered workspace role). It only ever lowers, so anyone may ask
  for it with no proof. It is how anyone, an agent included, runs the path of someone who is
  not an agent admin for real.
* **No PAT on a private runner.** The owner's direct sessions there sign in with the
  box's own device key (its runner token), which on a private runner also reaches the
  local-turn routes (`runner_auth._PRIVATE_RULES`) — never `/api/private-runners`,
  which takes the person's own token — and reads credentials under the owner's
  license. Such a turn's initiator assurance is `private-runner-key` (verified, like
  a PAT). A shared runner's token opens no direct session and reports on none, and a
  private runner's reaches only its local-turn routes, for its own registration.
  Only the owner registers or approves a device key on a private runner
  (`runners.may_trust_device_keys`); its runner admins still see and may revoke them.
* A **direct turn** (`Turn.origin = local`) is one session there with one agent,
  opened by the canopy plugin's SessionStart hook
  (`POST /api/harness/local-turns/`). It is a real turn: the person is its
  initiator (verified, `via=local`), it is RUNNING from the start and never
  claimed, and it carries the same caller envelope a runner turn gets
  (`trigger.private_runner` names the machine; `trigger.runner` is null).
* **Who may.** Exactly whom `decide` gives the WHOLE agent (rows 1–3): its owner
  or an agent admin, anyone with an editor line. A user or viewer, or a contact, is refused
  (403) and keeps the confined path through canopy.
* **Mode.** `manual`, always — except an agent admin who asks for `auto`. Stamped
  at open, like a dispatch's request; an editor does not get the
  routing ladder here, because nothing about a session on their own machine is an
  entry point the owner set a posture for.
* **It never holds the agent.** A direct turn is the person's own session: it neither waits
  behind the agent's runner turns nor delays them, and two people may use the agent at once.
  (No agent turn holds the agent either: an agent is not a lane.)
* A **license** (`AgentLicense`, `apps/agents/credentials/licenses.py`) is person × agent: the
  record that someone uses the agent directly. The access list decides who MAY
  (`decide`, above); the license is who DOES. Requested on the first direct turn and
  issued at once when `decide` gives the whole agent (basis `owner`, `admin` or
  `grant:<line>`, an editor line); anyone else gets a `requested` row the agent's admins
  see, and a refusal. An agent admin **revokes** it per person without editing the
  rule, and a revoked license is never re-issued automatically; **approve**
  reinstates it, re-asking `decide`, so it cannot license someone the access list
  does not give the whole agent. `GET /api/agents/{slug}/licenses` (any member)
  lists holders, their private runners, direct turns, credential reads and last use;
  the agent's Settings → Roles → Direct use shows it.
* **Credentials.** `/credentials/resolve` has a third principal beside `runner` and
  `admin`: **`license`** — an ACTIVE license (re-checked against `decide` on every
  read) AND one of the caller's OWN live turns on that agent named in
  `X-Canopy-Turn-Id`: their running direct turn, or — on their private runner's
  token — a turn of theirs that runner claimed. No turn, no secrets. A request naming
  that turn is a `license` read even from an agent admin or a runner owner. What it is
  handed is `authority`'s direct column: never the agent's canopy login (graded `system`,
  past an editor line's `manual_only` cap), the owner's GitHub delegation, or lent or shared
  credentials. Each read is logged with `via=license`,
  the turn and the license. Secrets read this way reach the person's machine;
  nothing brokers each call through canopy-web.
* **Reporting.** Only its initiator may renew, finish or append its transcript —
  there is no box. The lease is 8 hours, renewed by the session's prompts; a
  session that dies is swept `lost` like any turn.

## What a caller's turn tells the owner

A confined turn can act on nothing, so a caller's procedure ends its final
message with one line, `For <Agent>'s owner: <the ask>`, and tells the caller
the request is noted where the owner reviews it. The runner lifts that line out
(the laptop from the `cx-` session's `Stop` hook, whose profile names the turn;
the cloud box from the finished reply) and sends it as an `owner_ask` turn
event. canopy-web re-parses it and appends it to the turn's `result_note`, for a
confined turn only and once per line (`apps/harness/owner_ask.py`). The note is what every view of the turn shows the people who
may read it; the transcript stays the record.

## Admission rules

Some combinations of the above can never complete, and canopy refuses them
before any session starts instead of spending one (`apps/admission`). The worked example: an email from a **confined** caller, in
**manual** mode, claimed by a **cloud** runner. The ask procedure drafts a reply
and waits for a human yes, and a cloud session that ends cannot receive one.

* **What a rule is.** A row on a workspace (`AdmissionRule`), edited by its
  workspace admins (`admission.manage`) at `/w/:workspace/settings/admission-rules`. Its
  conditions are any subset of: agent, origin (the `Turn.origin` vocabulary),
  principal (who asked, in this condition's own words: `member` is a canopy user, `contact` an
  outside person with no account, `agent`, `system`; it is not an agent role), access
  (`confined`/`full`), capability, turn mode, runner kind
  (`emdash`/`cloud`/`remote`), and reach (`paused`/`offline`, below). A blank
  condition means any. A rule needs at
  least one. Its action is `reject`, `allow` or `auto` (below). It also carries a reason and two
  notice switches (sender, owner), and it can be enabled or disabled.
* **Where it is evaluated.** At enqueue, beside `_apply_capability`, for rules
  that need no runner (no turn-mode or runner-kind condition). And in
  `claim_next_turn` right after `turn_mode.for_turn`, the one point where the
  runner's kind and the resolved mode are both known; both claim channels pass
  through it. Rules apply to agent turns (an agent's own, or its chat's), and
  only the agent's own workspace's rules apply.
* **Precedence.** An enabled `allow` that matches exempts the turn from every
  `reject`. That is how one agent opts out of a workspace-wide rule. A disabled
  rule matches nothing, so a disabled agent-narrowed copy of a reject does NOT
  exempt anyone. At enqueue, a condition not known yet counts as matching for an
  `allow` and as not matching for a `reject`, so the claim decides. Among
  matching rejects, the most specific wins, then the oldest.
* **Run in auto mode** (`action: auto`). This is the third action, and it is
  decided at claim before the rejects (`evaluate.auto_rule`). A matching turn
  whose mode resolved **manual** runs **auto** instead. The basis reads
  `admission rule <id>: <reason>`, the rule's fired count rises, and the event
  log gets `admission.auto` once the claim lands. It raises only the agent's
  STANDING posture (`turn_mode.Resolved.raisable`: a routing rule or
  `Agent.turn_mode`). It never raises an editor's cap, a dispatch that asked for
  manual, or auto withheld from an unverified message. A session that started
  manual is raised only when nothing about THIS turn holds it back: its own rungs
  resolve to the standing posture, the message is verified, and the caller is not
  confined (`turn_mode._carried`). Otherwise a thread first answered manually on a
  laptop runs manual on the cloud, where nobody can approve its reply. It cannot
  be about refused or unclaimed turns. The use: `ace,
  cloud → auto`. A runner list puts the laptops first, so the cloud only takes
  the turn when no watched laptop is free, and there it runs instead of waiting
  for an approval nobody will give.
* **What a rejection does.** The turn closes **CANCELLED**, not FAILED, so no
  debugger turn starts. `result_note` reads `rejected: admission rule <id>
  (<conditions>) — <reason>`, the ledger's status event carries the rule id, the
  workspace event log gets `admission.rejected`, and the rule's fired count and
  last-fired time move. No runner ever holds the turn.
* **The notices** (`AdmissionNotice`, one row per notice, sent or not). They
  are fixed text and never model-written:
  * The **sender** gets an automatic reply on their email thread, from the
    agent's own mailbox. On a Gmail thread canopy-web never sends it itself (it
    holds no mailbox token it can open): the notice is left `pending`, and
    the runner that reads the agent's mailbox leases it on its next mailbox poll
    (`POST /api/harness/runners/{id}/admission-notices`, only for agents it may
    hold and only from the mailbox the sender wrote to), sends the same fixed
    text through its own gog client with the same threading and
    `Auto-Submitted: auto-replied`, and reports it
    (`apps/admission/runner_notices.py`; the runner side is
    `runner/canopy_runner/canopy_runner/admission_notices.py`). A runner
    failure leaves it pending for the next lease (15 minutes); one that no
    runner manages within 24 hours ends `failed`, saying why. A send that may
    have gone out (a timeout) is never retried. There is no reply to a machine
    (Auto-Submitted, Precedence bulk/list, list mail), a no-reply address, the
    agent's own mailbox, a system sender, or canopy/another agent. At most one
    reply goes per thread per 24 hours. It is **off** in each workspace until
    one of its workspace admins turns it on, on the Admission rules page
    (`AdmissionSettings`, `PUT /api/workspaces/{slug}/admission-settings`,
    logged to the event log); while it is off, the notice is recorded as
    `held`.
  * The **owner** gets, off the thread: an email (SES), plus a review ask on
    the agent's board parked on them (Waiting on you). Both name who wrote, the
    subject and thread, the rule and its reason, what the sender was told (a
    `Reply:` line: sent, queued for the runner, off in this workspace, or why
    not), and links to fix it: the rule
    on the settings page (`?rule=<id>`, highlighted), the agent's turn mode and
    runners, its access list, and its turns.
* **Defaults.** Every workspace is seeded with two rows (migration
  `admission/0002`, and at creation for every new one). The first rejects email + confined + manual + cloud, with both
  notices, and is **on**. The second is the same on a laptop (`emdash`) runner
  and is **off**. From then on they are ordinary rows. Disable one, narrow it
  with an `allow`, or delete it (a deleted default is not re-seeded).
* **Refusals as rules.** canopy refuses two
  kinds of turn on its own, before any rule: mail from a contact the workspace
  **blocked** (`channels.email.refused_email_turn`), and a person the agent offers
  **nothing** (no line gives them a role that opens a capability) (`_apply_capability`, access `none`). Those are access
  decisions, so they stand whatever the rules say: no `allow` can cover them,
  and disabling their rule does not let them run. Each is reported through a
  rule instead (`evaluate.at_refusal`): conditions `sender: blocked` or
  `access: none`, which also takes origin, principal, capability and agent but
  never a runner or a mode (a refusal comes before either). A matching enabled
  rule records the ledger event and the fired count and sends its notices, while
  the turn keeps the refusal's own `result_note`. Migration `admission/0005`,
  and every new workspace, seed `blocked-sender` and `offered-nothing`, each
  with **every notice off**, so by default these refusals stay silent. A blocked
  sender gets no reply unless a workspace admin turns one on.

* **Reach rules: no runner will take it.** `reach: paused` matches a turn when
  every runner that could take it (`harness.claim.turn_reach`, the claim's own
  coverage) is paused with no `unpause_at`; `reach: offline` when none is
  reachable and the turn has waited the rule's `wait_hours`. A pause WITH a
  resume time matches neither: the turn waits, and its status says
  `waiting_runner` with `parked` and `held_until`. Reach is asked at enqueue
  when such a rule is in scope; again for the queued turns a runner could take
  the moment it is paused open-ended (`harness.services.pause` →
  `evaluate.recheck_stranded`); and by `evaluate.sweep_unrunnable` on the
  off-request clock (every 5 minutes at most) for the offline wait. A claim
  never matches one. A reach rule cannot be an `allow` or name a runner kind or
  mode. The rejection's `result_note` and the owner's notice (a `Runners:`
  line) name each runner's situation (`claim.runner_situation`): who paused it
  (`Runner.paused_by`), when, its note and its resume time, or since when it was
  last heard from. A chat, Slack or MCP asker reads that on the turn
  (`turn_status.rejection` → `TurnStatus.detail` on a `cancelled` turn).
  Migration `admission/0008`, and every new workspace, seed `no-runner-paused`
  and `no-runner-offline` (24h): ordinary rules, **on**, owner notice only. A
  workspace admin who disables them gets turns that sit queued instead.

## The caller envelope

What the runner hands the agent beside each turn (`apps/harness/caller_context.py`,
VERSION 3 — 3 only added `person` and `trigger.kind`, so a v2 reader is unaffected): `who`, `verified` (THIS message), `role` and `source`
(below), `relationship`, `profile` (`full` | `confined`), `granted_by`, `capability` (the confined
profile), `turn_mode` (`{mode, basis}`), and:

* **`role`** — where the asker stands on the agent's role ladder: `owner` | `admin` | `editor` |
  `user` | `viewer` | `system`, null when refused. **`source`** — why: `owner` | `admin` |
  `system` | `session` | `list:<line>` (the access-list line that gave it, e.g.
  `list:domain:dimagi.com`), `""` when refused. An agent says "you are a viewer here because …"
  from these, instead of guessing. Added without a version bump.
* **`granted_by`** — the same answer as one word: `owner` | `admin` | `system` |
  `session:<role>` | `grant:<line>` (an editor line) | `capability:<name>` (a user or viewer in
  that capability) | `sensitive:<capability>` | `refused`, and `no-interface` for a turn with no
  agent.
* **`relationship`** — an older, coarser field, kept for readers that have not moved: `owner` |
  `admin` | `system` for those, and `member` or `contact` for everyone else (in the agent's
  workspace, or not). It is how canopy knows the asker, not a role: it says nothing about what
  they may do. Read `role`.

* **`ship_grant`** — when ANOTHER agent's verified login that is this agent's
  owner or an agent admin dispatched the turn at it, push / PR / merge in this agent's OWN
  repo are pre-approved even in `manual`. Nothing else is.
* **`sender_trust`** — `{email, reason, expires_at, active}` when an email turn's
  sender was tied to a member although THIS message is not aligned, by a temporary
  `SenderTrust` an agent admin set for that address (`verified` stays false); null
  otherwise.
* **`unproven_member`** — `{email, role, this_message_grade, needs, note}` when an
  email turn's sender stayed a `contact` although the address belongs to exactly
  one canopy user who is a member of the agent's workspace (the lookup member
  linking uses, `harness.channels.email.address_holder`): THIS message graded below
  `needs` (`dmarc` | `dkim_aligned`, `Contact.EMAIL_ALIGNED`), so it could not be
  tied to their account. It tells the agent (and the owner) "your own member,
  whose domain's mail authentication needs fixing", not "a stranger". It grants
  nothing and changes no other field; null otherwise, including for a blocked
  contact and any non-email turn. canopy also LOGS it, once, on
  the turn's event ledger at enqueue: a `TurnEvent` of kind `unproven_member`
  whose payload is this same object, read by `read_turn_events` and shown (with
  the note) in the activity drill-down. canopy writes it; a runner cannot post
  that kind.
* **`person`** (v3) — what canopy knows about the HUMAN asking:
  `{id, display_name, email, workspace, facts[], see_all}` (plus `hcp`, `grant`, `recall`, `record`, `projects`).
  null only when the asker is not a human (canopy or one of the agent's triggers,
  another agent's login, unknown). See "What agents know about a person" below.
* **`trigger.kind`** (v3) — what canopy-initiated work the turn is
  (`origin_ref.trigger`); null otherwise. **`trigger.agent_trigger`** —
  `{id, name, kind, set_up_by}` when one of the agent's own triggers fired the turn
  (see "Triggers"); null otherwise. The same object is in `who.trigger`. **`trigger.matched_classes`**
  is kept for agents whose skills still read it and is always `[]`: the caller classes it listed
  are gone, and `role` and `source` say which line admitted the asker.
* **`conversation`** — `{session_id, thread_id, subject, slack}`. `slack` (v3,
  additive) is where in Slack the conversation is, whenever its session holds a
  Slack thread (born there, or bound by a share): `{team_id, channel_id,
  channel_name, thread_ts, permalink, is_dm}`; null otherwise. Read from the
  session's cache (`apps/slack/location.py`) — building the envelope never calls
  Slack. `channel_name` needs the install's `channels:read` / `groups:read`
  unless a slash command named it, and is null
  for a DM; `thread_ts` is null for a DM; `permalink` falls back to the web-client
  link. With `turn_id`, the agent may act on that conversation: see "Agents and
  their own conversation" in `api-surface.md` (`start_new_slack_thread`).

Readers accept both spellings: `relationship: caller` means `contact`, `profile:
restricted` means `confined` (`normalize_relationship`, `normalize_profile`; the
runners and the canopy plugin's hooks do the same). A runner gets confined turns
only if it reports `envelope >= 2` (its code reads `confined`) and `profiles >= 3`
(its guard confines writes) on every heartbeat.

## How confinement is enforced

Who asked is recorded on every turn (`initiator_*`), and the caller envelope
(above) reaches the agent three ways: in the claim response, which the runner
writes to `~/.canopy/caller/<turn_id>.json`; through the `who_is_asking` MCP
tool; and beside every delivered prompt — the runner leaves
`~/.canopy/caller/pending/<task>.json` before the send and the canopy plugin's
`caller_context` UserPromptSubmit hook adds a summary next to the person's words
(cloud: `CANOPY_CALLER`).

* **Default deny.** An agent with no capability published and no editor line is reached only by
  its owner and agent admins; every viewer, user, contact or unidentified
  caller is refused (row 5). The interface is live state on canopy-web —
  never a file in the agent's repo; the owner or an agent admin edits it on the agent. It
  confines callers who reach the agent through canopy (web chat, Slack, email,
  embedded sites). canopy's MCP server offers NO per-agent tools: someone who
  wants to use an agent installs its plugin and runs it in their own Claude
  Code, as themselves (`/get-started`).
* **An agent's own login is the agent.** `Agent.user` grades as `system`;
  another agent's login grades like any person, or agent A could steer all of B.
* **Three layers, each failing closed.** (1) canopy stamps `Turn.capability` at
  enqueue (`harness.services._apply_capability`) or refuses the turn; (2) only a
  runner reporting `profiles` (and `envelope >= 2`) may claim such a turn, and it
  runs it in its own `cx-` session with the profile written to
  `~/.canopy/profiles/<task>.json` first; (3) the plugin-wide `profile_guard`
  PreToolUse hook refuses anything the profile does not list — Bash
  argument-for-argument, `{thread_id}`-pinned, paths realpath'd.
* **Native deny rules under the guard.** The runner also writes the capability as
  Claude Code's own DENY rules (`runner/canopy_runner/canopy_runner/native_permissions.py`
  → the `cx-` worktree's `.claude/settings.json`; `--settings` on cloud). Fleet
  sessions run with `--dangerously-skip-permissions`, so only deny rules take
  effect natively: whole tools, MCP servers and high-risk programs the capability
  never grants. Exact-argument scoping stays the hook's job.
* **A confined session never holds the runner owner's PAT.** It reaches canopy's
  MCP with a **caller token** (`apps/harness/caller_tokens.py`, issued by
  `harness.claiming.issue_credentials`, `cct_…`), minted per confined turn at claim
  and bound to its conversation; the canopy plugin's `headersHelper` sends it from
  the session's profile. It resolves to the conversation's CURRENT turn: tools run as
  that turn's caller (no canopy user at all for an outside contact), and only the
  tools the capability lists exist (`apps/mcp/turn_scope.py`, `agent ∩ caller`).
  Expired, long-finished, or a conversation whose current turn is not confined → it
  authenticates nothing; `who_is_asking` answers only about its own conversation.
* **A confined turn holds no credentials it does not need.** canopy refuses it
  the GitHub token delegated to the agent (`agents.credentials.delegations.github_token_for_turn` → 409).
  The cloud runner builds its env from an allowlist (`_confined_env` in
  `runner/ec2/cloud_runner.py`: Claude's login, locale/paths/TLS, which agent and
  turn) — no `CANOPY_TOKEN`, no agent `.env`, no vault key (it never calls
  `credentials/resolve`), no chat key; gog's keyring password only when the
  capability's own bash rules run the mail path (`canopy email …` /
  `bin/<slug>-email`).
* **A colleague's FULL turn reads as the colleague.** A full-profile turn not
  asked by the agent's owner, an agent admin or canopy (an editor line, a
  writer in an agent admin's session — `caller_tokens.is_caller_scoped`)
  gets a **scoped** caller token (`CallerToken.scoped`): every READ tool exists, plus
  driving the asker's open page (`all_tools` + `read_only`; a write is refused — the
  asker granted no act-as-you permission), and each runs as the conversation's current asker, so
  `turn_access.can_read_turn_content` and the session ACL decide what
  `harness_list_turns` / `read_turn_messages` / `read_turn_transcript` return.
  The cloud runner leaves it at `~/.canopy/scoped/turn/<turn id>.token` and sets
  `CANOPY_SCOPED_TURN` (`_scoped_token_env`); the laptop runner leaves
  `~/.canopy/scoped/task/<emdash task>.token` from `execute._confine`
  (`scoped_token.py`) and clears it for a later turn with none. The plugin's
  `canopy-web-mcp-headers.js` sends it and never falls back to a PAT once it has
  one; a write failure refuses the turn. A scoped token does not serve a
  confined turn's conversation, and a confined token does not serve a full turn's.
  **REST takes a scoped token too, but only where MCP would** (canopy-web#1782,
  `tokens.middleware._authenticate_caller_token`): re-resolved on every request
  as above, it signs in as the asker on a route the MCP server serves as a tool
  (`api_tools.is_tool_route`) and is refused (403) on every other one. The routes
  MCP leaves out (minting a PAT, the person's own memory switches and grants, the
  runner protocol) stay out of reach of a session acting for someone. A confined
  token is refused on REST (401): its capability's tool list is an MCP filter, and
  REST has no equivalent to apply. This is what lets the canopy CLI in a scoped
  session read as the asker instead of failing for want of a PAT; like MCP, REST
  refuses an agent turn's token every write but driving the page (non-GET → 403);
  a repo chat's token keeps its writes.
* **Projects run as the person who asked.** Agents and projects are both first
  class, and both act FOR the person who asked; they differ only in WHO the
  session is. An agent's turn acts as the agent, bounded by the asker (the rule
  above). A project turn or project chat (a repo, no agent) has no agent to act
  as, so it acts as its asker: a verified, active human initiator gets the same
  scoped caller token (`caller_tokens._project_asker`), so its session acts on
  canopy with exactly that person's access — nothing more, and never the box's (the
  cloud runner strips its own `CANOPY_TOKEN` from the turn). Unlike an agent's caller
  token it may write: a repo chat is the person's own tool.
  The token follows the conversation's current asker and voids when a later turn
  has none verified. A project turn with no person behind it (a schedule, a
  drill) gets no token and no canopy identity. GitHub is the one thing a project
  borrows from an agent: its repo's `RepoIdentity` agent, for project dispatches
  and project chats alike (`delegations.turn_agent`; `runners.md` → "The GitHub
  identity a turn gets"). The cloud runner deletes the token when the turn ends.
* **Known limit.** On a laptop every session is the same OS user and emdash
  spawns it, so the runner cannot strip its environment; on any box, credential
  FILES (agent `.env`s, gog's keyring, ace's Drive key) stay readable to an
  escaped session. The guard and the native deny layer are what keep a confined
  session off them.

Spec: `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-and-access-design.md`.

## Acting on behalf of the person who asked

An agent's turn does the work of the person who started it, and canopy-web carries that
person without being told (`apps/harness/turn_asker.py`). A request authenticated with
the login canopy-web issued for a turn (`TurnAgentToken`) works for that turn's person;
the bearer middleware works them out on every such request, and nothing the agent sends
can add or remove them. The person is found only when the turn that login serves now
(its own, or a session login's newest turn) is executing, unconfined and runs for this
agent, and was started by a verified, active human who is not an agent — or was itself
started on such a person's behalf, so a chain of agents keeps them. The request must not
have lowered itself (`acts_as`), and the person's own turn cap applies to them. Anything
else — a stored PAT, a turn canopy itself started, an unverified asker — is the agent
acting for itself.

    X-Canopy-On-Behalf-Of: <the person's email>

still works as an assertion. It must name that same person; any mismatch, and any other
credential sending it (a browser session included), is refused with a 403 that says why.

On someone's behalf, these are judged as that person:

| Decision | Where | Their access needed |
|---|---|---|
| Pin a turn, or place a chat, on a runner | `access.may_pin_runner` (dispatch pins, chat placement, moving queued turns) | the target agent admin, or the runner's administrator |
| Ask for `turn_mode: auto` | `turn_gates.check_requested_turn_mode`; the claim re-checks `requested_turn_mode_by`, which is them | the target agent admin |

Reads are unchanged: a turn already reads as its asker where that matters (scoped caller
tokens, above). Each decision made on someone's behalf is recorded as an
`agent.on_behalf.decided` event. Every turn the agent starts from there, through any entry point
(a dispatch, a send into another session, a new session), carries
`origin_ref.on_behalf_of` (`{user_id, email, turn, agent, decided}`), stamped by
`services.enqueue_turn` alone; a client's own is dropped. Its initiator stays the agent:
the agent made the call, for that person. The receiving agent's envelope says so in
`who.on_behalf_of` (`{user, relationship, via_agent, turn}`, the relationship being the
person's to the receiving agent), which every turn an agent's login started carries:
`null` means the agent acted on its own.

## Acting as the person on canopy-web (act-as-you, in your own turn)

An agent working in a turn you started can ask to act **as you** on canopy-web itself,
for 30 minutes, and does so only once you approve with your passkey
(`apps/harness/act_as.py`, `harness.models.ActAsGrant`; rules pinned by
`tests/test_act_as_you_on_canopy.py`):

- **Asked by the turn, for the turn's own person.** The agent calls
  `POST /api/me/act-as-requests/` with the login canopy-web issued for its turn; canopy-web
  works out who you are from that login exactly as "Acting on behalf of the person who
  asked" does (`turn_asker.check`): an executing, unconfined turn for this agent, started
  by a verified, active human who is not an agent (or a chain on their behalf). Nothing it
  sends names the person, so it can never ask to act as anyone else. A stored token, a
  confined turn, a contact's, canopy's own or an unverified asker's turn asks no one (403
  saying why); so does a person with no passkey.
- **It belongs to the conversation, not the one turn.** In a chat the reply carrying the
  approve link reaches the person only when the asking turn ends, and each message after is
  a new turn with a new login. So a request waits 30 minutes, and both the request and the
  permission live while the turn runs **or**, when it was asked in a conversation (a chat
  session, or a laptop's session-scoped turn login), while that conversation is active and
  its newest turn is still the person's (`act_as.held`). Any turn login of the same agent
  in that conversation working for the same person reads the status, collects the token
  (still once) and stops it (`act_as.agent_may`); with a turn login,
  `GET /api/me/act-as-requests/` lists that agent's waiting and live requests there. A
  colleague's message, or the conversation closing, ends it. A push goes to the person's
  devices at once (how many it reached is kept: `push_sent`, and `pushed` in the event).
- **Approved with a fresh passkey touch, for that request, in your browser.** A push, a
  card on your supervisor feed and a row in Settings → Agents acting for you lead to
  `/act-as/<id>`: the agent, its reason verbatim, the conversation, what it asked for and
  the deadline — or, once it is not waiting, what happened and when.
  "Allow read-only" / "Allow read + write" each run a WebAuthn assertion begun for this
  request (`…/approve/begin` + `/finish`); a session cookie, a step-up window or any token
  is not enough, and approve/decline refuse every token.
- **Read or write, 30 minutes, dies with the turn.** `read` reaches GET routes and
  read-only tools only; `write` reaches every route canopy's MCP server serves as a tool
  except one list, `act_as.EXCLUDED_PREFIXES` / `EXCLUDED_PATTERNS` / `EXCLUDED_TOOLS`:
  tokens and PATs, the person's memory switches and grants, passkeys, vaults, keys and
  secrets, connected sites' signing keys, act-as and host-grant approvals, agent-login
  acceptance, workspace invites and the runner protocol. 30 minutes from the touch, never extended; a new approval needs a
  new request. Every request re-checks it: stopped, run out, or its turn and
  conversation over, and the token signs in as no one.
- **The token is returned once.** The agent's first status read after the approval
  carries it (`cact_…`); only its hash is kept. It signs in as you on REST
  (`tokens.middleware._authenticate_act_as_token`) and MCP (`mcp.act_as_scope`, and
  again inside the in-process request), never mints, never steps up, and is refused where
  only you may act (`kinds.speaks_for_a_turn`). A turn it starts is not a verified asker's,
  so it never chains into another permission.
- **Attributed "<agent> as <person>".** Every request is a row in the MCP call log with
  your email, the acting agent and the grant (`MCPAuditLog.acting_agent`,
  `act_as_grant`); its credential label names the agent in the settings audit and turn
  provenance; `act_as.requested|approved|declined|stopped|expired` events (agent, person,
  turn, scope — never the reason) are in the workspace's event log for its admins.
- **You see and stop it**: Settings → Agents acting for you, beside host grants, and the
  approval page; the agent may stop it too (`POST …/{id}/stop`).

## Acting as the person on a connected site (act-as-you permissions)

The **act-as-you permission** is the person's word for a **host grant**
(`tokens.HostGrant`): a time-limited permission, given in the moment, that lets an
agent call a connected site's tools as that person, within their own access on that
site, for the page they are on. It is the only way an agent acts as a person *on a
host* (the header above is canopy-web's own decisions):

- **Given by acting, never by loading, and only with the person's passkey.** The
  panel asks the site for one only when the person sends from it (or opens a
  conversation whose live card needs it); nothing can use it until they confirm it
  with their passkey in the panel ("Let <agent> act as you on this page for 30
  minutes?"), and the send waits for that. It then shows "<agent> can act as you on
  this page until <time> · Stop". 30 minutes from the passkey: the panel's token
  renewals carry the same end, never extend it.
- **Never a contact's.** Only a canopy user has a passkey, so a contact is never
  given one, and an agent that serves the site on pages is not offered to a contact
  there at all; when no agent is left, the site shows them no panel.
- **Read by one lookup, with no fallback.** `host_grants.lookup`: a turn (gateway) or
  a View in a conversation reads only the grant of the page that last acted in that
  conversation as the person who started the turn / is viewing — expired or stopped
  is a refusal ("…has ended — send again to grant it for another 30 minutes"), and so
  is one not confirmed with the passkey, never
  another page's grant or the agent's own credential.
- **A chat on a page reaches that site only as the person, under their act-as-you
  permission — owners included.** The gateway (`site_tools` / `site_call` /
  `site_read`) serves every turn in a conversation held on a site's page (the
  session's `embed_app`) whose agent is set up for that site — some capability of its
  interface lists it in `sites:` (`interface.page_site_of`) — whatever the turn's
  profile: a confined visitor's (its capability must list the site), an editor's
  (scoped token), the owner's or an agent admin's (found from the chat key the runner gave
  only that session). Every call uses the SENDER's grant for that page. A full-profile
  turn is narrowed by the widest ceiling the owner set on that site
  (`interface.site_ceiling`).
- **The agent's own login at that site is withheld from such a turn.** A capability
  names it per site: `own_login: {connect-labs: [LABS_MCP_TOKEN]}` — credential names,
  never values. The claim carries `page_site: {site, withheld}`
  (`interface.page_turn`); a cloud runner leaves those names out of the turn's
  environment and its per-turn env file and sets `CANOPY_PAGE_SITE`; a laptop runner
  cannot set an emdash session's environment, so it leaves a by-task marker
  (`~/.canopy/page/task/<task>.json`) for the agent's own MCP headers helper — the
  agent's `.env` is still on that disk, so there the boundary is the helper and the
  instructions. The caller envelope's `site` block tells the agent it is on a page,
  reaches the site only through the gateway as the person, and has no account of its
  own there in this conversation.
- **The person sees and stops theirs**: Settings → Agents acting for you
  (`/api/me/host-grants/`), and Stop in the panel (`/api/embed/host-grant`). Stop
  takes effect on the next call.

Mechanics: [`embedding-internals.md`](embedding-internals.md) § Act-as-you
permissions; the wire contract: `sdk/python/README.md` § Host grant contract v1.

## Who can see a session

A session is visible to the people who were **in the conversation where it
happened**, plus whoever they explicitly add — never to someone merely for being
in the same workspace. Multiplayer is always an explicit choice (a participant
row, added by the session's owner). Agents get no implicit access: an agent's own
login reads a session only if it was made a participant, it is running a live
turn in it (`access.own_turn_session`), or the person granted it review (a
coach). Canopy only operates on what a machine has already **sent**; it asks a
machine for more (live streaming, history) only when its owner, or someone they
explicitly added, asks (`access.may_pull`). Adding someone is the session owner's
(`access.can_share`), with one more way in: the owner of the agent a member's
conversation is with (`Agent.owner`, not its agent admins), once in it, may bring someone
new in — a person or an agent — but owns nothing, reads nothing new, and changes or
removes nobody; never a contact's or a sensitive one (`access.can_add_people`). Sensitive conversations stay sealed to
their own people (`tenancy.md` § Sensitive conversations). The rule is
`apps/canopy_sessions/access.py`; enforcement detail is `tenancy.md` § The
chat-session ACL.

| Where it happened | Who sees it (read + write unless noted) | Who may pull from the machine |
|---|---|---|
| **Laptop** (a runner-discovered session) | the box's owner (`Runner.owner`), + people they add, + a project's team if they shared that project's repo with it (`ProjectSessionShare`, opt-in per person × project, `/api/me/session-sharing/`); the box's runner admins read only (to move it off the box) | the box owner, + people they add as editor, + that project's team |
| **Web chat** | its creator + people they add | the same |
| **Email** | everyone on the thread who is a verified member (§ Who is in a session) | the agent's admins, + the thread's people |
| **Slack** | everyone in the Slack channel (a DM: its people), read + write, as in Slack | the agent's admins, + the channel's people |
| **Panel** (an embedded widget) | the visitor (a contact: `contact_session_q`) | the visitor |
| **An agent's own work** (an alarm, a scheduled turn) | the agent's admins (`Agent.is_admin`) | the agent's admins |
| **Work an agent starts for you** (a dispatched session) | you + the parent conversation's people (`services._inherit_participants`) | the same |
| **Coaching** | + the agent the person granted (`SessionReviewGrant`), read-only, never a sensitive one | no — it reads what is already uploaded |

**Every session says who can see it.** `GET /api/canopy-sessions/{id}` carries `audience`
(`{label, extras, count}`, `apps/canopy_sessions/audience.py`) — e.g. "Only you", "You and 2
people you added", "Everyone on this email thread (4)", "Members of #channel", "Hal's admins
(the agent's own work)", plus extras like "The Supply project team" or "Trellis may review
(coaching)". It is built from the same leg pieces as the ACL, and
`tests/test_session_audience.py` holds its readers equal to `access.can_read` for every
session shape. The chat page and the embedded panel show it as one line at the top.

The email and Slack rows describe the entry points those channels open (participant rows
written when a person is on the thread or in the channel); the session ACL itself
only honours the rows.

**Every change to who can see a session is an event.** A participant row added,
re-roled or removed, a Slack thread bound or detached, a move between workspaces, a
person's project sharing or a coach's grant, and a session becoming sensitive each
write one `session.*` event to the workspace's log, ids only, naming who did it and
under which turn — so a workspace admin can say what happened to a session without reading it
(`chat-sessions.md` § A session's lifecycle is in the event log).

## Who is in a session

A session is visible to the people who were in the conversation where it happened, plus
whoever they add. A `SessionParticipant` row (EDITOR) is the grant, made at the channel;
the ACL itself stays `apps/canopy_sessions/access.py`. Never a non-member, a contact
or an agent's login, and never into a sensitive conversation.

* **Email: everyone on the thread** (`harness.channels.email.admit_thread_people`) — the
  members among the From, To and Cc of each inbound message whose From is aligned (DMARC,
  or DKIM signed by the From: domain), joined as each message arrives; an unaligned
  message admits nobody, so a forged From cannot Cc someone into a thread's history. The
  agent's own replies admit their To/Cc (`mail.agent_mail._admit_recipients`).
* **Slack: everyone in the channel, full access — as in Slack** (`apps/slack/audience.py`) —
  every linked canopy member (`SlackUserLink`, or auto-linked by verified email) in the
  conversation the session lives in (channel members; a DM's or group DM's people) is an
  editor, synced from `conversations.members` (cached 5 min) on each inbound message and
  by a 10-minute sweep of sessions active in the last 14 days; one the channel brought in
  (`SlackAudienceMember`) leaves when they leave the channel, never on an answer Slack did
  not give. Contacts and guests see it in Slack only.

## Slack threads

Who may write into a Slack thread's conversation is an access decision, so two
Slack behaviours live here (`apps/slack/`, pinned in `tests/test_slack.py`).

* **A confined reply in someone else's thread gets a thread of its own**
  (`services._fork_confined`). When a message in an existing thread would run
  confined — asked of a probe turn through `access.decide_for_turn`, so it cannot
  disagree with the real stamp — and the thread's session is not the sender's,
  canopy posts a new top-level message, opens a session keyed to THAT thread
  (owned by the sender, `metadata.slack_forked_from` / `slack_forked_session`,
  seeded with the thread they were reading), and links it from the original
  thread. Sharing the owner's session would queue behind the owner's turn
  (`routing.blocking_turn`) and answer in their thread from a `cx-` session
  nobody can find. One fork per (thread, person); writing in the original thread
  again continues it.
* **`@canopy branch <ask>` splits a conversation into a new thread**
  (`apps/slack/branch.py`). Inside a thread with an agent session, anyone who may
  write into that conversation (its owner or an editor — a member in the
  channel is one — or an agent admin) gets a new top-level message, a
  child `Session` (`parent_session`, `parent_claude_session` from the parent
  binding's `transcript_id`, `metadata.slack_branched_from`) keyed to the new
  thread, and a first turn seeded with the parent conversation, condensed by
  `canopy_sessions.exports.build_branch_seed` (tool output dropped, capped at
  `BRANCH_SEED_CHARS` = 30k, keeping the first ask and the end). The original
  thread gets a link and carries on; branch as often as you like. The seed is a
  prompt, not a Claude fork — emdash creates tasks through its UI and cannot pass
  `--fork-session`. A viewer cannot branch (they could not write into it), and a
  confined reply never branches (it forks, above).

## What agents know about a person (the fleet brain)

`apps/contacts/people.py` + `people_api.py` (`/api/people/…`). One
`Person` per human — a member (`Person.user`, joined to their correspondent row by
a VERIFIED address only) or a contact (`services.person_for(user=…|contact=…)`).

* **What may be kept: work context only.** `PersonFact.kind` is closed —
  `role | project | instance | preference | correction | terminology` — by a DB
  CHECK constraint as well as the API (anything else is a 400). No health,
  personal life, performance judgements or sentiment. Facts are append-only:
  superseded (same person, same workspace) or retracted, never edited.
* **Per workspace (v1).** A fact is written in one workspace and served
  only there — the envelope carries the facts of the turn's agent's workspace
  and no other. Reading or writing needs membership of that workspace (an agent's
  login is a member), AND the person must be someone that workspace deals with
  (`people.known_in`: a member, a contact, an initiator of a turn there, or the
  subject of a fact there) — otherwise 404, so an id or an address confirms
  nothing across tenants.
* **Raw conversations stay private.** The people API's one read of turn content
  is `GET /api/people/{id}/conversations/?agent=<slug>`: the turns
  that person started with THAT agent, for that agent's OWN login only. An agent admin
  of the agent may call it but sees only turns the turn ACL already shows them.
  No other agent, and no plain member, can read them. A fact's `source_turn` is
  a link, readable only by whoever can already read the turn.
* **The person decides what agents get — per session, per agent.** Two switches,
  record (gates writes) and use (gates reads), each canopy-level `available` +
  `default` with a per-session override; they are policy, never a grant — an agent is
  served only under a grant the person gave THAT agent, and only for people the
  `HCP_PEOPLE` rollout gate names. Switches, grants, the envelope's `hcp` block and
  the rollout gate: [`hcp-service.md`](hcp-service.md) § "The person's record/use
  switches", § "canopy's own agents: grants per agent, only by the person's act" and
  § "Who has memory at all — the rollout gate".
* **The subject sees everything.** `GET /api/people/me/` (the page
  `/people/me/`): every live fact in every workspace, and the last
  50 reads. Every read — the envelope's and the API's — is a `PersonAccess` row.
* **Who may retract**: the person, whoever asserted it (the user, or the asserting
  agent's login), or a workspace admin (`members.manage`).
* **Who writes: the session that learned it.** The agent records a fact in the
  turn where the person said it (HCP `addPreference`, `turn` = that turn), prompted
  by the envelope's person block and canopy's turn checklist. There is no
  follow-up digest turn and no stored per-person digest — don't add a second pass
  over a transcript; the session doing the work has the context.
* **Retention** (`apps/retention`) scrubs turn content, not facts; a
  fact whose source turn was scrubbed keeps its statement.

### Project participants, coverage, and contact notes

* **Project participants.** `AgentProject.participants` (through
  `ProjectParticipant`: `role` free text ≤ 80, `source` fact | turn | manual | summary |
  review | backing | reply | checkin,
  `created_at`; one row per project and person, the FIRST source wins). Filled
  automatically (`apps/agents/participants.py`): any fact filed against a project
  makes its subject a participant (`people.record_fact`), and a task in a project
  whose `raised_by` turn a HUMAN started makes that human one (task create, patch,
  `set_task_project`). Served only inside the agent's workspace: on the project
  detail (`participants`: id, display_name, email, role, source, since — same gate
  as the project), at `GET /api/people/{id}/projects/?workspace=` (same gates as
  `GET /api/people/{id}/`, logged), and in the envelope `person.projects` (id,
  ext_id, name, agent — at most 5, most recently active first, archived left out;
  additive, envelope stays v3). Backfill: migration `agents/0041` (facts) and
  `manage.py backfill_project_participants` (facts and task raisers).
* **Coverage — a dead brain must be loud.** `GET /api/people/coverage/?workspace=
  &days=7` and `manage.py people_coverage --workspace <slug> [--days N] [--json]`
  (exit 1 when unhealthy), both from `apps/contacts/coverage.py`. Per agent:
  human-started turns, how many were handed a NON-EMPTY person block (≥ 1 live
  fact), facts the agent recorded IN-SESSION (asserted by it, sourced from one of
  those human turns), and how many people. "Non-empty"
  is recorded when the envelope is BUILT, as `PersonAccess.had_context` on the
  envelope read — not re-derived later from facts that have since changed.
  **Rule:** `healthy` = with ≥ 10 human turns, ≥ 1 fact recorded in-session; the
  workspace is healthy when every agent is. Counts only, no person named. Members of the
  workspace; an agent admin who is not a member (an inherited owner) sees only the
  agents they administer; anyone else 404.
* **`Contact.notes` is mirrored into a fact.** Each non-empty `notes` is ONE live
  `role` fact (declared, asserted by nobody, statement = the first 500 characters
  flattened to one line), marked `PersonFact.source_contact`. `PATCH
  /api/contacts/{id}/` with `notes` supersedes the previous mirror (clearing the
  notes retracts it), so the brain is the one place agents read. The `notes`
  field is kept. Backfill: migration `contacts/0011` (historical models,
  idempotent) and `manage.py mirror_contact_notes`.

### The HCP service

The fleet brain is each person's Human Context Protocol instance
(`apps/contacts/hcp/`, `hcp_api.py`, discovery `/.well-known/hcp-configuration`),
and apps canopy does not operate may ask a person for parts of it through
canopy's own OAuth 2.0 authorization server (`hcp_oauth.py`, issuer
`<origin>/api/hcp` — separate from the `/oauth/*` MCP sign-in server). `hcpat_`
tokens open `/api/hcp/v1/` and nothing else, and the person's own record/use
settings bound every client whatever it was granted. It is internal-only behind
one gate, `HCP_SERVICE_AUDIENCE` (default `internal`). Clients, consent, tokens,
revocation and every judgment call: `docs/architecture/hcp-service.md`; the
self-audit against the spec: `docs/architecture/hcp-conformance.md`.
