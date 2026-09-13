# Agent instances and the ACL — you own the instance, not the agent

**Date:** 2026-09-12
**Status:** Design — not implemented. Phase 0 is independently shippable.
**Supersedes in part:** `2026-06-30-workspace-multi-tenancy-design.md` (the `Agent.workspace`
FK it introduced becomes an instance field; the tenancy *rules* it established survive)
**Related:** `2026-09-05-agent-credentials-design.md` (per-agent secrets become per-INSTANCE
secrets — a second tenant's `echo` needs its own mailbox, not a share of the first's)

## Problem

Three complaints that turn out to be one.

> *"We need to clean up the ACL."* — Jonathan, 2026-09-12
>
> *"Agents will have instances in tenants or users, so you don't own the agent, you own the
> instance of the agent."* — Jonathan, same conversation

**1. Authorization has four different notions of "may act on this agent."**

| Mechanism | Where |
|---|---|
| `Agent.owner` — a User FK | `apps/agents/models.py:23` |
| `Agent.workspace` + `WorkspaceMembership` | `apps/agents/models.py:31`, every tenancy predicate |
| `Runner.paired_by` → `user_workspace_slugs` | `apps/harness/services.py:444` |
| The `{OWNER, EDITOR}` role check on delete | `apps/agents/api.py:179` |

Four answers to one question is not an access-control model; it is four access-control
models that happen to agree today.

**2. The role named `viewer` is not read-only.** Measured, not assumed: `_require_role`
appears in exactly one file (`apps/workspaces/api.py`) and every use demands `OWNER`. The
*only* place in the codebase where `EDITOR` is distinguished from `VIEWER` is agent deletion
(`apps/agents/api.py:179`). A workspace `viewer` can therefore upsert agents, create
projects, send chats and publish demos. Anyone inviting a colleague as a "viewer" expecting
read-only is wrong, and the name actively misleads them. This matters now rather than later
because the point of the current work is handing workspaces to people who are not Jonathan.

**3. `Agent` is simultaneously a definition and an instance**, which is why (1) is a mess.
One row carries both `owner` (a person) and `workspace` (a tenant, NOT NULL). That shape
cannot express "echo runs in two tenants": there is one workspace column. So ownership has
two answers because the row is two things.

## The decision

> *"An agent's definition is shared across tenants, so they all update when echo updates.
> You could of course fork echo, but then that would be a different agent."* — Jonathan

**Definitions are shared and singular. Instances are owned.**

- **`Agent`** — the definition: slug, display name, persona repo, skill catalog. **Not
  tenant-scoped.** One `echo`. Improving echo improves it everywhere.
- **`AgentInstance`** — one tenant's (or one user's) running copy: its board, its items, its
  turns, its credentials, its runner assignments, its schedules, its mailbox. **This is the
  thing you own**, and the only thing an ACL needs to reason about.
- **Forking is not a mode.** A fork produces a new `Agent` with its own slug. There is no
  "detached instance" state to represent, and no merge story to design.

The payoff is the reason the split is worth its cost: a fix to echo's `turn` skill reaches
every tenant running echo, without anyone copying files between repos. That is the whole
thesis of the agent operating model, and today's schema cannot express it.

## The role model

Four roles, in a ladder — each contains the one below.

| Role | Enforced as | Can |
|---|---|---|
| **Viewer** | *no account* — a shared link | Read what was shared with them: a storyboard, a narrative, a walkthrough, a session transcript. Genuinely read-only, and genuinely enforced today. |
| **User** | workspace member, `viewer` | Interact with an agent instance: chat, answer a blocked dialog, triage an item, read the board. **No writes to the instance's shape.** |
| **Author / executor** | workspace member, `editor` | Everything above, plus: create and edit agent instances, run turns, publish work products, edit schedules, manage runner assignments. |
| **Administrator** | workspace member, `owner` | Everything above, plus: members and invites, the shared vault, inbound configuration, instance credentials, deleting an instance or the workspace. |

Two things this table asserts that are **not true today** and are the work:

- The `viewer`/`user` tier is currently indistinguishable from `editor` everywhere except
  agent deletion.
- "Author" operations are currently available to any member.

One thing it asserts that **is** true today and must survive: the anonymous link tier is
properly enforced, by `PUBLIC_PATH_PREFIXES` plus each public endpoint's own token check.

## The risk, stated plainly

**60 call sites derive tenancy from `agent.workspace`.** Every one must re-derive it through
the instance, and the historical failure mode is documented in this codebase's own comments
(`apps/agent_runs/api.py:86`):

> *"Fails CLOSED on an unhomed agent (`agent.workspace_id IS NULL`): the old
> `if agent.workspace_id and not wsvc.is_member(...)` short-circuited to…"*

`Agent.workspace` was made NOT NULL precisely because, while nullable, **six separate
tenancy predicates independently grew a `workspace_id IS NULL` leg meaning *allow***. This
migration reintroduces exactly that surface, sixty times over, because during the transition
an `Agent` legitimately has no workspace.

**So the controlling rule for the whole migration: no predicate may ever treat "no tenant
found" as permission.** Every site fails closed or does not ship. The mechanism is the one
this repo already uses — `tests/test_claim_schedule_parity.py` exists so that a divergence
between two authorization paths "fails CI instead of production," and the same discipline
applies here: a test that asserts the instance-derived tenant set equals the agent-derived
one, kept green through Phases 1–2 and deleted only in Phase 3.

## What hangs off which

Sixteen FKs point at `Agent`. Each needs a home, and the question is always: *would a second
tenant running echo want its own?*

| Model | Home | Why |
|---|---|---|
| `AgentSkill` | **Definition** | Echo's skills *are* echo. A shared definition that did not share its catalog would not be shared at all. |
| `AgentCredential` | **Instance** | The clearest case: a second tenant's echo needs its own mailbox token, not a share of the first's. See the credentials spec. |
| `AgentTask`, `AgentTaskCommand` | **Instance** | A board is work in a tenant, not a property of the agent. |
| `AgentWorkProduct` | **Instance** | Output belongs to whoever's work produced it. |
| `AgentSync` | **Instance** | A sync is a conversation with a particular stakeholder. |
| `AgentBootstrapReport` | **Instance** | Provisioning is per-deployment by definition. |
| `harness` FKs (turns, runner assignments) | **Instance** | Routing is "which box runs *this tenant's* echo" — already the question `RunnerAssignment` answers. |
| `agent_runs` FK | **Instance** | A run happened somewhere. |

`AgentSkill` being the lone definition-level model is a useful sanity check on the split: if
more than one or two models want to live on the definition, the boundary is drawn wrong.

## Phasing

**Phase 0 — make `viewer` mean something. Independent; ships without any of the below.**
Introduce the author/executor boundary: writes to an instance's *shape* (create, edit,
delete, schedule, runner assignment) require `editor`; interaction (chat, item decisions,
reading) stays open to `viewer`. This is a pass over the write endpoints adding one gate,
plus a test per endpoint asserting a `viewer` is refused. It closes the live mismatch
between the role's name and its power, and it is the piece the current rollout actually
needs. **Do this first and separately** — it needs no model change and no migration.

**Phase 1 — `AgentInstance` lands additively.** New model, `Agent.workspace` stays and stays
NOT NULL. Every existing agent gets exactly one instance by backfill. Nothing reads the
instance for authorization yet. Reversible by dropping a table.

**Phase 2 — move tenancy derivation, site by site.** Each of the 60 sites moves from
`agent.workspace` to its instance, behind the parity test above. Order by blast radius:
read-only projections first, claim/routing last.

**Phase 3 — drop `Agent.workspace`, delete the parity test.** Only once Phase 2 is complete
and the parity test has been green across a real deploy cycle.

## Out of scope

- **Cross-tenant instance sharing.** One instance belongs to one tenant-or-user. Two tenants
  wanting the same board is not a thing.
- **Per-instance definition overrides.** If a tenant needs different behaviour, they fork —
  which by the decision above produces a different agent. Resisting this is what keeps the
  shared-definition promise meaningful.
- **A migration path for agents that should become two agents.** If `echo` in one tenant has
  diverged in practice, that is a fork, done by hand, once.
- **Changing the anonymous tier.** It works and is enforced; this spec does not touch it.
- **The `Agent.owner` FK's fate.** It probably becomes `AgentInstance.owner`, but whether a
  personal instance needs a user FK *and* a workspace FK is a Phase 1 question, not a
  Phase 0 blocker.
