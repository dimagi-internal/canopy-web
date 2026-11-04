# Agent permissions, named roles, and optional capability doors

**Status:** proposed 2026-11-02, for Jonathan's review. Supersedes the role ladder of
`2026-10-30-agent-roles-and-access-list-design.md` (the access list, workspace defaults, `decide`
and the sensitive-data rule stay exactly as that spec and `docs/architecture/access.md` describe).

## Why

Jonathan, reading the Access screen: **"viewer" is misleading** (it can ask, it cannot see anything
else), **"user" is confusing** (everyone is a user) and **write vs run cannot be told apart**. The
ladder `owner > admin > editor > user > viewer` also made the common case awkward. Most agents are
meant to be used in full by anyone who has access to them; a narrow, hand-built capability ("door") is
the exception, yet the old `user` and `viewer` roles existed mostly to decide who could open which
door, and every capability had to declare which of the two it needed.

## Permissions

What a person may do with ONE agent, five of them:

| Permission | Means |
|---|---|
| `read` | Ask the agent through its read-only `ask` capability. **Off until the owner publishes `ask`**: nothing publishes it by default and the access-list form does not pre-select it. |
| `write` | Drive the whole agent, in its own authority: every skill it has (today's `write` + `run`, one permission). |
| `agent_edit` | Change the agent itself: its skills and capabilities (today's `edit`). |
| `admin` | Configure it in canopy-web: access list, capabilities, credentials, routing. |
| `own` | Transfer or delete it and name its admins. Single owner, `Agent.owner`; never a line. |

**Dependency rule** (data, enforced when a role is built and tested): `agent_edit` implies `write`
implies `read`; `admin` implies everything except `own`. `write` also implies every scoped
`use:<capability>` (below). Nobody can hold a permission without what it depends on.

## Roles

A role is a named bundle of permissions, in ONE table (`apps/agents/roles.py`). Built-ins only for now;
a user-defined role would be a new row in the same table and the same engine, which is why the table
is data and the closure is computed rather than hand-written.

| Role | Permissions | Replaces |
|---|---|---|
| `read_only` | read | `viewer` |
| `read_write` | read, write | new: today's `editor` without the right to change the agent |
| `editor` | read, write, agent_edit | `editor` |
| `admin` | all but own | `admin` (still `Agent.is_admin`, not a line) |
| `owner` | all | `owner` |

`read_write` and `read_only` are placeholder names. `user` and `viewer` are gone as agent roles.
`system` stays a non-person actor (canopy, the agent's own login, an approved task's dispatch).

## Optional, rare: capability lines

A line grants **either one role or one named capability**: `Anyone at dimagi.com may use "connect"`.
The line's permission is the scoped `use:connect`. It opens that capability and nothing else (no
`read`, no `ask`). Several capabilities for one subject are several lines.

A capability **loses its `role: viewer | user` field**: it says what it is (tools, paths, sites), never
the level it needs. A published interface that still carries `role:` is refused with a pointer, like
the other retired keys. `read` opens exactly the capability named `ask`; `write` opens all of them
(it drives the whole agent anyway); `use:<name>` opens `<name>`.

A person's permissions are the **union** of every line that names them. Their *role* (shown, and the
`role` in the caller envelope) is the strongest role line; a person with only capability lines has no
role, only capabilities.

## Safety rails, by permission

Same behaviour as today, now stated as a permission rather than a role name:

* A `domain` or `contact` line (an address a message can claim) can **never grant a role containing
  `write`**: it may give `read_only` or a named capability. (Legacy lines already on a list keep
  resolving as they did, as before; the rail is on adding and changing a line.)
* A **workspace default** line whose role contains `write` (read_write, editor) is the workspace
  **owner's** call; other default lines are the workspace admin's. Capability lines and read_only are
  the admin's.
* `admin` is still not addable as a line; `manual_only` still gates a line that gives `write`.

## Subtree option

A `workspace` line may be **"everyone in workspace X, or any workspace below it"** (`include_below`).
It makes Dimagi-wide lines work for people who are only in a child workspace such as `connect`. It is
the subtree of the line's own workspace in the parent tree, so it never crosses organisations. The
`at_least` floor and the domain narrowing apply to the role the person holds in whichever workspace of
that subtree matches.

## One engine for agent and workspace roles

`apps/common/rolesets.py` is a small, Django-free engine: a vocabulary of permissions, a table of
roles, an implication rule, `allows(role, permission)`, `holds(permissions, permission)` and rank. Both
`apps/agents/roles.py` (bundles + implications) and `apps/workspaces/permissions.py` (a ladder:
each role adds permissions, derived from the existing `MINIMUM_ROLE` table) build one. Workspace
vocabulary (`logs.read`, `members.manage`, ...) and the ladder `viewer < editor < admin < owner <
superuser` are unchanged; nothing is renamed on the workspace side.

## Migration (`agents/0117`, `0118`)

* `0117`: `AgentGrant.capability`, `AgentGrant.include_below`; `role` may be empty when `capability`
  is set (a check constraint requires exactly one).
* `0118` (data): old `editor` lines become `editor`. Old `viewer` lines become `read_only`, **plus**
  one capability line for every other capability that was open to viewers (so a site's `connect`
  visitors keep their door). Old `user` lines become one capability line for every capability that role
  could open (all of them); a `user` line with no published capability disappears (it admitted nobody
  to anything). Every stored capability loses `role`; the interface source is regenerated. `manual_only`
  is carried over. Each agent's old interface, source and lines go to the settings audit
  (`SettingChange`, operation `access_roles_migration`), as `0112` did.

## Left out (follow-ups)

* `admin` as an addable line; user-defined roles.
* Dropping the legacy address-only write lines (Eva's verified `dimagi.com` contact line) after
  review.
* A UI preset "Turn on ask" on the Capabilities tab (the template exists; only the access form's
  default changed).
