# Agent roles and one access list

**Status:** building, 2026-10-30. Jonathan, after an audit showed the same colleague reaching five different amounts of five agents, depending on which workspace the agent happened to live in and which domains its interface had copy-pasted: "we start to use groups for permissions instead of individuals", one role vocabulary, and the interface should stop being a place where nobody understands who gets what.

## The problem

Access to an agent was decided by four stacked things that each used the word "member" differently:

1. the person's **workspace role** (and the rule that only a parent's owners and admins reach into child workspaces);
2. an implicit rule that a workspace **editor** is an **agent editor** ("full profile, every turn manual");
3. the agent's published **interface**: `full:` rules, per-door `callers`, `callers_default`, `editors: confined`;
4. which **domains** each agent's author remembered to list.

`member` meant "a member of the agent's own workspace", so an editor, a viewer and a workspace admin were all "members", and a Dimagi editor who was not in the agent's workspace was a "contact". Eight of fifteen agents publish no interface and are reachable by every editor of their workspace; four hand-wrote the same `ask` door with the agent's name swapped in.

## Vocabulary (one meaning each)

**Role** — what a person may do with ONE agent. One ladder, strongest first:

| Role | May |
|---|---|
| **owner** | Everything an admin may, plus transfer or delete the agent and name its admins. |
| **admin** | Configure the agent in canopy-web: its access list, doors, credentials, routing, runners, integrations. |
| **editor** | Steer the whole agent (every skill it has, in its own authority) and change the agent itself (its skills and capabilities). |
| **user** | Use the agent as designed: open any door it publishes for users, including doors that act. |
| **viewer** | Ask. The agent's `ask` door and nothing else. |

`system` is not a person: canopy itself, the agent's own login, an approved task's dispatch.

**Permission** — what a role is made of. `read` (open a viewer door) · `write` (open a user door) · `run` (steer the whole agent) · `edit` (change the agent's skills and capabilities) · `admin` (configure it) · `own`. A role is a bundle of these; `roles.py` holds the table, and nothing else in the code names a role.

**Source** — why someone has a role: named, a workspace group, a contact group, or implicit. A **role** is what you can do; a **source** is why. They are never the same word.

**Access list** — the one place that says who has which role on an agent. A **line** is `(who, role)`. `who` is one of:

* a **person**;
* **everyone in a workspace** (optionally "at least editor" there). Reads the person's effective role in that workspace, so a parent's owners and admins count as they do everywhere else;
* **contacts**: people who are not in canopy as members and reach the agent by email or Slack, optionally "verified, at domain D".

The highest role among a person's matching lines wins. Implicit lines, shown in the list but not editable there: the agent's owner → owner, the agent's workspace owners → admin, explicit admins → admin.

**Workspace defaults** — lines set once on a workspace that every agent in it (and below) inherits unless that agent sets `inherit_workspace_defaults: false`. This is the "dimagi-wide" / "workspace-wide" mechanism: one line on `dimagi` reaches every agent there. A new agent inherits its workspace's defaults and nothing else.

**Door** — a named profile an agent publishes for viewers and users: which tools, paths and sites a confined session may touch (`ask`, ACE's `connect`, labsdata's `data`). A door says only the **lowest role** that may open it (`viewer` or `user`). It never says who: that is the access list's job. The standard `ask` profile is a platform template an agent turns on with one line.

**Contact** is not a role. It describes how canopy knows someone (not a workspace member); a contact who matches a contacts line is a viewer or user by that line, and with no line is refused.

**Admin is never bare.** There are six unrelated admins (agent admin, workspace admin, runner admin, vault key holder, Google Workspace admin, GitHub org owner). This spec unifies the first two only; every message that names one names which.

## The decision

`access.decide` keeps its signature and stays the one function every door asks. It resolves the role from the lines, then:

| Role | access | mode |
|---|---|---|
| owner · admin · system | `full` | the routing ladder; may ask for `auto` |
| editor | `full` | the agent's own mode, unless the winning line says `manual_only` |
| user | `confined`, to the door asked for (else `ask`), if that door's lowest role is `user` or `viewer` | the routing ladder |
| viewer | `confined`, to a door whose lowest role is `viewer` | the routing ladder |
| none | refused, with what the asker CAN do and who can raise them | — |

The sensitive-data rule stays over the top, unchanged.

## Compatibility

Existing behaviour is preserved at cutover by translating, not by reinterpreting:

* the implicit "workspace editor → editor tier, manual" becomes an explicit workspace-default line `everyone in <ws> at least editor → editor, manual_only`;
* each `full:` rule becomes a line (`member` → everyone in the agent's workspace; `contact@d:verified` → verified contacts at d);
* each door's `callers` become lines (or, when they repeat the agent's other lines, nothing) and the door keeps only `role`;
* `editors: confined` is the absence of the editor line;
* `Agent.owner` and `AgentAdmin` stay the stores of owner and admin for now and appear as implicit lines.

`manual_only` is a flag on the line so that dropping the gate is one visible change per agent, not a code change. A parity test runs the old table and the new resolver over every (role × interface × door) cell of `tests/test_access_matrix.py` and requires the same answer.

The caller envelope keeps `relationship` for agents already reading it and gains `role` and `source`; the old values map owner→owner, admin→admin, member→editor/user/viewer by the winning line, contact→viewer.

## An agent's own login (who may create it)

An agent's admin may create the agent's canopy login without being a workspace admin, when the address is at a domain canopy itself owns for agents. That is what makes it safe: the login is a brand-new, password-less account that no person can sign in to, so it cannot become a way to put a human's address into a workspace. Still never adopts an existing account; still one login to one agent; recorded in the settings audit.

## Build order

1. This spec, `roles.py` (the ladder and permission table), the `AgentGrant` table, the resolver. Nothing reads it yet.
2. Translate every agent's access into lines (data migration); `decide` reads lines; parity test.
3. Doors carry `role`; the standard `ask` template; the interface's `full:`, `callers`, `callers_default` and `editors:` are read for compatibility, then removed.
4. The access list in the UI (replacing the Callers tab and the roster), `who_is_asking` and the envelope carry `role` and `source`, so an agent can say why someone is a viewer and who can raise them.
5. The agent-login gate.
6. Remove the old paths and fold `AgentAdmin` into lines.
