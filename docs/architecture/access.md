# Access — the glossary and the one rule

Every word canopy uses about who may do what has exactly one meaning, and it is
defined here. Code docstrings point at this page instead of re-defining terms
(`apps/agents/turn_context.py`, `session_context.py`, `refusals.py`, `grants.py`, `roles.py`,
`published_profiles.py`, `profile_settings.py`, `apps/harness/caller_context.py`, `apps/harness/turn_mode.py`,
`apps/workspaces/permissions.py`). Reuse existing words, one meaning per word, no new terms.

There are two ladders, and they are different questions:

* a **workspace role** says what a person may do in a *workspace* (read logs,
  invite people, create content, reshape agents);
* an **agent role** says what a person may make one *agent* do.

THE rule that joins them is one function, `apps/agents/turn_context.resolve_turn_context`. It reads
the facts of a conversation and who is asking, and returns a **Turn Context**: what one turn may do
and why. Every entry point that makes an agent do work asks it; everything downstream reads its
result; `tests/test_access_matrix.py` asks it through every entry point. Don't write a second
predicate for "may this person make this agent do X" — ask the rule.

This page is the model, the glossary, the roles, getting into a workspace, whose authority an agent acts with, triggers, runner roles and turn mode. The rest is split by subject:

## The rest of access, by subject

- [access-turn-context.md](access-turn-context.md) — What one turn may do and why: the Turn Context and what it holds, profiles, how it is resolved, the two moments (enqueue and claim), the fan-out, what confinement keeps, the decision table and every refusal.
- [access-agent-roles-and-access-list.md](access-agent-roles-and-access-list.md) — What a person may make one agent do: agent roles and configurable roles, the access list and workspace defaults, read mode and host sites.
- [access-direct-use-and-admission.md](access-direct-use-and-admission.md) — direct use of an agent, what a caller's turn tells the owner, admission rules, the caller envelope.
- [access-confinement-and-act-as.md](access-confinement-and-act-as.md) — how confinement is enforced, acting on behalf of the asker, act-as-you on canopy-web and on host sites.
- [access-sessions-and-people.md](access-sessions-and-people.md) — who can see and who is in a session, Slack threads, what agents know about a person.
- [hcp-service.md](hcp-service.md) — the HCP service.

## The words

| Word | Means | Where it lives |
|---|---|---|
| **Session Context** | The durable facts of a conversation, whoever is asking. | `session_context.py` |
| **asker** | Who a turn is for, and whether this message proves it (`verified`). | `turn_context.Asker` |
| **standing** | What the asker holds on this agent, and why: relationship, permissions, role, source. | `TurnContext.standing` |
| **Turn Context** | What one turn may do, resolved fresh for each message; always produced, even when refused. | `turn_context.py` |
| **profile** | The complete definition of what a turn can do; one kind from a closed set. In the UI, "what it may do". | `turn_context.Profile` |
| **access** | `full`, `confined` or `refused`: how a profile is enforced. A roster row and the topology say `none` for refused, on the wire. | `TurnContext.access` |
| **workspace role** | What a person may do in a workspace: viewer < editor < admin < owner < superuser. Each role holds a set of workspace *permissions* (`content.write`, `logs.read`, …), not to be confused with the agent permissions below. Built on the same role engine as agent roles (`apps/common/rolesets.py`), with its own vocabulary. | `apps/workspaces/permissions.py`, `WorkspaceMembership.ROLE_RANK` |
| **permission** | What a person may do with ONE agent: `read` (be answered in read mode), `write` (drive the whole agent), `agent_edit` (change the agent itself), `admin` (configure it), `own`. `agent_edit` implies `write` implies `read`; `admin` implies all but `own`. A line may also grant the scoped `use:<site>`, which opens that one host site. | `apps/agents/roles.py` |
| **agent role** | A named bundle of permissions, **defined by a workspace** (`AgentRole`) and usable below it. Three come built in as the defaults: **editor** (read + write + agent_edit), **read_write** (read + write), **read_only** (read); they are editable, cloneable and never deletable. A workspace adds its own ("read + use the connect site"). **owner** and **admin** are fixed in code: they are `Agent.owner` and `Agent.is_admin`, never a line. Never "member", "contact", "viewer" or "user". | `apps/agents/roles.py` (vocabulary, the built-ins' seed, the fixed roles), `rolecatalog.py` (the catalog), `roleadmin.py` (editing it) |
| **system** | Not a person and not in the table: canopy itself (a schedule, a drill), the agent's OWN canopy login, an approved task's dispatch. It may do what an agent admin may. | `roles.SYSTEM` |
| **access list** | The one list per agent that says who has what: lines of `(who, role)` or, rarely, `(who, one host site)`. | `AgentGrant`, `apps/agents/grants.py` |
| **line** | One entry of an access list. `who` is a person, everyone in a workspace (optionally also every workspace below it), anyone at a domain, or a known visitor or sender. | "The access list" below |
| **workspace default** | A line set once on a workspace; every agent in it, and in the workspaces below it, inherits it unless the agent opts out. | `AgentGrant.workspace`, `Agent.inherit_workspace_defaults` |
| **read mode** | The default confined profile for someone who holds `read` and not `write`. It **exists on every agent from the start**: canopy's default (`published_profiles.default_read_profile`), or the agent's own, which replaces the default whole. | "Read mode and host sites" |
| **host site** | A confined profile tied to one registered site (ACE's `connect`): which tools and paths a conversation held on that site may use. Rare. It never says who may use it: that is the access list's job. | "Read mode and host sites" |
| **site tool limit** | Optional, per host site: tool-name globs that narrow which of the host's tools an agent may call there. It never widens: the host's grant (its scopes for that page and the person's own access) decides what exists, and an empty limit adds nothing. Settings form: "Site tools it may use". Stored and sent as `ceiling`, a name that stays (below); the old word suggested a maximum grant, which it is not. A limit that matches every tool the site offers is refused as a save, since it limits nothing. | `profile_settings.set_site`, `host_gateway.tool_allowed` |
| **entry point** | A way work reaches an agent: the harness API, a canopy-web chat, Slack, email, a site's panel. Every one asks the rule. | "Where it is asked" |
| **role** vs **source** | A role is what someone can do; a source is why they have it (`owner`, `admin`, `system`, `session`, `list:<line>`). Never the same word. | `Standing.agent_role`, `Standing.source` |
| **contact** | Not a role. How canopy knows someone who is not a workspace member: an emailer, a Slack person outside the workspace, a widget visitor (who may have no address at all). A contact has an agent role only if a line names them; with no line they are refused. | `apps/contacts` |
| **verified** | THIS message proves the sender's address: a signed-in request, mail that is DMARC-aligned or DKIM-signed by its own From: domain, a full member of our own Slack. An embedded site's visitor is never verified (the host vouches for them, not the human). | `caller_context.is_verified` |
| **refusal** | Why a turn is not run, how to fix it and who can: one code from a closed set. | `apps/agents/refusals.py` |
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
`credential_resolve`; the caller envelope's `authority` block tells the session; what one
turn really gets is `TurnContext.credentials`):

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
  the rule lets run as the whole agent (never a caller's confined turn); a box gets it
  only in its own resolve's `delegated` field, proved by its device key. canopy gives it
  no expiry: the provider's own is shown as information. → `vault.md` § Whose account
  an entry is
* **A trigger's turn** — a schedule or alarm mail the agent's own trigger fired runs
  with no more authority than the person who set it up has on the agent, and stops
  firing, loudly, when they lose it (see "Triggers").
* **An act-as-you permission** — time-limited, granted by the person asking, in the
  moment, with their passkey. It has two forms: **a host site's page** (a host
  grant: the agent acts as you at that site, "Acting as the person on a host
  site" in [access-confinement-and-act-as.md](access-confinement-and-act-as.md)) and **canopy-web itself, in your own turn** (an agent you started asks,
  you approve read-only or read + write, and it acts as you on canopy for 30 minutes —
  "Acting as the person on canopy-web" in [access-confinement-and-act-as.md](access-confinement-and-act-as.md)). Nothing else lets an agent act as a
  person. In particular:
  - **An agent's caller token only reads.** A whole-agent turn asked by someone
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
    ([access-confinement-and-act-as.md](access-confinement-and-act-as.md)). A confined turn's caller
    token keeps its profile's own tool list. A **repo chat** (no agent: a person's own
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
| **viewer** | `read` | Reads the workspace. Talks to an agent only as far as that agent's access list lets them. |
| **editor** | + `content.write`, `agent.work`, `session.drive`, `events.write` | Makes things; **reshapes agents and sends them work** (the routes behind an agent's settings, schedules, tasks and dispatch). Whether a turn of theirs runs the whole agent is the agent's access list, not this role. |
| **admin** | + `logs.read`, `members.manage`, `integrations`, `runners.route`, `retention.manage`, `admission.manage`, `projects.decide` | **Runs the workspace**: every log, members below admin, the integrations, the default runner order, how long chat and turn content is kept, which turns are refused, and starting or declining a proposed project (beside that project's own owner). **Holds no agent keys** — a workspace admin is not an agent admin. |
| **owner** | + `own`, `control` | **The keys**: the shared vault, the Slack app, host sites, deleting the workspace — and is an **agent admin of every agent** in it. Holds or stops all work in the workspace's tree. **Is told**: blocking alerts, access requests, owner notices. |
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

There is no other way in. A host site's visitor is a **contact**, never a member
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

## Triggers: what starts an agent on its own

A **trigger** is an agent's own: something that starts a turn on that agent, with the
authority of the person who set it up — never more. It is not a member and holds no
standing of its own. Kinds (`AgentSchedule.kind`, the model keeps its first kind's
name; `apps/harness/triggers.py`):

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
  so `turn_context.resolve_turn_context` gives it the whole agent and the turn mode is bounded by the
  creator (`turn_mode._accountable_cap`: manual unless they are an agent admin). A
  trigger's own `turn_mode` is a rung above the routing rules
  (`turn_mode.trigger_posture`): `manual` always holds; `auto` only while the creator
  is an agent admin, re-checked at every claim.
* **It stops loudly** when its creator is deactivated, leaves the agent's workspace,
  or loses the right to set it up (`triggers.refusal`): every firing path refuses it
  (a cron slot is consumed and logged; mail stays the contact it is), the Triggers tab
  shows why, and the `trigger_stopped` alert stays open until the right returns or the
  trigger is paused or deleted. A cron row with no creator fires as canopy's own.
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
* **A refusal points here:** a verified contact refused by the access list
  (`not_on_access_list`) is told that, if it is automated, an agent admin can bind it to an
  inbound-mail trigger on the agent's Triggers tab, so an automated sender is never cancelled
  at enqueue without anyone being told.

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

Decided at claim, top rung first (`apps/harness/turn_mode.py`), and carried on the Turn Context's
`review`: a dispatch's requested mode (`auto` only from an agent admin) → the `manual_only` cap of an
editor line (`manual`) → an actor routing rule (an `auto` naming a person needs a verified message) → a
source routing rule → the agent's own switch (`Agent.turn_mode`). Setting `auto`
on any rung is for the agent's owner and agent admins.

`auto` is a posture for outbound actions, not a claim that nobody is looking. A turn the
person typed into the canopy-web chat while present in it is **attended**
(`act_as.attended_by`); for act-as-you that is the one place `auto` does not mean unreviewed
([access-confinement-and-act-as.md](access-confinement-and-act-as.md)). The mode itself is unchanged.
