# Several canopy tenants on one Slack workspace

**Status:** approved 2026-09-22, building.

## Why

A Slack workspace could be connected to exactly one canopy workspace
(`SlackInstallation.team_id` unique, with a `workspace` FK). Dimagi staff live in
one Slack and support several canopy tenants, so every tenant after the first
could not reach its agents from Slack at all. It also broke sharing a session to
Slack: a repo session on a laptop whose runner is registered in `dimagi` could
not post, because the Dimagi Slack was connected to `connect`.

## Model

- `SlackInstallation` becomes **Slack-wide**. It keeps `team_id` (still unique)
  plus everything that belongs to the one Slack app install: bot token and user,
  `app_id`, the configuration token, command-sync state and `agent_declared_at`.
  The `workspace` FK is **dropped**.
- New `SlackWorkspaceLink(installation, workspace, linked_by, linked_at)`, with
  `workspace` **unique**: a canopy workspace has at most one Slack; a Slack may
  serve many workspaces.
- Migration: one link per existing installation, to its current workspace, then
  the column goes. `connect` is unchanged in behaviour.
- `SlackUserLink` stays per installation: it records *who a Slack user is*, which
  is a fact about the Slack, not about any tenant. It grants nothing. Membership
  is checked per message against the tenant the message is about.

## Connecting

OAuth install from `/w/<ws>/settings/slack` by an **owner of that workspace**,
exactly as before, plus Slack's own install consent for that Slack (decided
2026-09-22: no sign-off from tenants already linked). The callback:

- creates the installation, or refreshes the shared one's bot token if the Slack
  is already connected;
- links this workspace to it. A workspace already linked to a *different* Slack
  is re-pointed, since its owner is the one asking.

The old rule "re-pointing a Slack at another workspace needs ownership of both"
is gone. Linking no longer moves anyone's messages: it adds a tenant beside the
ones already there and gives it nothing of theirs.

**Accepted risk:** any canopy workspace owner who can complete Slack's install for
a Slack can make the bot answer there as their agents. The bot posts with each
agent's name and avatar. That is the consent model chosen; Slack's own
app-install policy for the Slack is the other gate.

## Inbound routing: the tenant comes from what a message is about

No inbound path reads a tenant off the installation any more.

| message | tenant |
|---|---|
| in a thread canopy already has a session for | that session's workspace |
| names an agent (`hal …`, `/hal`) | the agent's workspace (slugs are globally unique) |
| names nothing | the only Slack-enabled agent across **all linked** tenants, else the list |
| `/canopy cloud` | the caller's own queued turns, in linked tenants they are a member of |
| a button click | the session the button names (re-checked against linked tenants + team + channel) |

Everything downstream follows that tenant: the member-or-contact decision, the
`Contact` row, the session. A Slack user who is a member of tenant A but not of B
is answered as a **contact** by B's agents, the same rule as any non-member.

Session lookups by thread key are restricted to linked tenants
(`workspace_id__in=installation.workspace_ids()`), so an unlinked tenant's
leftover rows are unreachable.

**Event log.** `Event.workspace` is NOT NULL on purpose. A refusal is recorded
against the tenant the message resolved to. When it resolved to none (no agent
named and several to choose from, or a blocked sender before any agent), it is
recorded against the installation's **home** tenant, the earliest link. Such a
row carries only a Slack user id, a channel and a generic refusal line.

## Slash commands and the Slack app

Commands are a property of the one Slack app, so `reconcile` works across the
agents of **every linked tenant**, for both what to add and what counts as
canopy's to remove. Otherwise a sync triggered from tenant B would delete A's
`/hal`. The configuration token and "declare the app an agent" stay on the shared
row. An owner of any linked tenant may set, clear or use them, as the settings
page already allows for its own tenant.

## Sharing a session

`share._installation` finds the Slack through the session's workspace's link;
with no session, through the caller's own linked workspaces. Bound-session
lookups (`bound_repo_session`) are restricted to linked tenants.

## Out of scope

Disconnecting a tenant (there is no disconnect today), one tenant on several
Slacks, per-tenant command namespaces, and showing a tenant which others share
its Slack.

## Tests

- a second tenant connects to an already-connected Slack: a link is added, the
  first tenant's link is untouched, and the bot token is refreshed;
- a named agent in tenant B answers with a B session; the sender is a member of A
  only and is answered as a **contact recorded in B**;
- a thread whose session is in A continues in A even when B also has agents;
- with agents in both tenants and none named, the list names both;
- reconcile triggered from B keeps A's commands and adds B's;
- share from a session in a newly linked tenant posts through the shared bot;
- the migration: an existing installation keeps working with one link.
