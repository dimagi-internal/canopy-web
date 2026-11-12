# Access — the glossary and the one rule

Every word canopy uses about who may do what has one meaning, defined here; code docstrings point at this
page instead of re-defining terms. Reuse existing words, one meaning per word, no new terms.

There are two ladders, and they are different questions:

* a **workspace role** says what a person may do in a *workspace* (read logs, invite people, create content,
  reshape agents);
* an **agent role** says what a person may make one *agent* do.

THE rule that joins them is one function, `apps/agents/turn_context.py::resolve_turn_context`. It reads the facts of
a conversation and who is asking, and returns a **Turn Context**: what one turn may do and why. Every entry point
that makes an agent do work asks it; everything downstream reads its result; `tests/test_access_matrix.py` asks it
through every entry point. Don't write a second predicate for "may this person make this agent do X": ask the rule.

This page is the glossary, whose authority an agent acts with, workspace roles, getting into a workspace, triggers,
runner roles and turn mode. The rest is split by subject:

- [access-turn-context.md](access-turn-context.md) — what one turn may do and why: the Turn Context, profiles, the two moments (enqueue and claim), what confinement keeps, the decision table, refusals.
- [access-agent-roles-and-access-list.md](access-agent-roles-and-access-list.md) — what a person may make one agent do: agent roles, the access list and workspace defaults, read mode and host sites.
- [access-direct-use-and-admission.md](access-direct-use-and-admission.md) — direct use of an agent, what a caller's turn tells the owner, admission rules, the caller envelope.
- [access-confinement-and-act-as.md](access-confinement-and-act-as.md) — how confinement is enforced, acting on behalf of the asker, act-as-you on canopy-web and on host sites.
- [access-sessions-and-people.md](access-sessions-and-people.md) — who can see and who is in a session, Slack threads, what agents know about a person.
- [hcp-service.md](hcp-service.md) — the HCP service.

## The words

| Word | Means | Where it lives |
|---|---|---|
| **Session Context** | The durable facts of a conversation, whoever is asking. | `apps/agents/session_context.py::SessionContext` |
| **asker** | Who a turn is for, and whether this message proves it (`verified`). | `turn_context.Asker` |
| **standing** | What the asker holds on this agent, and why: relationship, permissions, role, source. | `turn_context.Standing` |
| **Turn Context** | What one turn may do, resolved fresh for each message; always produced, even when refused. | `turn_context.TurnContext` |
| **profile** | The complete definition of what a turn can do; one kind from a closed set. In the UI, "what it may do". | `turn_context.Profile` |
| **access** | `full`, `confined` or `refused`: how a profile is enforced. A roster row and the topology say `none` for refused, on the wire. | `TurnContext.access` |
| **workspace role** | What a person may do in a workspace; each role holds a set of workspace *permissions* (`content.write`, `logs.read`, …), not to be confused with the agent permissions below. Built on the same role engine as agent roles. | `apps/workspaces/permissions.py`, `WorkspaceMembership.ROLE_RANK`, `apps/common/rolesets.py` |
| **permission** | What a person may do with ONE agent: `read` (be answered in read mode), `write` (drive the whole agent), `agent_edit` (change the agent itself), `admin` (configure it), `own`; a line may also grant the scoped `use:<site>`. | `apps/agents/roles.py` (`_IMPLIES` is the dependency rule) |
| **agent role** | A named bundle of permissions, **defined by a workspace** (`AgentRole`) and usable below it. **owner** and **admin** are fixed in code (`Agent.owner`, `Agent.is_admin`), never a line. Never "member", "contact", "viewer" or "user". | `roles.SEED` (the built-ins), `roles.RESERVED_KEYS`, `rolecatalog.py`, `roleadmin.py` |
| **system** | Not a person and not in the table: canopy itself (a schedule, a drill), the agent's OWN canopy login, an approved task's dispatch. It may do what an agent admin may. | `roles.SYSTEM` |
| **access list** | The one list per agent that says who has what: lines of `(who, role)` or, rarely, `(who, one host site)`. | `AgentGrant`, `apps/agents/grants.py` |
| **line** | One entry of an access list. `who` is a person, everyone in a workspace (optionally also every workspace below it), anyone at a domain, or a known visitor or sender. | `apps/agents/grants.py` |
| **workspace default** | A line set once on a workspace; every agent in it, and in the workspaces below it, inherits it unless the agent opts out. | `AgentGrant.workspace`, `Agent.inherit_workspace_defaults` |
| **read mode** | The default confined profile for someone who holds `read` and not `write`. It exists on every agent: canopy's default, or the agent's own, which replaces the default whole. | `published_profiles.default_read_profile`, `access-agent-roles-and-access-list.md` |
| **host site** | A confined profile tied to one registered site: which tools and paths a conversation held on that site may use. It never says who may use it: that is the access list's job. | `access-agent-roles-and-access-list.md` |
| **site tool limit** | Optional, per host site: tool-name globs that narrow which of the host's tools an agent may call there. It never widens: the host's grant decides what exists, and an empty limit adds nothing. Stored and sent as `ceiling` (the name stays). A save that sets a limit matching every tool is refused, since it limits nothing. | `profile_settings.set_site` (`_limits_nothing`), `apps/host_grants/host_gateway.py::tool_allowed` |
| **entry point** | A way work reaches an agent: the harness API, a canopy-web chat, Slack, email, a site's panel. Every one asks the rule. | `access-turn-context.md` § The rule (the decision table) |
| **role** vs **source** | A role is what someone can do; a source is why they have it (`owner`, `admin`, `system`, `session`, `list:<line>`). Never the same word. | `Standing.agent_role`, `Standing.source` |
| **guest** | A workspace membership for ONE thing the workspace runs, and outside it for everything else: role `guest` plus a `scope` (`{"apps": {key: app role}}`). Not a workspace role: it holds no permission and no line can name it. | `WorkspaceMembership.GUEST`, `apps/workspaces/guests.py`, § Guests |
| **contact** | Not a role. How canopy knows someone who is not a workspace member: an emailer, a Slack person outside the workspace, a widget visitor (who may have no address at all). A contact has an agent role only if a line names them; with no line they are refused. | `apps/contacts` |
| **verified** | THIS message proves the sender's address: a signed-in request, mail that is DMARC-aligned or DKIM-signed by its own From: domain, a full member of our own Slack. An embedded site's visitor is never verified (the host vouches for them, not the human). | `caller_context.is_verified` |
| **refusal** | Why a turn is not run, how to fix it and who can: one code from a closed set. | `apps/agents/refusals.py::Code` |
| **agent admin** | Someone who holds an agent's keys: its owner, a workspace owner (implicitly), or a person granted by name (`AgentAdmin`), each still a member of the workspace. On the ladder, *admin* is this role and *owner* is the agent's owner above it. | `apps/agents/models/agent.py::Agent.is_admin` |
| **workspace admin** | A person with the workspace role admin: runs the workspace, holds no agent keys. | "Workspace roles" |
| **runner admin** | A runner's owner or a `RunnerAdmin` grant. | `apps/harness/runners.py::can_administer_runner` |
| **vault key holder** | A person who holds a copy of a vault's key. | `vault.md` |

The words say which admin they mean, always: a message that says "admin" alone has not said who may.

## Whose authority an agent acts with

**An agent acts as itself — with its own credentials or ones a person delegated to it — or as you only under an
act-as-you permission you granted.** Two things decide what a session carries: **where it runs** (a runner box, or
*direct*: the person's own Claude Code) and **whether it is reviewed** (`manual` or `auto`). Three kinds of
credential follow, in ONE table (`apps/agents/credentials/authority.py::_TABLE`, asked by `credential_resolve`; what
one turn really gets is `TurnContext.credentials`). Code asks `stance`; it never branches on a principal's name.

* **Its own credential** — the agent's own login (`Agent.user`) and the secrets in its own vault. A runner turn signs
  in with a login issued for that turn (`TurnAgentToken`); expiry and rotation are `apps/agents/services/agents.py::agent_token_ttl_days`
  and `apps/agents/credentials/pat_rotation.py`. An agent token with rotation off is watched (`apps/agents/alerts_agent_token.py`).
* **A delegated credential** — a standing credential from a person's own account lent to the agent (their GitHub
  token). A vault entry with `account: "delegated"` (`models/vault.py::VaultEntryAccount`), sealed only to the
  machines that run the agent and are the lender's own or cloud boxes, never under the vault key: no workspace
  member, agent admin or license holder reads it, nor canopy-web. A box gets it only in its own resolve's
  `delegated` field, proved by its device key; it is the agent's to use in any turn that runs as the whole agent,
  never a caller's confined turn. In a direct session it is the person's own, so the lent copy never travels. The
  sealing rules are `apps/agents/credentials/accounts.py` (module docstring, `delegated_devices`, `may_hold_for`).
* **A trigger's turn** — runs with no more authority than the person who set it up has on the agent, and stops
  firing, loudly, when they lose it ("Triggers").
* **An act-as-you permission** — time-limited, granted by the person asking, in the moment, with their passkey, in
  two forms: **a host site's page** and **canopy-web itself, in your own turn** ([access-confinement-and-act-as.md](access-confinement-and-act-as.md)).
  An `auto` turn may ask to act as the person to read only (`act_as.unreviewed_write_refusal`). Nothing else lets an
  agent act as a person. In particular:
  - **An agent's caller token only reads.** A whole-agent turn asked by someone who is not the agent's owner, an
    agent admin or canopy (an editor line, a writer in someone else's chat) reaches canopy as its asker so that it
    sees what they may see, but its tools are the READ tools plus driving the asker's own open page. A write is
    refused on MCP and on REST saying the asker has granted no act-as-you permission. A confined turn's token keeps
    its profile's own tool list; a **repo chat** (no agent) is that person's own tool and keeps every tool, as them.
    → `apps/harness/caller_tokens.py::Grant` (`read_only`), `PAGE_DRIVE_TOOLS`, `apps/mcp/turn_scope.py::READ_ONLY_REFUSAL`,
    `apps/tokens/middleware.py::_authenticate_caller_token`, minted in `apps/harness/claim_payload.py`.
  - **An agent's canopy login is always the account at its own email** (`Agent.email`), never any other. Linking an
    account that someone has signed in to interactively waits for that person to accept behind a passkey step-up
    (a token is refused), because an agent admin must not be able to act as a person. → `apps/agents/agent_login.py::link_by_email`,
    `services.agents.link_canopy_user`, `models/vault.py::AgentLoginRequest`.
  - **A runner never hands a turn its owner's token.** The cloud runner strips the box's own token from every turn,
    agentless repo turns included; such a turn reaches canopy as its asker through its caller token. →
    `runner/ec2/cloud_runner.py::_agent_env`.

## Workspace roles

The ladder is `WorkspaceMembership.ROLE_RANK`; what each role holds is one table keyed by permission name in
`apps/workspaces/permissions.py`, and nothing outside `apps/workspaces/` names a role. Detail: `roles.md`. Invariants
the table does not make obvious:

* An editor reshapes agents and sends them work (`agent.work`), but whether a turn of theirs runs the whole agent is
  the agent's access list, not this role.
* A workspace **admin** runs the workspace (logs, members below admin, integrations) and **holds no agent keys**: not
  an agent admin (`Agent.is_admin` answers yes for the agent's owner, a workspace owner or an `AgentAdmin`, and no
  while acting under a role cap).
* An **owner** holds the keys and is an agent admin of every agent in the workspace.
* A **superuser** has an owner's keys here and below **without the owner's email**: `services.owner_user_ids`
  and `mail_recipients` leave superusers out unless nobody else would be told. It is the workspace-level sibling of
  the system superuser (`User.is_superuser`), which holds the same switches for the whole fleet and no workspace access.

## Guests

A **guest** (`WorkspaceMembership.GUEST`, below viewer in `ROLE_RANK`) is in a workspace for what its `scope` names
and nothing else: today one or more apps, each at one app role (`{"apps": {"household": "assistant"}}`). The
relationship is explicit: listed on the members page ("Guest: Household (assistant)", `guests.describe`), invited and
removed like any member. Invariants:

* **Fail-safe by default.** `services.membership`, `member_role` and `is_member` answer None / False for a guest
  unless the caller passes `include_guests=True`; `member_roles(_of)`, `user_workspace_slugs`, `effective_memberships`
  and every listing built on them leave guests out. So every gate, `perms.can`, access-list line ("everyone in the
  workspace", `grants.matches` via `permissions.role_meets(role, "")`), mail route, MCP caller check and workspace read
  treats a guest as outside, unchanged. `permissions.ROLES` leaves guest off the ladder (no permission, `is_role`
  false), and `MINIMUM_ROLE` never names it. A guest may sign in but not create a workspace
  (`login_gate.can_create_workspace`).
* **Only code written for guests opts in:** `services.guest_scope` / `guest_app_role` / `guest_scopes`,
  `workspace_apps.access.roles_for` (their scoped app role and `public`, never `member`, a capability role or a grant),
  the app routes that use an app (`workspace_apps/api.py::_installation`, `guest_ok`, gate label `app-guest`), the
  members list and `GET /api/workspaces/guest-of` (the shell shows a guest only their apps).
* **Making one.** Only a guest invite does: `create_invite(role="guest", scope=…)` by someone who may manage members
  AND maintain each named app; the scope is vouched for by the app that owns it (`guests.register`, from
  `workspace_apps`' `AppConfig.ready()`; no validator, no guest). Accepting grants exactly it (`services._grant`
  merges a further guest invite's apps; a full member stays a full member). A role change never makes a guest;
  promoting one drops the scope.
* `tests/test_guest_members_never_leak.py` walks every GET as a guest and checks the non-route decisions.

## Getting into a workspace

Nothing is automatic: there is no self-join and no domain auto-join. People at a domain in a workspace's
`access_request_domains` may only **request an invitation**. A person becomes a member in exactly four ways, and an
agent's own login in a fifth. The only code that may create a `WorkspaceMembership` is `services._grant`,
`services.ensure_member` (its default role is editor) and workspace creation; `apps/workspaces/tests/test_access_requests.py`
fails on any other path and `tests/test_no_implicit_enrolment.py` pins `ensure_member`'s callers.

* **An invite.** A workspace admin or owner invites an email address at a role they may grant
  (`permissions.may_manage_member`: strictly below their own unless owner). Accepting grants that role, never lowering one already held.
* **An approved access request.** A person whose login email's domain is in `access_request_domains` may ask; a
  workspace admin or owner (a parent workspace's owners included) approves or denies. Approval is upgrade-only, so it
  never demotes a member. Notification is best-effort and never fails the request (`apps/workspaces/access_requests.py`).
* **A tree add.** A workspace admin or owner adds someone who is ALREADY a member of another workspace in the same
  tree, one the adder is in too, with no invite (`services.add_tree_member`). Anyone outside the tree is the same 404
  as a missing user; an existing member is 409.
* **Creating a workspace** makes you its owner, subject to `login_gate.can_create_workspace`: an invite-admitted user
  with no membership may not, or invite-admission would become transitively delegable (create a workspace, mint
  invites, and each invitee clears the login gate too).
* **An agent's own login** (not a person): a brand-new, password-less editor from the agent's mailbox address, linked
  to that one agent (`apps/agents/agent_login.py::create_agent_login`). An agent admin may when the address is at an
  agent-only domain (`is_agent_address`), so the account can never be a person's way into the workspace; any other
  address takes someone who can invite (`members.manage`). An address that is already a canopy user is refused, never
  adopted. Moving the agent brings its login into the destination on the same bar (`follow_agent`).

There is no other way in. A host site's visitor is a **contact**, never a member, and `access_request_domains` means
"may ask", never "may join". A request cannot probe tenants: a workspace whose domains do not match yours and one
that does not exist return the **same 404**. The domains are server-set, never client input.

**Auto-approve** (`Workspace.auto_approve_role`, owner-set): blank is off; set to viewer or editor (never above:
`apps/workspaces/access_requests.py::AUTO_APPROVE_ROLES`), a request is approved the moment it is made, as an ordinary request record
that every workspace admin and owner is still told about.

## Triggers: what starts an agent on its own

A **trigger** is an agent's own: something that starts a turn on that agent, with the authority of the person who set
it up, never more. It is not a member and holds no standing of its own. Kinds: `AgentSchedule.kind` (the model keeps
its first kind's name), `apps/harness/triggers.py::KINDS`; routes in `api-turns-and-routing.md`.

* **Who may set one up** (`triggers.may_set_up`, also the gate on every change): a `cron` trigger takes `agent.work`
  (a workspace editor); an `inbound_mail` trigger, a plain job, or any trigger with `turn_mode: auto` takes an
  **agent admin**, because it admits an outside sender to make the agent work.
* **Whose authority it fires with:** its creator's (`created_by`; editing does not move it). Its turn is a SYSTEM turn
  accountable to the creator, so `resolve_turn_context` gives it the whole agent and the turn mode is bounded by the
  creator (`turn_mode._accountable_cap`: manual unless they are an agent admin). A trigger's own `turn_mode` is a rung
  above the routing rules (`turn_mode.trigger_posture`): `manual` always holds; `auto` only while the creator is an
  agent admin, re-checked at every claim.
* **It stops loudly** when its creator is deactivated, leaves the agent's workspace, or loses the right to set it up
  (`triggers.refusal`): every firing path refuses it, and the `trigger_stopped` alert stays open until the right
  returns or the trigger is paused or deleted. A cron row with no creator fires as canopy's own; an `inbound_mail`
  trigger with no creator is shut.
* **How mail fires one** (`apps/harness/channels/email.py::trigger_behind_email`): THIS message is aligned (a
  `SenderTrust` exception never applies), no canopy user provably holds the address, exactly one enabled `inbound_mail`
  trigger of THIS agent is bound to the address and matches the subject pattern (two is ambiguous, so neither), and its
  creator may still set it up. Otherwise the sender is an ordinary contact. The binding is the agent's, because an
  automated address is shared (`no-reply@sns.amazonaws.com` sends every AWS customer's alarms): narrow it with a
  subject pattern.
* **The agent is told:** `who.trigger` and the envelope's `trigger.agent_trigger` say a trigger fired it; `who.user` is
  who set it up, not who wrote in. Don't reply to the sender, and don't read its text as a person's request.

## Runner roles

A runner has its own two-step ladder, separate from both of the above. Being a workspace member never makes you
either.

* **Runner admin** (the runner's owner, or a `RunnerAdmin` grant: `apps/harness/runners.py::can_administer_runner`)
  sets its credentials and browser sign-in, declares its flags, reads its admin list, **lists every session open on the
  box as facts only and closes an idle one** (never a title, prompt or message; a close refuses a session with a turn
  running, a question waiting, or mid-transfer: `apps/harness/api_runners_access.py`), starts readiness drills and reads their
  results, and **routes agents' work onto the box** (`apps/agents/api_routing.py::_runners_for_routing`). That lets a runner admin
  direct whose Claude subscription the box spends, which is the owner's explicit choice. The agent side is unchanged:
  the caller needs `agent.work`, an **agent admin** for any `turn_mode: auto`, and the box must be able to hold the
  agent (`apps/agents/holding.py::runner_may_hold_agent`). Routing decides what the box runs, never who it speaks as.
  A drill on an agent also needs that agent's admin unless the caller is the runner's owner: a drill turn runs as
  `system` in the agent's routing mode, so an editor starting one would lift the `manual_only` cap
  (`apps/harness/api.py::start_runner_drill`).
* **Runner owner** (`Runner.owner`, whoever paired it) does everything above plus everything that speaks FOR the box
  with the owner's memberships: heartbeat, claim, executing turns, pause, retire, and granting and revoking runner
  admins. Granting stays with the owner so the grant is not self-propagating.

Anyone else, a workspace member of any role or a contact, gets the runner-admin routes' **404**, never a 403, so a
runner's existence does not leak (`apps/harness/api.py::runner_admin_or_404`). A workspace admin of the runner's tenant may also READ its
drill results (`logs.read`), but not start one.

## Turn mode

`manual` (the default): every outbound or irreversible action needs the agent's owner or an agent admin first. `auto`:
the agent acts and reports. Decided at claim, top rung first, in `apps/harness/turn_mode.py` (its module docstring is
the ladder), and carried on the Turn Context's `review`: a dispatch's requested mode (`auto` only from an agent
admin), the `manual_only` cap of an editor line, an actor routing rule (an `auto` naming a person needs a verified
message), a source routing rule, then the agent's own switch (`Agent.turn_mode`). Setting `auto` on any rung is for the
agent's owner and agent admins.

`auto` is a posture for outbound actions, not a claim that nobody is looking: a turn the person typed into the
canopy-web chat while present in it is **attended** (`act_as.attended_by`), and for act-as-you that is the one place
`auto` does not mean unreviewed ([access-confinement-and-act-as.md](access-confinement-and-act-as.md)).
