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

- Every turn fact's `meta` also names, when it has them, its `thread` (`session:<uuid>`, or `mail:<uuid>` for an email turn whose session could not be made), the `contact` who asked and the agent that asked (`by_agent`: `initiator_agent`, else the agent whose own login the actor is) — `activity.who_and_where`. Ids only, never content. Facts written before this were filled from their turns (`events/0007`).
- `turn.finished` — `harness.services.finish_turn` and `harness.claim.sweep_expired_leases` (lost/cancelled); `meta.status`, and `meta.runner`/`meta.runner_kind` — the box that ran it, by name (Health's "Ran on").
- `turn.enqueued` and `chat.session.created` come from the rows themselves (`harness/signals.py`, the post-save creation receiver beside provenance's), so every creation path — raw `Session.objects.create`s, the runner's report sweep, Slack, a refused email turn — counts.
- `chat.message` — `canopy_sessions.services.send_message` (both paths), `ref` = the turn.
- `setting.changed` — `events.audit.audited`, alongside the `SettingChange` row (`ref` `sc<pk>`).
- `mcp.call` — `mcp.audit`, alongside `MCPAuditLog` when it has a workspace (`ref` `mcp<pk>`).
- `runner.invited`, `runner.invitation.approved`, `runner.paired` — a new cloud box's pairing invitation made, approved and used (`apps/harness/api_runner_invitations.py`; `ref` the invitation, or the new runner for `runner.paired`).

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

## Missing sessions

A reported session that matches nothing has no workspace: it counts nowhere, and
only the box's admins see it (Health → Runners → Missing sessions,
`GET /runners/{id}/missing-sessions`). **Check for missing sessions** asks the box
for a full rescan (`usage_scan_requested_at`, read off `usage-scope`); **Send up**
files the chosen ones where the report sweep files that box's sessions
(`harness.services.session_home`) with a binding keyed `claude:<uuid>`
(`canopy_transcript.transcript_for_key`, both runners) and requests the ordinary
backfill; their usage is matched at once.

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
`vault_health.vault_health` (also what the checklist's vault step reads), each
workspace's shared vault is `Workspace.shared_vault_source`. Tree walk:
`wsvc.subtree` + `agents.tree.agents_in_tree` (shared with both topology views).
