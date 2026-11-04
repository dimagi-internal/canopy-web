# Configurable agent roles

**Status:** decided 2026-11-04 (Jonathan's answers are in the box below and win over anything later in this
file that reads differently); built in four PRs, section 13.
Extends `2026-11-02-agent-permissions-and-roles-design.md` ("a user-defined role would be a new row in
the same table and the same engine", line 34-36, and "user-defined roles" under *Left out*, line 107).
The access list, `access.decide`, the sensitive-data rule and the safety rails stay as
`docs/architecture/access.md` describes them.

**The requirement (Jonathan, 2026-11-04):** "the rolesets should all be end user configurable, not
hardcoded."

> **Decisions (Jonathan, 2026-11-04), which amend this spec.**
>
> 1. **The vocabulary stays fixed:** `read`, `write`, `agent_edit`, `admin`, `own`, plus the scoped
>    `use:<door>`. A custom role is a new named bundle of those at the read / write / agent_edit level
>    (for example "read + use:connect", or "write"). It may hold `read`, `write`, `agent_edit` and
>    `use:<door>` entries and can **never** hold `admin` or `own` (those are `Agent.is_admin` and
>    `Agent.owner`, never a line).
> 2. **`agent_edit` is enforced** (a separate change wires the route gates and a runner rail to it), so
>    it **is** offered in the builder; the earlier exclusion (sections 2 fact 2, 4, 14) is dropped. A role
>    containing `agent_edit` needs the workspace **owner** to define, exactly like `write`.
> 3. **The built-ins `read_only`, `read_write`, `editor` are seeded rows**, editable or cloneable and never
>    deletable. `owner`, `admin` and `system` stay fixed in code.
> 4. **Workspace roles (viewer / editor / admin / owner) stay as they are** (section 11).
> 5. Every other open question takes this spec's own recommendation (section 15), except where it
>    conflicts with 1 to 3: the safety rails are by permission and name **`write` or `agent_edit`**
>    wherever the text below says only `write`.

## 1. In plain words

Today an agent has five named roles (owner, admin, editor, read and write, read only). They are fixed in
code. A person who runs a workspace can pick one of three for a person or group, but cannot rename one,
change what it allows, or make a new one such as "Can use Connect only" or "Reviewer".

After this work, **the people who run a workspace define the roles themselves.**

* A role is a **name and a list of things it allows**. The list is chosen from what canopy can actually
  enforce: *be answered in read mode*, *drive the whole agent*, and *use one named door* (a capability the
  agent published). Ticking "drive the whole agent" automatically ticks "read mode"; you cannot build a role
  that can drive but not read.
* The three roles you can give out today (read only, read and write, editor) still exist the day this ships,
  with the same meaning, but they are ordinary entries in a list that you can edit, rename or copy. Nothing
  changes for anyone until someone edits one.
* **Two things stay fixed because they are not "roles you give out":** the agent's **owner** (whoever owns
  the agent) and its **agent admins** (people who hold its keys: credentials, the vault, who else is admin).
  These come from who the agent belongs to, never from a line on the access list, and a custom role
  cannot include them. Letting a role say "admin" would make a second, weaker way to the agent's secrets.
* Roles are defined once per **workspace**, and are usable by that workspace and everything below it, the
  same way a workspace-wide default access line already reaches every agent below it. Define "Reviewer"
  once on Dimagi and it is available to every agent in Dimagi and its divisions.
* **Who may do it:** a workspace admin may make and edit roles that cannot drive or change an agent. Anything
  that lets someone **drive the whole agent** or **change it** (`write`, `agent_edit`) is the workspace owner's call, exactly as it already is for a
  workspace-wide default line, and takes a passkey touch because one edit changes what many people can do.
* **Safe by construction:** a role that is in use cannot be deleted. Editing a role changes what its
  holders can do from the next request, and is written to the settings log with a count of who it
  affects. A role that people known only by an email address (domain and contact lines) hold can never be
  edited to include "drive the whole agent" or "change it".

What this design does **not** do: make the *workspace* roles (viewer, editor, admin, owner) configurable.
Section 11 says why not yet.

## 2. What is hardcoded today (the full list)

The engine is already data-driven. The roles are not.

| Where | What is fixed | File |
|---|---|---|
| The role table | `_TABLE` names exactly five roles; `ROLES` is built once at import | `apps/agents/roles.py:43-62` |
| What a line may give | `GRANTABLE = (EDITOR, READ_WRITE, READ_ONLY)`, copied to `ADDABLE` | `apps/agents/roles.py:64`, `apps/agents/grants.py:178` |
| Display order | `LADDER = reversed(ROLES.order)` | `apps/agents/roles.py:61`, used `apps/agents/api_access_list.py:60` |
| Rail: address-only kinds | `check_role` rejects any role not in `ADDABLE`, then rejects `grants_write(role)` | `apps/agents/grants.py:202-210`, `186-188` |
| Rail: default lines | `_require_default_rights` asks `roles.allows(role, WRITE)` | `apps/agents/api_access_list.py:237-243` |
| What a line grants | `AgentGrant.permissions` calls `roles.permissions_of(self.role)` | `apps/agents/models/agent.py:415-418` |
| Strongest role of a person | `roles.rank` / `roles.highest` over `ln.role` | `apps/agents/grants.py:160-161` |
| Stored width | `AgentGrant.role` is `CharField(max_length=12)` | `apps/agents/models/agent.py:362` |
| API input | `role: Literal["read_only","read_write","editor"]`, twice | `apps/agents/schemas/access_list.py:15`, `:29` |
| API output | roster `role: Literal["owner","admin","editor","read_write","read_only","system"]` | `apps/agents/schemas/agents.py:697` |
| Role descriptions | `_CAN` and `_PERMISSIONS` tables | `apps/agents/api_access_list.py:40-43`, `57-60` |
| Frontend | `ADDABLE_ROLES`, `WRITE_ROLES`, `roleWord` by name | `frontend/src/components/agents/accessWords.ts:25`, `:29`, `:55-62`; used `AccessLineForm.tsx:164`, `AgentAccessList.tsx:231-232` |
| Generated types | the three literals | `frontend/src/api/generated.ts:28748`, `:28797`, `:27995` |
| Envelope text | `lowest_role_with(WRITE)` and `roles.EDITOR` in `who_can_grant` | `apps/harness/caller_context.py:690-692` |
| Session writer stamp | `agent_role=roles.EDITOR, source="session"` | `apps/agents/access.py:207` |
| Owner/admin/system | `roles.OWNER`, `roles.ADMIN`, `roles.SYSTEM` in `_stamp`, roster, vault members | `apps/agents/access.py:199-205`, `apps/agents/credentials/vault_members.py:53,103` |
| Seeded in migrations | role strings written by `0118` and `0119` | `apps/agents/migrations/0118_roles_and_capability_lines.py`, `0119_use_ask_lines_become_read_only.py:49-50` |
| Tests | ~40 files name `roles.EDITOR` / `READ_ONLY` / `READ_WRITE` | e.g. `tests/access_helpers.py:10-42`, `tests/fixtures/site_gateway.py:53` |

Two facts that shape the design:

1. **The decision never reads a role's name.** `decide` unions the permissions of the matching lines and
   asks `roles.holds(permissions, WRITE)` (`apps/agents/access.py:260`); a capability is opened by
   `roles.can_use(permissions, name)` (`apps/agents/roles.py:94-101`, `grants.py:93`). The envelope's
   `permissions` is the contract (`apps/agents/interface.py:596-611`). So making roles data touches the
   places that turn a line into a permission set, not the decision.
2. **`agent_edit` is being enforced** by a separate change (route gates and a runner rail). This spec was
   first written when nothing enforced it; the owner's decision (box above) is that it is offered to role
   builders, with the same owner-only rule as `write`. The built-in `editor` holds it.

## 3. Who may define roles, and where (decision)

**Pick: roles are defined on a workspace, usable by that workspace and every workspace below it; names are
unique along the chain; no overriding.** In the common case that is an *organisation catalog*, defined on
the root (`dimagi`), with a division (`connect`) able to add roles of its own.

Why not the alternatives:

* **Per agent.** An access list's lines are not only per-agent. A workspace default line is a role plus a
  subject that "every agent in that workspace and below inherits" (`apps/agents/grants.py:96-102`,
  `apps/agents/models/agent.py:323-327`). One default line naming a role must mean one thing on every agent
  it reaches, so the role cannot live on an agent. Per-agent roles would also make "Dimagi-wide Reviewer"
  impossible, and put one more thing on the page of every agent.
* **A global catalog.** Canopy has tenants. A role definition is a setting of the people who run a
  workspace, and `Workspace.parent` already says what flows down (`apps/workspaces/models.py:80-92`):
  only ownership flows down; "no other ACCESS flows down" because isolation between divisions is the point.
  A global catalog would let one organisation's role names and edits reach another's; the tenant rule
  ("a write names its workspace", CLAUDE.md) has no meaning there.
* **Catalog on the root with per-workspace *overrides*.** Rejected. The same default line (`Reviewer` on
  `dimagi`) would then grant different things in `connect` than in `dimagi`, and "what does this person
  hold" would need the line's scope *and* the agent's workspace. That is the shadowing ambiguity a role
  table exists to remove (`apps/workspaces/permissions.py:14-19`, five spellings of one tier). A role key
  therefore means one thing everywhere it is visible, because nobody below can redefine it.

Rules that follow:

* **Visible set** of a workspace W = roles defined on W and on each ancestor (the same chain as
  `Workspace.ancestor_slugs`, `apps/workspaces/models.py:161`, and `ancestor_chains`, `:379`).
* **Keys are unique along a chain.** Defining `reviewer` on `connect` is refused (409) if `dimagi` already
  has it; defining it on `dimagi` is refused if any workspace below defines it (the 409 names where).
* **Who defines.** By permission, never by role name (`tests/test_roles_named_only_in_workspaces.py`):
  `perms.MEMBERS_MANAGE` (`apps/workspaces/permissions.py:66`, admin) on the defining workspace, except
  that **creating, editing or deleting a role whose permissions before or after contain `write` or
  `agent_edit` takes `perms.OWN`** (`:102`, owner), the same split as defaults (`api_access_list.py:237-243`, spec 2026-11-02
  lines 71-73). A workspace admin can make "Connect-door only" and "Reader"; only an owner can make or
  change anything that drives the agent.
* **Who may read.** Any member of the workspace (a person choosing a role must see what it means). A
  non-member gets 404, as every list in canopy does.

## 4. What a role is

A role is `(key, label, description, set of permissions)`.

* **key:** immutable slug, `^[a-z][a-z0-9_]{0,39}$`. Stored on lines, sent in the envelope's `role`. The
  existing keys `read_only`, `read_write`, `editor` are kept so nothing already stored or exported moves.
  **Reserved, refused:** `owner`, `admin`, `system` (fixed, below), and the retired words `viewer`, `user`,
  `member`, `contact` (`tests/test_retired_access_vocabulary.py`; `docs/architecture/access.md` glossary).
* **label:** the name a person sees ("Read and write", "Reviewer"). Editable. The UI is the only place the
  label appears; the key is what machines read.
* **description:** optional, one sentence of the creator's words. Shown beside the generated "can ..." text.
* **permissions:** a subset of the **fixed vocabulary**, chosen in the builder:
  * `read`: be answered in read mode (`roles.py:25`).
  * `write`: drive the whole agent in its own authority (`roles.py:27`). Implies `read` and every
    `use:*` (`roles.py:50-55`).
  * `use:<door>`: open one named capability the agent published (`roles.py:33`, `roles.py:80-82`). A role
    may hold several. The name is free text validated by the same pattern as a capability line
    (`grants.py:175`), with the same advice-not-refusal when an agent has not published it
    (`api_access_list.py:106-113`): a role is defined above agents, so it cannot know their doors.
  * `agent_edit`: change the agent itself (its skills and capabilities). Implies `write` (and so `read` and
    every `use:*`). Offered in the builder (decision 2); owner-only to define, like `write`.
  * **`use:*` alone is not offered.** It is what `write` is; a role meaning "every door but not drive" is
    not a thing the decision distinguishes (`access.py:260-276`).
* **The dependency rule is enforced at save.** The service runs the submitted set through
  `RoleSet.close` (`apps/common/rolesets.py:64-73`) and **stores the closed set**, so a stored role cannot
  hold `write` without `read` (the builder ticks the implied boxes and shows them as implied). The set is
  closed again when loaded (idempotent), so a later change to the rule (a new implication) reaches stored
  roles without a migration. An empty set is refused (a role that grants nothing is a bug, not a role).

**Can a custom role contain `admin` or `own`? No.**

* `admin` is `Agent.is_admin`: the owner, a workspace owner, or an `AgentAdmin` row (`docs/architecture/
  roles.md`, "agent_for_admin"). Every route that holds the keys asks `agent_for_admin` (`gates.py:17`),
  not a line; the role table already says an access-list line "cannot give `admin` or `owner`"
  (`docs/architecture/access.md`, "Workspace defaults"; `grants.py:204-206`). A role that *listed* admin
  would either do nothing (a lie in the UI) or be a second door to credentials and the vault key copies
  (`docs/architecture/vault.md`: "who may hold a copy of its key" is admins), bypassing the one place that
  decides it. A role may not widen that.
* `own` is `Agent.owner`, single by construction (`roles.py:31-32`; spec 2026-11-02 line 26).

So `owner` and `admin` stay in code as **fixed roles**, not rows. `system` likewise (a non-person actor,
`roles.py:20`). They keep appearing in the roster, the envelope and the Roles page, read-only, so the page
tells the whole story; the fixed vocabulary and the closure engine stay in `apps/agents/roles.py` (the
vocabulary is "what the platform can enforce"; the roles are bundles of it).

### Safety rails, extended by permission (never by role name)

The existing rails are already stated by permission and stay verbatim:

* A `domain` or `contact` line can never give a role containing `write` or `agent_edit` (`grants.py:183`, `:207-210`;
  spec lines 68-70). `check_role` keeps that rule and loses only its `role not in ADDABLE` clause
  (replaced by "role is visible to this scope").
* A workspace default whose role contains `write` or `agent_edit` is the workspace owner's call
  (`api_access_list.py:241`).

Configurable roles open two new ways round them, both closed here:

1. **Editing a role under an address-only line.** `read_only` today cannot become writable, so a verified
   `dimagi.com` contact line can never drive an agent. If an admin could add `write` to a role such a line
   uses, the line would silently begin to drive every agent it reaches, and the rail ("on adding and
   changing a line", `grants.py:180-182`) would not fire because no line changed. **Rule:** an edit that
   adds `write` or `agent_edit` is refused (422, naming the lines) while any `domain` or `contact` line holds the role.
   Already-existing legacy address-only write lines (Eva's, spec line 108) keep resolving as today and are
   listed in the refusal so they can be removed first.
2. **Raising a role that a workspace default uses.** An edit that adds `write` to a role used by any
   *default* line is, in effect, "give every agent here to these people" and needs `OWN` like adding the
   default would. Covered by "before or after contains `write` takes OWN" in section 3, no extra rule.

`manual_only` stays a property of the **line** (`models/agent.py:367`), not of the role; a role is not
asked "does it need approval".

## 5. The five built-ins

| Today | Becomes | Editable? | Why |
|---|---|---|---|
| `read_only` | seeded row | yes: label, description, permissions; not deletable; "reset to default" | A person can make it include a door or rename it "Reader". Read mode is an agent-wide default, keyed to the permission `read` (`interface_default.is_read_mode`), not to this row. |
| `read_write` | seeded row | same | |
| `editor` | seeded row | same; keeps `agent_edit` in its stored set | Editing it cannot remove `agent_edit` from the *vocabulary*; the stored set is whatever it is saved as. |
| `admin` | fixed in code | no | `Agent.is_admin`; not a line (`roles.py:64` comment, `grants.py:176-178`). |
| `owner` | fixed in code | no | `Agent.owner`; not a line. |

* **Seeding:** one row per built-in on the **root** workspace of each tree, created by the data migration
  for existing roots and by workspace creation for new roots (the same place that makes the first
  membership, `apps/workspaces/services.py`; only `ensure_member` / `_grant` / creation make memberships,
  CLAUDE.md, so seeding a catalog there is the established pattern). A child workspace sees them through
  the chain. No row has a NULL workspace (CLAUDE.md "never a NULL-means-allow tenant").
* **"Builtin" is a flag, not a privilege.** It means *not deletable* and *has a reset-to-default*. The
  defaults live in one place in code (`roles.SEED`, the old `_TABLE` entries for these three), used only
  by the seeder and the reset action. The decision never reads `SEED`.
* **Cloneable.** The Roles page's "Copy" creates a new role pre-filled from any visible one. Editing in
  place is for "we call it Reader"; copying is for "like Read and write, plus Connect".
* **Name-keyed code that survives.** `access.py:207` stamps a session writer's display role `editor`;
  `caller_context.py:690-692` prints "as read_write or editor". Both are text/labels, not decisions. They
  stay correct because built-ins are not deletable; Phase 3 changes `who_can_grant` to list the visible
  roles that hold `write` (from the catalog) rather than two literals.

## 6. Data model

```
AgentRole
  workspace   FK Workspace, NOT NULL, CASCADE     # the defining workspace
  key         CharField(40)                       # immutable; unique (workspace, key); chain-unique by the service
  label       CharField(80)
  description CharField(300, blank)
  permissions JSONField                           # the CLOSED set, e.g. ["read","write","use:*"]
  builtin     BooleanField                        # not deletable; has "reset to default"
  position    PositiveSmallIntegerField           # order in the catalog and the picker
  created_by  FK User (SET_NULL), created_at, updated_at

AgentGrant (existing)
  role        CharField(40)   # widened from 12 (models/agent.py:362); the key, immutable copy of role_def.key
  role_def    FK AgentRole, PROTECT, null=True (null exactly when `role == ""`)   # NEW
```

* **A line points at its role row, not at a name to look up.** The line stores the row (`role_def`) and
  the key (`role`, kept so the envelope, generated types, `describe`, the unique `subject_key` and the
  existing `agent_grant_role_or_capability` check constraint, `models/agent.py:385-388`, all keep working
  unchanged). Because keys are immutable and the row is fixed at the moment the line is added, the two
  cannot drift. This removes any read-time resolution across the chain (no shadowing, nothing to cache) and
  makes "delete refused while used" a **database guarantee** (`PROTECT`), not a convention the next author
  can forget. The chain matters only at *add* time (which roles may I choose).
* `AgentGrant.permissions` becomes `self.role_def.permissions` for a role line (still
  `frozenset({use:<capability>})` for a capability line); `roles.permissions_of` stays for the fixed
  owner/admin/system (`models/agent.py:415-418`, `interface.py:598-599`).
* **Constraints:** `UniqueConstraint(workspace, key)`; a check that `role == "" <=> role_def IS NULL`.
  Chain-uniqueness and "no empty set" are service rules (a DB cannot express them), tested.
* **Save path.** `AgentGrant.save` already derives `subject_key` (`models/agent.py:420-427`); it also
  resolves `role_def` from `role` along the line's chain when it is not given. Production routes pass the
  row explicitly; the shim keeps `tests/access_helpers.py` (and ~40 test files) working and is what the
  migration backfill uses.
* **Why not store the role set on the line?** A role edit must change its holders, which means one place to
  edit. Copying onto the line would make an edit a data migration.

### Migration of existing `role` strings

Four migrations over two PRs (the rule in CLAUDE.md: a `RunPython` that updates rows other tables
reference goes in its own migration, ahead of any schema change to those tables; `0119` did the same):

1. `0121` schema: create `AgentRole`; widen `AgentGrant.role` to 40; add `role_def` nullable. (PR 1)
2. `0122` data: seed `read_only`, `read_write`, `editor` on every root workspace with today's permissions
   (closed). It only inserts into the new table. (PR 1)
3. `0123` data (its own migration, it updates lines): set `role_def` on every line with a role to the seeded
   row of **its tree** (agent line: the agent's workspace root; default line: its workspace's root); one
   `SettingChange` per tree, operation `configurable_roles_migration`. A `role` string that is none of the
   three (nothing should exist; `0118` removed the old ones) aborts the migration naming the line rather
   than guessing. (PR 1)
4. A later pair in PR 2: re-link any line written by old code during the deploy window, then add the
   `role == "" <=> role_def IS NULL` constraint, each in its own migration.

`manage.py check_agent_roles` proves, from live data, that every role line is linked to the row of its own
tree and grants the permissions the code table gave it (run it before and after).

Test lives until the migration deploys, then is deleted (CLAUDE.md "A data migration's test lives until its
migration deploys"); `tests/test_migrations_apply.py` stays.

## 7. API

All under the one `NinjaAPI`; request and response models in `apps/agents/schemas/`, routes in a new
`apps/agents/api_roles.py` beside `api_access_list.py`. Every route declares `@gate` (CLAUDE.md,
`tests/test_every_route_declares_its_gate.py`).

| Route | Gate | Notes |
|---|---|---|
| `GET /api/workspaces/{ws}/agent-roles` | `member` | the roles visible to `{ws}`: own and ancestors', with `defined_on`, `builtin`, `permissions`, generated `can`, `in_use` (lines), `can_edit` |
| `POST /api/workspaces/{ws}/agent-roles` | `members.manage`, `setting=True`, `step_up=True` | `write` in the set also needs `own` |
| `PATCH /api/workspaces/{ws}/agent-roles/{key}` | same | label, description, permissions; never the key |
| `POST .../{key}/reset` | same | restore a built-in's defaults |
| `DELETE .../{key}` | same | 409 while any line holds it, listing them (agent or workspace, who); built-ins 409 always |
| `GET /api/agents/{slug}/access-list` (existing) | `member` | `roles` becomes the roles **visible to this agent** (its workspace's chain) with `key`, `label`, `can`, `permissions`, plus two booleans the form needs: `contains_write` and `address_only_ok` (replaces the hardcoded `WRITE_ROLES`) |
| `POST/PATCH .../access-list` (existing) | `agent-admin`, `setting=True` | `role` is `str`; the service checks it is visible and applies the existing rails |
| `GET/POST/DELETE /api/workspaces/{ws}/agent-defaults` (existing) | unchanged | `roles` = those visible to `{ws}` |

* **`Literal` becomes `str`.** `AccessLineIn.role`, `AccessLinePatch.role` (`schemas/access_list.py:15,29`)
  and the roster's `role` (`schemas/agents.py:697`) are `str | None`. The error for a bad role is the
  service's 422 in the server's words, as `check_role` does today (`grants.py:204`). The `gen:api` types
  (`generated.ts:28748,28797,27995`) change from unions to `string`; the route docstring at
  `api_access_list.py:138-142` is published as the OpenAPI description (CLAUDE.md), so it stops listing
  names and points to `GET .../agent-roles`.
* **MCP and the CLI.** MCP is the REST API (`docs/architecture/mcp-surface.md`): the new routes are tools
  automatically (`list_agent_roles`, `create_agent_role`, ...); `add_access_line` loses its `enum` for
  `role`, so an agent adding a line must read the roles first. That is the same step a human takes and is
  a documented behavior change for the plugin's skills (grep the skills for `read_write` before Phase 2).
  Nothing is excluded from MCP (CLAUDE.md "MCP is the REST API"). A role write from a PAT or MCP is not
  step-up-gated (a token never steps up, CLAUDE.md); it must therefore keep the owner rule for `write` and
  the audit, and the route decides "token allowed" explicitly, as other routes do
  (`docs/architecture/api-surface.md`, Passkeys).
* **Audit.** Every role write is `@gate(..., setting=True)` and calls `audit.note_change(scope=
  f"agent-roles:{ws}", before=, after=, summary=)` in the same transaction, like
  `api_access_list.py:145-147`. The summary carries the **blast radius** ("changes what 14 lines on 9 agents
  and 3 workspace defaults grant"), computed from the same `in_use` query. Never a per-route audit
  (CLAUDE.md, `docs/architecture/settings-audit.md`).
* **Step-up.** The two access-line write routes carry no step-up today (`api_access_list.py:136,160`)
  because one line is one person. A role edit is **one action that changes what everyone holding it can
  do**, so create, edit, reset and delete require a passkey step-up (`require_recent_step_up(request)` after
  the 404/403 checks; `step_up=True` on `@gate`), for every role, not only write-containing ones: roles are
  rarely edited and a conditional step-up is a rule someone gets wrong. (Open question 3.)

## 8. The caller envelope and everything that reads roles

* **`role` in the envelope is a string key; `permissions` is the contract** (already shipped,
  `interface.py:576-583` `grant_facts`; `_held_permissions` `:589-611`). Nothing reads a role name to decide
  what a caller may do, and docs must say so: a reader that branches on `role == "editor"` breaks the day
  someone renames or reshapes it; a reader on `permissions` does not. Phase 3 puts a sentence in
  `docs/architecture/access.md` and the plugin's caller-context skill.
* The envelope's `role` is "the strongest role line" (`grants.py:160-161`). With free bundles, "strongest"
  is not a chain, so the rule is stated: **most permissions after closure, ties by key**. It is for display
  and the envelope only; `permissions` carries the decision. `RoleSet.strongest` is replaced for agent
  roles by this over the line's `role_def` (the rolesets engine keeps its rank for the *workspace* ladder).
* An in-flight turn's envelope is re-decided on read (`interface.py:567-580`, `decide_for_turn`), so a role
  edit reaches it too, as a line edit does today. The turn's capability stamp and its admitted
  confinement are not re-opened. Verification item for Phase 2: a test that edits a role between enqueue
  and read.

## 9. Bulk reads and caching

The rule: a view judging many (person, agent) pairs reads the facts once (`docs/architecture/roles.md`,
"A view that judges many pairs reads once"; `tests/test_bulk_authz_parity.py`).

* **Roles ride with lines.** `lines_for` (`grants.py:96-102`) and `lines_for_many` (`:105-126`) add
  `select_related("role_def")` to the queries they already make. `Standings` (`standings.py:50-56`) does
  not change: it stores the lines it read, and `Resolution.permissions` is computed from them. **No new
  query, and none per pair**; the pinned query counts (`tests/test_fleet_reads_do_not_grow.py`) hold.
* **Nothing is cached across requests** (`roles.md:316-318`: "read per request and dropped with it"). That
  is why an edit applies from the next request with nothing to invalidate. The catalog read for the
  *picker* (`GET .../agent-roles`, the access-list `roles`) is its own small query by chain, not on the
  decision path.
* **Parity test.** Extend `tests/test_bulk_authz_parity.py`'s world with a custom role (a door role, a
  role holding `write`, a role defined on a child) and assert `Standings.of(...)` and `decide` agree, and
  that `lines_for_many` issues the same number of queries with 1 and 20 custom roles.

## 10. Edit and delete while in use

* **Lines keep working.** A line stores the row, so editing the row changes what it grants and nothing
  else breaks. Renaming a label touches no line. A key never changes.
* **Delete is refused while used (409)**, naming the agents and workspace defaults that hold it, by
  `PROTECT` and by the service's friendly pre-check. The caller either removes those lines, **or reassigns
  them in one step** (`DELETE .../{key}?reassign_to=<key>`, same rails: the target must be visible to
  every scope and respect the address-only rail). Reassigning is one `SettingChange`.
* **Removing a role's last permission or an edit that adds `write` to an address-only-held role** is
  refused (section 4).
* **Moving an agent between workspaces** (`apps/agents/move.py:440-451`) must also check roles: a line
  whose `role_def` is not visible from the destination's chain would grant something the destination's
  owners never agreed to. The move preview (`p.breaks`) lists those lines; the move re-points a line to the
  destination's same-key role only when its permissions are identical, else refuses.
* **Nesting or detaching a workspace** (`set_workspace_parent`, an owner action) changes chains. As
  built (`roleadmin.carry_lines_after_move`): a line in the moved subtree whose row its new chain does not
  show is re-pointed to the role of that key it can see when the permissions are identical, and otherwise
  the move is refused, naming the lines. Open question 5 (merge vs. refuse).
* **Workspace deletion** removes its roles with it. It is refused (409, saying what to remove) while the
  workspace has default lines or anything still holds one of its roles: `role_def` is `PROTECT`.

## 11. The workspace roles: not now

**Recommendation: do not make workspace roles (viewer, editor, admin, owner) configurable in this work.**
Make agent roles configurable first; give workspace roles a read-only "bundle by permission" view later.

Why:

* **The blast radius is the whole product.** 14 permissions gate every route
  (`apps/workspaces/permissions.py:109-124`), through `perms.can` / `slugs_with` / `role_allows` and the
  route gates (`apps/api/route_gates.py`), not one decision function. An agent role changes what one
  decision grants; a workspace role changes whether someone may read logs, manage members, hold the Slack
  app, delete the workspace.
* **The ladder is structural, not just a table.** The workspace roles are a ladder with rank, not free
  bundles (`RoleSet.ladder`, `permissions.py:131-134`), and rank is load-bearing: `may_manage_member`
  (`:222-245`) lets an admin act only strictly below themselves; inheritance down the tree is *only* owner
  and admin (`models.py:73-79`, `WorkspaceMembership.INHERITS_DOWN`); `Workspace.auto_approve_role`,
  invites, the last-owner guard, access requests, the role cap (`apps/workspaces/role_cap`) and an access
  line's own `at_least` floor (`grants.py:135-139`, `AT_LEAST_OPTIONS` in the UI) all name those four. A
  custom role between editor and admin has no defined answer to "may an admin remove one?".
* **Confused-deputy is possible.** Several permissions are deliberately owner-only because they cut other
  people off or hold keys (`OWN`, `CONTROL`, `permissions.py:97-107`). A configurable table would make
  "which permission may an admin grant themselves" an authorization question about the authorizer.
* **There is one authorizer by design.** Only `apps/workspaces/services.py` decides membership
  (CLAUDE.md); more states multiply its test matrix (`tests/test_workspace_authorizer_is_sole_gate.py`,
  `tests/test_roles_named_only_in_workspaces.py`).
* **Cost:** a `WorkspaceRole` table, per-permission minimum rank replaced by sets, a rank for each custom
  role, a migration of `WorkspaceMembership.role` (an enum used in `ROLE_RANK` everywhere), the role cap,
  invites, the members UI and the whole MCP/CLI surface: an order of magnitude beyond this spec.
* **What to do later, cheaply:** a read-only "what each workspace role can do" page generated from
  `MINIMUM_ROLE` (the data already exists: `permissions.py:109`), and the question "do you need a new tier,
  or a permission change?" asked case by case. If a real tier is needed, add a permission-based rail (like
  `agent_for_work`) rather than a role.

## 12. Frontend

* **A Roles page** under the workspace's Settings (`settings/<section>`, one descriptor in
  `frontend/src/guide/surfaces.ts` so the `/guide` coverage test passes; "a new page needs a descriptor",
  CLAUDE.md). A dense table (not cards, CLAUDE.md "APP UI"): label, key, defined on, "can ..." in words,
  in use (N lines on M agents), built-in badge, row actions Edit / Copy / Reset / Delete. Fixed rows (agent
  admin, owner) are listed read-only with a note "comes from who the agent belongs to".
* **The editor** is a small form: name, key (create only), one sentence, and a checklist in plain words
  ("Be answered in read mode", "Drive the whole agent", "Use these doors: ..."). Ticking drive ticks read
  and the doors and says so ("includes read mode and every door"). The server's refusal (422/403/409) is
  shown beside the form in its own words, as the access form does (`AccessLineForm.tsx:8-16`).
* **The access-line form** (`AccessLineForm.tsx:164-176`, `AgentAccessList.tsx:231-232`,
  `WorkspaceAgentDefaults.tsx:89`) renders `data.roles` (key, label, `can`) instead of `ADDABLE_ROLES`, and
  disables a role by `contains_write` for address-only kinds / non-owners instead of `WRITE_ROLES`. Delete
  `ADDABLE_ROLES` and `WRITE_ROLES` (`accessWords.ts:25,29`); `roleWord` (`:55-62`) reads the label from the
  role list passed in, falling back to the key. `ascending` (`:42-44`) is gone; the server sends them
  weakest-first.
* **"What this role can do"** appears next to every choice: the generated `can` text (the `_CAN` strings,
  `api_access_list.py:40-43`, extended with "use the 'connect' door") plus the creator's description.

## 13. Phased plan (as built)

Each phase is one PR (or a few small ones), correct and safe to deploy alone, merged before the next.

| # | PR | What | Verification |
|---|---|---|---|
| 1 | **Data.** `AgentRole`; `AgentGrant.role` widened to 40 and `role_def` added (`0121`); built-ins seeded on every root (`0122`); every role line linked to its tree's row (`0123`, its own migration: it updates rows); seeder on root creation and a save shim that links a line by key; this spec; `manage.py check_agent_roles` | No behaviour change: nothing reads the rows yet. | Migration test (a child, a default line, another tree, a capability line, a non-built-in aborts); `tests/test_migrations_apply.py`; the live-data parity command. |
| 2 | **The resolver reads the rows.** `roles.py` is the seed, the fixed roles and validation; `AgentGrant.permissions` is the row's; `lines_for*` `select_related`; `resolve` ranks by permissions; safety rails by permission (`write` or `agent_edit`); a later migration re-links stragglers and adds the `role == "" <=> role_def IS NULL` constraint | Seeded rows give today's permissions, so every decision is unchanged. | `test_access_matrix`, `test_bulk_authz_parity` with a custom role and a fixed query count, the caller-envelope contract. |
| 3 | **API, gates, audit.** `api_roles.py` (list, create, edit, reset, delete with `reassign_to`), chain visibility and key uniqueness, closure at save, owner rule for `write` and `agent_edit`, address-only rail on edit, audit with blast radius, step-up; access-line `role` becomes `str`; `generated.ts` | First time a role can change. | `tests/test_agent_roles_api.py`, route-gate and role-naming guards. |
| 4 | **UI and docs.** The Roles page, role picker with words, `access.md`, `roles.md`, `api-surface.md`, `settings-audit.md`, `pages.md`, CLAUDE.md | | Vitest, `tsc`, `eslint`, doc guards. |
| later | Move and nest honour roles (section 10); the read-only "workspace roles by permission" view (section 11). | | |

## 14. What NOT to do

* Do **not** let a custom role hold `admin` or `own`, or make a line the way to be an agent admin.
* Do **not** key behavior on a role name anywhere (envelope readers, `decide`, plugins); `permissions` is
  the contract. Adding `if role == "editor"` is the bug this design exists to prevent.
* Do **not** allow overriding an inherited role key in a child workspace.
* Do **not** resolve a line's role by key at read time, or cache roles across requests; the row is on the
  line, read with the lines.
* Do **not** relax the address-only or owner-only rails "because an admin made the role"; they are by
  permission and must follow the role's current contents (section 4).
* Do **not** audit per route or hand-write a settings row; use `@gate(setting=True)` and
  `audit.note_change`.
* Do **not** exclude the new routes from MCP, or a client-side loop over pins; roles are workspace data
  read by workspace.
* Do **not** touch workspace roles in this work.
* Do **not** add a NULL workspace to `AgentRole`; a row with no workspace would be visible to nobody
  (CLAUDE.md "never a NULL-means-allow tenant").

## 15. Open questions, and what was decided

Jonathan's answers (box at the top) settle the first four items below; everything else took this spec's
recommendation.

1. **Where do custom roles live?** Any workspace, visible downward, no overrides (recommendation taken).
2. **May a workspace admin make roles?** Admin for roles that cannot drive or change the agent; owner for
   anything with `write` or `agent_edit` (recommendation, widened to `agent_edit` by decision 2).
3. **Step-up on every role edit?** Yes, every create, edit, reset and delete. A token cannot step up, so a
   role write by token is refused when the role before or after holds `write` or `agent_edit`, and allowed
   (owner rule and audit intact) otherwise.
4. **`agent_edit`:** enforced elsewhere and offered here (decision 2). `editor` and `read_write` differ by it.
5. **Nesting a workspace whose catalog collides with the new parent's:** refuse, naming the keys; the
   built-ins every root is seeded with are not a collision, and a role key seen along a chain resolves
   to the one nearest the root. (Follow-up work, not in the four PRs.)
6. **May built-ins be deleted?** No. They can be renamed, reshaped, reset.
7. **Label in the envelope?** No: the envelope carries the key and `permissions`.
8. **Per-person toggles instead of named roles?** No.
