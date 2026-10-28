# Analytics: the fact log, turn usage, and the health/analytics dashboard

## Rule

**Anything worth counting is emitted as a fact through `apps.events.activity.emit`
(or `activity.for_turn`), and analytics reads only `Activity` and `SessionUsage`.**
Nothing else creates an `Activity` row (`test_activity_has_one_writer`). A new
metric means: add a kind to `activity.KINDS`, emit it where the thing happens, read
it in `apps/events/analytics.py`.

## The pieces

| Piece | Where | What |
|---|---|---|
| `Activity` | `apps/events/models.py` | Append-only fact: workspace (NOT NULL), `at`, dotted `kind`, `actor_kind` (user/agent/contact/runner/system), `actor`, `agent` + `agent_slug`, `surface`, `ref`, small `meta`. Indexed (workspace, at), (workspace, kind, at), (actor, at), (kind, ref). Never holds content; retention and `prune_events` do not touch it. |
| `emit` / `for_turn` | `apps/events/activity.py` | The one writer. Own savepoint; a failure is logged and swallowed, never undoes the action. A fact with no workspace is dropped. `for_turn` derives tenancy (`Turn.tenant_slug`), agent (`acting_agent`), person (`initiator_user`, else `enqueued_by`) and surface (origin) once. |
| `SessionUsage` | `apps/events/models.py`, `apps/events/usage.py` | THE token record: per Claude Code session, per UTC day, as its box reports it. Each box scans its own transcripts in the repos canopy names for it (`GET /runners/{id}/usage-scope`: its declared projects and the agents it serves) with `canopy_transcript.send_usage_report` — both runners — and posts totals, never content (`POST /runners/{id}/usage`, every ~10 min, only changed files). Matched on write to canopy's record of that Claude session (a binding's `transcript_id`, a turn's `session_key`/`cli_session_id`) and attributed from it: workspace, agent or repository, person. No cost. |
| `Event` | `apps/events/models.py` | The OPERATIONAL log (faults, actions). It coalesces repeats into a count, so it is never counted from. |

## Kinds and producers

- Every turn fact's `meta` also names, when it has them, its `thread` (`session:<uuid>`, or `mail:<uuid>` for an email turn whose session could not be made), the `contact` who asked and the agent that asked (`by_agent`: `initiator_agent`, else the agent whose own login the actor is) — `activity.who_and_where`. Ids only, never content.
- `turn.finished` — `harness.closing.close_turn`, the one terminal transition every path goes through: a runner's finish (`finish_turn`), an expired lease (`claim.sweep_expired_leases`, lost/cancelled), a queued turn stopped (`cancel_turn`), a rider ending with the turn it rode, an admission rule refusing a queued turn (`admission.evaluate.reject`, cancelled), and a stranded turn Slack moves off a dead box (lost). A turn written already cancelled at enqueue (a blocked sender, an offered-nothing caller) makes no transition, so only `backfill_activity` writes its `turn.finished`. `meta.status` (`failed` and `lost` count as failures; `cancelled` is a stop, never one), and `meta.runner`/`meta.runner_kind` — the box that ran it, by name (Health's "Ran on").
- `turn.enqueued` and `chat.session.created` come from the rows themselves (`harness/signals.py`, the post-save creation receiver beside provenance's), so every creation path — raw `Session.objects.create`s, the runner's report sweep, Slack, a refused email turn — counts.
- `chat.message` — `canopy_sessions.send.send_message` (both paths), `ref` = the turn.
- `setting.changed` — `events.audit.audited`, alongside the `SettingChange` row (`ref` `sc<pk>`).
- `mcp.call` — `mcp.audit`, alongside `MCPAuditLog` when it has a workspace (`ref` `mcp<pk>`).
- `runner.invited`, `runner.invitation.approved`, `runner.paired` — a new cloud box's pairing invitation made, approved and used (`apps/harness/api_runner_invitations.py`; `ref` the invitation, or the new runner for `runner.paired`).
- `runner.pairing.approved` — a person approved a laptop's browser pairing (`apps/harness/api_runner_pairings.py`, `ref` the pairing); its claim emits `runner.paired` with the new runner as `ref`.

Active people = distinct `actor` with `actor_kind=user` that day. An agent acting with
its own login is `agent`, not a person.

## Dimensions

Every fact and usage row names an agent (`agent_slug`) or, for work no agent ran,
a repository (`project`, `activity.repo_of`: a repo turn's project, or the project
of a session no agent runs — a person's own Claude Code session in a repo). The
analytics tables are one aggregation (`analytics._breakdown`) over agents,
repositories and people.

## Network

`analytics.network` (`GET /api/workspaces/{slug}/network`, page `/w/:ws/network`, a tab of
Health) draws who works with whom from `turn.enqueued` facts alone: each thread links to
whoever wrote in it — a person (`actor_kind=user`, even when they wrote by email), else
the contact, else the asking agent — and to the agent that answered it. A turn in no
thread (a schedule, a bare API dispatch) is not drawn. Totals include the pairs of people
or contacts who wrote in the same thread. At most `MAX_THREADS` threads, busiest first
(most writers, then most turns). Labels are names; a chat's title is shown, and the thread
opens, only where `canopy_sessions.access.visible_session_q` lets the viewer read it. Each
node carries its workspace, so the page can colour or cluster by workspace.

A contact canopy can name is drawn as who it is (`analytics._contacts_who_are_known`): an
agent when its address is an agent's (its `email`, its login's, or an inbound mailbox), else
a person when the contact is linked to an account or its address is exactly one account's
verified email. Writing to an agent of a workspace you are not a member of (a member of the
parent asking a child's agent, someone who joined after they wrote, an agent mailing one
outside its workspace) is recorded as a contact, which is right for access, but it is still
that person or agent. The side panel is computed from
the graph the page holds (`networkGraph.profile`): for a person or contact, who wrote in
the same threads, which agents and channels, and their threads; for an agent, who uses it;
for a thread, its writers. Each thread carries `last_at`, its latest turn in the window.

## Only sessions canopy has

A reported session that matches nothing canopy holds is dropped, not kept: canopy
counts the sessions a person's machine sent it and never reaches down for the
others. A matched session's usage row always has a workspace.

## Shipping a change to the shared transcript core

`packages/canopy_transcript` (and `canopy_cron`, `runner/canopy_acp`) run on the
boxes. They are in the runner code-sha paths — `canopy_transcript` and
`canopy_cron` in the laptop runner's (`provenance.CODE_PATHS`, the installers,
`deploy-labs.yml`), `canopy_transcript` and `runner/canopy_acp` in the cloud
runner's (`update_runner.sh`, `up.sh`, `deploy-labs.yml`) — so a change marks
every box stale and the auto-updater ships it after the next deploy.

## History

`manage.py backfill_activity [--days 365] [--no-usage]` seeds facts from turns,
sessions, setting changes and MCP calls, and usage from transcripts retention has not
purged yet. Idempotent on (kind, ref) — the same refs the live producers write — so it
is safe to run after deploy and again later.

## Health

`apps/agents/health.py` composes existing answers — never a second opinion: each
agent's steps are `setup.setup_steps`, its vault state is
`vault_health.vault_health` (also what the checklist's vault step reads). Tree walk:
`wsvc.subtree` + `agents.tree.agents_in_tree` (shared with both topology views).

It asks every (runner, agent) pair which machines may open each vault, so it reads
the fleet once: `device_keys.serving_facts(agents)` (a `routing.ServingFacts`) is
passed down through `vault_health`, `setup_steps` and `keys_for_agent`, and the
same rule (`runner_serves_agent`, `runner_routes_agent`) answers each pair from
it; the viewer's admin standing is one `access.Standings`. More runners cost no
more queries (`test_health_reads_the_fleet_once_…`); each agent still costs a
fixed few dozen for its own checklist steps.

Because of that per-agent cost the page never waits for every checklist: it reads
`/health?setup=false` (the table, with `steps`, `vault`, `left` and the totals
counted from them null) and shows it, then `/health/setup?agents=…`
(`health.setup_of`, at most 25 slugs, only agents of the tree) three agents per
read, four reads in flight (`pages/health/useAgentSetup.ts`), each row filling
as its read lands; the tiles counted from checklists show once all are in. A plain
`/health` still answers everything in one read, for MCP callers.
