# Agent roles and one access list

**Status:** built 2026-10-30. Jonathan, after an audit showed the same colleague reaching five different amounts of five agents, depending on which workspace the agent happened to live in and which domains its interface had copy-pasted: "we start to use groups for permissions instead of individuals", one role vocabulary, and the interface should stop being a place where nobody understands who gets what.

## The problem

Access to an agent was decided by four stacked things that each used the word "member" differently:

1. the person's **workspace role** (and the rule that only a parent's owners and admins reach into child workspaces);
2. an implicit rule that a workspace **editor** is an **agent editor** ("full profile, every turn manual");
3. the agent's published **interface**: `full:` rules, per-capability `callers`, `callers_default`, `editors: confined`;
4. which **domains** each agent's author remembered to list.

`member` meant "a member of the agent's own workspace", so an editor, a viewer and a workspace admin were all "members", and a Dimagi editor who was not in the agent's workspace was a "contact". Eight of fifteen agents publish no interface and are reachable by every editor of their workspace; four hand-wrote the same `ask` capability with the agent's name swapped in.

## Vocabulary (one meaning each)

**Role** — what a person may do with ONE agent. One ladder, strongest first:

| Role | May |
|---|---|
| **owner** | Everything an admin may, plus transfer or delete the agent and name its admins. |
| **admin** | Configure the agent in canopy-web: its access list, capabilities, credentials, routing, runners, integrations. |
| **editor** | Steer the whole agent (every skill it has, in its own authority) and change the agent itself (its skills and capabilities). |
| **user** | Use the agent as designed: use any capability it publishes for users, including ones that act. |
| **viewer** | Ask. The agent's `ask` capability and nothing else. |

`system` is not a person: canopy itself, the agent's own login, an approved task's dispatch.

**Permission** — what a role is made of. `read` (use a viewer capability) · `write` (use a user capability) · `run` (steer the whole agent) · `edit` (change the agent's skills and capabilities) · `admin` (configure it) · `own`. A role is a bundle of these; `roles.py` holds the table, and nothing else in the code names a role.

**Source** — why someone has a role: named, a workspace group, a contact group, or implicit. A **role** is what you can do; a **source** is why. They are never the same word.

**Access list** — the one place that says who has which role on an agent. A **line** is `(who, role)`. `who` is one of:

* a **person**;
* **everyone in a workspace** (optionally "at least editor" there, optionally only those whose address is at a domain). Reads the person's effective role in that workspace, so a parent's owners and admins count as they do everywhere else;
* **anyone at a domain**, member or not, "verified" by default: the Dimagi-wide line. A signed-in request, an aligned email and a Slack full-member all count as verified;
* **a known contact** (an emailer, a Slack member outside the workspace, a widget visitor who may have no address), optionally at a domain and/or verified. The line that lets a site's visitors through to a capability.

The highest role among a person's matching lines wins. Implicit lines, shown in the list but not editable there: the agent's owner → owner, the agent's workspace owners → admin, explicit admins → admin.

**Workspace defaults** — lines set once on a workspace that every agent in it (and below) inherits unless that agent sets `inherit_workspace_defaults: false`. This is the "dimagi-wide" / "workspace-wide" mechanism: one line on `dimagi` reaches every agent there. A new agent inherits its workspace's defaults and nothing else.

**Capability** — a named profile an agent publishes for viewers and users: which tools, paths and sites a confined session may touch (`ask`, ACE's `connect`, labsdata's `data`). A capability says only the **lowest role** that may open it (`viewer` or `user`). It never says who: that is the access list's job. The standard `ask` profile is a platform template an agent turns on with one line.

**Contact** is not a role. It describes how canopy knows someone (not a workspace member); a contact who matches a contacts line is a viewer or user by that line, and with no line is refused.

**Admin is never bare.** There are six unrelated admins (agent admin, workspace admin, runner admin, vault key holder, Google Workspace admin, GitHub org owner). This spec unifies the first two only; every message that names one names which.

## The decision

`access.decide` keeps its signature and stays the one function every entry point asks. It resolves the role from the lines, then:

| Role | access | mode |
|---|---|---|
| owner · admin · system | `full` | the routing ladder; may ask for `auto` |
| editor | `full` | the agent's own mode, unless the winning line says `manual_only` |
| user | `confined`, to the capability asked for (else `ask`), if that capability's lowest role is `user` or `viewer` | the routing ladder |
| viewer | `confined`, to a capability whose lowest role is `viewer` | the routing ladder |
| none | refused, with what the asker CAN do and who can raise them | — |

The sensitive-data rule stays over the top, unchanged.

## What was done to the old rules

The old rules were translated, then removed:

* migration `agents/0112` moved every agent's rules onto its list: the implicit "workspace editor → editor tier, manual" became a line `everyone in <ws> at least editor → editor, manual_only` (unless the interface said `editors: confined`); each `full:` rule became a line (`member` → everyone in the agent's workspace; `contact@d:verified` → a verified contact at d); each capability's `callers` became lines and the capability kept only `role`. It stripped `full:`, `editors:`, `callers_default` and `callers` from the stored interface and kept every agent's old interface and source in the settings audit (`SettingChange`, operation `access_list_migration`);
* the parser refuses the retired keys with a pointer to the access list, and `Agent.uses_access_list` and `convert_access` are deleted;
* `AgentAdmin` and `Agent.owner` deliberately stay the stores of admin and owner and appear as implicit lines on the list, so there was nothing to fold;
* `manual_only` stays a flag on the line, so dropping the gate is one visible change per agent, not a code change;
* the caller envelope keeps `relationship` (owner / admin / system, and member / contact for everyone else) for agents already reading it, and gains `role` and `source`.

## An agent's own login (who may create it)

An agent's admin may create the agent's canopy login without being a workspace admin, when the address is at a domain canopy itself owns for agents. That is what makes it safe: the login is a brand-new, password-less account that no person can sign in to, so it cannot become a way to put a human's address into a workspace. Still never adopts an existing account; still one login to one agent; recorded in the settings audit.

## Build order (done)

1. This spec, `roles.py` (the ladder and permission table), the `AgentGrant` table, the resolver.
2. Every agent's access translated into lines (data migration); `decide` reads lines; `convert_access` checked that every real interface answered as before.
3. Capabilities carry `role`; the interface's `full:`, `callers`, `callers_default` and `editors:` removed (migration `agents/0112`).
4. The access list in the UI (replacing the Callers tab), `who_is_asking` and the envelope carry `role` and `source`.
5. The agent-login gate.
6. The old paths removed (`uses_access_list`, `convert_access`, the caller classes). `AgentAdmin` and `Agent.owner` stay as the stores of admin and owner.

The reference for the result is `docs/architecture/access.md`.
