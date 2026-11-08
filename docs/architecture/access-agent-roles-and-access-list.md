# Access: agent roles, the access list, read mode and host sites

What a person may make one agent do: agent roles and configurable roles, the access list and workspace defaults, read mode and host sites. Part of the [access](access.md) topic; the index there lists every part.

## Agent roles

What a person may make ONE agent do (`apps/agents/roles.py`). A **permission** is one thing; a
**role** is a named bundle of them. The vocabulary is fixed (`read`, `write`, `agent_edit`, `admin`,
`own`, and `use:<site>`); the bundles are **configurable** (next section); these are the built-ins
and the two fixed roles, strongest first:

| Agent role | Holds | In plain words |
|---|---|---|
| **read_only** | `read` | Is answered in read mode, and nothing else. **Read mode exists on every agent**: the confined session can only read and draft a reply (and send it on the thread it was asked on); the access-list form pre-selects no role. |
| **read_write** | `read`, `write` | Drives the **whole agent**, in its own authority: every skill and every host site it has (`full` access). The ordinary way to give someone an agent. |
| **editor** | + `agent_edit` | Everything read_write may, and changes the agent itself: its skills, its persona and the code in its own repo (table below). |
| **admin** | + `admin` | Configures the agent in canopy-web: its access list, read mode and host sites, credentials, routing, runners, integrations. May ask for `auto` and pin any runner that can hold it. |
| **owner** | + `own` | Everything an agent admin may, plus transfer or delete the agent and name its admins. |

* **The dependency rule** (`apps/agents/roles.py`, enforced when a role is built, tested in
  `tests/test_agent_access_list.py`): `agent_edit` implies `write` implies `read`; `admin` implies
  everything except `own`; `write` also opens every host site. A role cannot hold a permission
  without the ones it depends on.
* **One engine.** The closure, `allows(role, permission)`, `holds(permissions, permission)` and rank
  are `apps/common/rolesets.py`, shared with the workspace's roles
  (`apps/workspaces/permissions.py`, a ladder derived from `MINIMUM_ROLE`). The mechanism is shared;
  the vocabularies (`read`, `write` here; `logs.read`, `members.manage` there) are not.
* `system` is not in the table (see "The words"). Another agent's login is not `system`: it is
  graded like any person, or agent A could steer all of agent B.
* **What the rule reads.** The rule unions the permissions of every matching line
  (`Resolution.permissions`) and asks one question: do they hold `write` (`roles.holds(…, WRITE)`)?
  Then they get the whole agent. Otherwise they get one confined profile, chosen by what their lines
  open (`roles.can_use`: `write` opens all, `read` opens read mode, `use:<name>` opens the host
  site `<name>`; a stored `use:` of read mode's own name is a read-mode line). What each change to an
  agent needs is the table below.
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
  | Read mode and host sites: `PUT`/`DELETE …/read-mode`, `PUT`/`DELETE …/host-sites/{site}`, including `sensitive` | **admin** | decides who may reach what, and how sensitive data is handled |
  | Machine capabilities (`PUT …/capabilities`, refresh, slot data) | **admin** | leases money-spending machines (`capabilities.md`) |
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
  (`Standing.source`, the Turn Context).
* **Safety rails are stated by permission**, not by role name: a `domain` or `contact` line can
  never grant a role that contains `write`; a workspace default whose role contains `write` is the
  workspace owner's call. See "The access list".

### Configurable roles

**The roles a line can give are rows, defined by the people who run a workspace**
(`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`; the Roles page, Settings → Agent
roles; `GET|POST /api/workspaces/{slug}/agent-roles`, `PATCH|DELETE …/{key}`, `POST …/{key}/reset`).

* A role is a key (immutable, stored on its lines and sent in the envelope), a name, a sentence and a
  set of permissions chosen from `read`, `write`, `agent_edit` and `use:<site>`. It is stored **closed**
  (write carries read and every host site, agent_edit carries write). A custom role can **never**
  hold `admin` or `own`: those come from who the agent belongs to, not from a line.
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
  host sites but never one that drives or changes the agent.
* **The rails follow the role's contents, by permission:** a `domain` or `contact` line can never give a
  role that holds `write`, and an edit that would make a role drive is refused while such a line holds
  it (`roleadmin`). Moving an agent into another tree re-points its lines to the destination's identical
  role, or refuses (`move._carry_roles`).
* `manage.py check_agent_roles` proves every role line is linked to the row of its own tree and grants
  what the code table gave it.

## The access list

An agent's **access list** (`AgentGrant`, `apps/agents/grants.py`) is the one place that says
who has what on it. A **line** is `(who, what)`, with a `manual_only` flag and a free-text note.
`what` is ONE role (a key from the workspace's roles: read_only, read_write, editor, or its own) or,
instead and rarely, ONE host site (the API's `host_site`, stored as `AgentGrant.capability`:
the scoped permission `use:<site>`, which opens that one site and nothing else — not `read`, not read
mode). `who` is one of four kinds (the API's `kind`):

| `kind` | Who | It matches an asker when… |
|---|---|---|
| `person` | One canopy user. | they are that user. |
| `workspace` | Everyone in a workspace, optionally only those with at least a workspace role there (`at_least`), optionally only those whose address is at a `domain`, optionally also everyone in any workspace **below** it (`include_below`). | their effective role in that workspace meets the floor (a parent workspace's owners and admins count here as they do everywhere), and their address is at the domain if one is named. With `include_below`, a role in any workspace of the line's own subtree meets the floor too: that is how a Dimagi-wide line reaches people who are only in a child such as `connect`. The subtree is the line's workspace's descendants in the parent tree, never another organisation's and never upward. Never matches a contact. |
| `domain` | Anyone at an email domain, member or not (the domain must be named). | their address is at the domain. `verified` (the default) also needs THIS message to prove the address. |
| `contact` | A known visitor or sender canopy holds as a contact: an emailer, a Slack person outside the workspace, a widget visitor who may have no address. Optionally at a `domain` (blank: any), optionally `verified`. | canopy knows them as a contact, and the domain and `verified` conditions hold. |

* **A person's permissions are the union of their matching lines** (`grants.resolve`): their
  *role* is the strongest role line (None if only host-site lines name them), and what they may
  do is every line's permissions together. The lines are listed strongest first, role lines before
  host-site lines; the line that gives `write` (or opens the profile the turn runs in) is the
  Turn Context's `source`: `list:<subject key>` (`person:12`, `workspace:connect:editor:`,
  `workspace:dimagi::` + `:below`, `domain:dimagi.com`, `contact:`, and `#use:<site>` appended on a
  host-site line); its words, for a person to read, are `Standing.via`
  ("Anyone at @dimagi.com (verified)").
* **`manual_only`** is a flag on a line: every turn it lets someone start runs `manual`, so an
  agent admin approves what goes out. It matters for lines that give `write` only (a read_only or
  host-site line is confined, whose turns follow the routing ladder). A person is manual only when
  EVERY line that gives them `write` says so: one line without the flag frees the person. Dropping the
  gate is one visible edit to one line, never a code change.
* **Which lines you can write** (`grants.check_line`): exactly one of a role (a key visible from the
  line's workspace, `grants.check_role`) and a host site; a `person` line names a canopy user in a
  workspace you can see (`no such person…` otherwise); a `workspace` line names a workspace you are
  in, and `at_least` must be a workspace role; a `domain` or `contact` line can give `read_only` or
  a host site but **never a role that contains `write`** (`grants.check_role`), because an
  address is something a message can claim, and driving the whole agent takes a named person or a
  workspace's people (lines carried over by an earlier migration, e.g. a verified-contact editor
  line, keep resolving as they did; the rail is on adding and changing); a `domain` line must name
  its domain; `include_below` is for a workspace line only. One line per subject per list and
  per host site (409 on a repeat: change that line instead). Naming a host site the agent
  does not have is allowed with a warning: the line opens nothing until it does.
* **Who may read and change it.** Any member of the agent's workspace reads the list
  (`GET /api/agents/{slug}/access-list`, which also lists the agent's `host_sites: [{name, site}]`
  a line may name). An **agent admin** adds, changes and removes the agent's own lines and turns
  inheritance on or off. Every change is a settings change (`access-list:<slug>`,
  `docs/architecture/settings-audit.md`). The UI is the agent's Settings → Roles → Access.
* **A line can only open what exists.** It cannot open a host site the agent does not have. Read
  mode always exists: a read_only line opens it on an agent with no read mode of its own (canopy's
  default) as on one with its own. Before putting `read_only` on an agent, know that it is live:
  `manage.py read_mode_gate` lists agents with such a line and no read mode of their own.
  `manage.py answering_snapshot` prints how every agent answers, read-only and repeatably, so a change
  to read mode, host sites or the rule can be diffed.
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
is the access list. A stored document that still carries `role:`, `full:`, `editors:` or `callers:` is
refused (422) with a pointer here.

## Read mode and host sites

(The whole path for a site, from the workspace's registration to the gateway, is
[host-sites.md](host-sites.md).)

The two settings admins edit for **what** a confined turn may do (Settings → Channels → Read mode and
host sites; `GET|PUT|DELETE /api/agents/{slug}/read-mode`, `GET /api/agents/{slug}/host-sites`,
`PUT|DELETE …/host-sites/{site}`; `apps/agents/profile_settings.py`). Who may use them is the access list,
never these settings. Saving is for the agent's owner or an agent admin, browser or token; `dry_run: true`
on a PUT checks a draft with the server's own parser without saving; every save is a settings change
(the page shows its history).

* **Read mode** is the default confined profile for someone who holds `read` and not `write`, **on every
  agent with no setup**. With no setting it is canopy's default (`read_mode.default_read_profile`,
  built from the agent and pinned exactly by `tests/test_default_read_profile.py`, so a code change that
  widens every agent at once cannot pass unnoticed; its version rides the envelope as
  `default_profile_version`): it reads the agent's own worktree, drafts a reply in its scratch folders,
  and, when the agent has a mailbox, reads and answers the thread it was asked on; its entry is the
  agent's `/<slug>:answer-caller` skill when its catalog has one. An admin may customise it: the first
  change starts from canopy's default as it stands for this agent and saves it as the agent's own, which
  then replaces the default whole; reset returns to canopy's default. A read-mode turn's envelope lists only
  `read` in `permissions`. A sensitive conversation never gets read mode (its email-reply rules are not a
  sensitive profile).
* **A host site** is one row per registered site: a confined profile (`description`, `entry`, `tools`,
  `bash`, `read_paths`, `write_paths`) plus an optional **site tool limit** (stored and sent as `ceiling`; the form says "Site tools it may use": tool globs
  that narrow what the host's grant already allows, on the owner's word; empty adds nothing), `own_login` (the agent's own credentials at that site) and **Handles sensitive data**. A
  conversation held on that site selects it, and its profile gains canopy's gateway tools (`site_tools`,
  `site_call`, `site_read`) and the page's own actions (`page_*`) → [access-confinement-and-act-as.md](access-confinement-and-act-as.md).
  A new row starts from `profile_settings.new_site_defaults`, saved explicitly so a later change widens nobody.
  Removing a row gives a turn already stamped with it the `deny_all` profile, and a line naming it opens
  nothing. **Handles sensitive data** is per site, not per agent: it confines a conversation on that site
  for everyone (the decision table), while the owner's ordinary chat with the same agent is still the whole
  agent. Such a site that holds a shell, writes, or any tool but the gateway's site tools, `current_page`,
  `who_is_asking` and read-only local tools is a 422.

**Storage and the names that stay.** Both settings are views of one document, `Agent.interface`, which
stays the only storage and is written only by `profile_settings.store` (live state on canopy-web, never a repo
file). Its stored entries live under a `capabilities` key; a host site's row is an entry naming that
one site, and the entry's name (`profile_settings.key_for`, never renamed) is what a turn is stamped with and what
an access line names. These stored and wire names stay, each behind one constant:

* **Read mode's stored name.** Canopy's default is stamped `read`; an agent's own read mode is stored and
  stamped under the name `ask` — the one place that name lives is `read_mode.READ_MODE_STAMP`, and
  `published_profiles.is_read_mode` is the one place that says the two names are read mode. New read-mode entries
  are written under it, and turns stamped with it keep resolving.
* A host site's **site tool limit** is stored and sent as `ceiling` (`Agent.interface`, `/host-sites`).
* `Turn.capability` (the stamp), `AgentGrant.capability` (a line's host site), `AdmissionRule.capability`,
  `use:<name>` in stored role permissions, `data: sensitive`, the envelope's `capability` and `granted_by`
  (`capability:<name>`), and `profile: full|confined` beside `access`. Renaming them would split live
  conversations (`session_naming.restricted_key`, `cloud_runner._kept_identity`).

**One entry per site.** A host site's entry names exactly one site, and no site is named by two
entries: the writer refuses a document that breaks either (`profile_settings.document_problems`). So a site's
tool limit and its own login are that one entry's (`published_profiles.site_ceiling`, `published_profiles.own_login_of`), and
a page's declared resource selects nothing: an entry carrying `pages:` is refused.

**What a document may hold.** The settings routes refuse a document they cannot hold
(`profile_settings.document_problems`, which `manage.py answering_snapshot` reports as `shape`) and leave it as
it is. Retired keys are refused: `full:`, `editors:` and an entry's `callers:` or `role:` are a 422 naming
the access list; `callers_default: none`, `version` and an entry's `input` are accepted and ignored, and
not written back. An unknown key is refused, not ignored. The default document is `{capabilities: {}}`
(`read_mode.default_interface()`): no host site and no read mode of its own. Every agent has
one (a row holding `{}` reads as the default), and an agent with nothing set is reached by its owner, its
agent admins, the people whose lines give `write`, and, in read mode, the people whose lines give `read`.
The old `/interface` routes answer 410.
