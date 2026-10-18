# Analytics: the fact log, turn usage, and the health/analytics dashboard

## Rule

**Anything worth counting is emitted as a fact through `apps.events.activity.emit`
(or `activity.for_turn`), and analytics reads only `Activity` and `TurnUsage`.**
Nothing else creates an `Activity` row (`test_activity_has_one_writer`). A new
metric means: add a kind to `activity.KINDS`, emit it where the thing happens, read
it in `apps/events/analytics.py`.

## The pieces

| Piece | Where | What |
|---|---|---|
| `Activity` | `apps/events/models.py` | Append-only fact: workspace (NOT NULL), `at`, dotted `kind`, `actor_kind` (user/agent/contact/runner/system), `actor`, `agent` + `agent_slug`, `surface`, `ref`, small `meta`. Indexed (workspace, at), (workspace, kind, at), (actor, at), (kind, ref). Never holds content; retention and `prune_events` do not touch it. |
| `emit` / `for_turn` | `apps/events/activity.py` | The one writer. Own savepoint; a failure is logged and swallowed, never undoes the action. A fact with no workspace is dropped. `for_turn` derives tenancy (`Turn.tenant_slug`), agent (`acting_agent`), person (`initiator_user`, else `enqueued_by`) and surface (origin) once. |
| `TurnUsage` | `apps/events/models.py`, `apps/events/usage.py` | Tokens per turn, or per typed message's answer, from whichever record of its model calls exists: a `claude -p` transcript's `{"type":"result"}` records (`runs > 0`, never replaced by a per-message sum); an interactive transcript's per-message `message.usage`, once per message id; or a session's rows, which carry the same usage (and the record's timestamp) on each assistant record's first row (`canopy_transcript.record_usage`) and are credited to the person's message they answer (`usage.record_for_chat`; its turn when it has one, else `chat:<session>:<index>`). Dated by the model call itself, so recovered history counts on its own day. Carries `agent_slug` or `project` and the person. **No cost**: `claude -p`'s `total_cost_usd` is a list-price estimate that does not describe a subscription. |
| `Event` | `apps/events/models.py` | The OPERATIONAL log (faults, actions). It coalesces repeats into a count, so it is never counted from. |

## Kinds and producers

- `turn.finished` — `harness.services.finish_turn` and `harness.claim.sweep_expired_leases` (lost/cancelled); `meta.status`.
- `turn.enqueued` and `chat.session.created` come from the rows themselves (`harness/signals.py`, the post-save creation receiver beside provenance's), so every creation path — raw `Session.objects.create`s, the runner's report sweep, Slack, a refused email turn — counts.
- `chat.message` — `canopy_sessions.services.send_message` (both paths), `ref` = the turn.
- `setting.changed` — `events.audit.audited`, alongside the `SettingChange` row (`ref` `sc<pk>`).
- `mcp.call` — `mcp.audit`, alongside `MCPAuditLog` when it has a workspace (`ref` `mcp<pk>`).

Active people = distinct `actor` with `actor_kind=user` that day. An agent acting with
its own login is `agent`, not a person.

## Dimensions

Every fact and usage row names an agent (`agent_slug`) or, for work no agent ran,
a repository (`project`, `activity.repo_of`: a repo turn's project, or the project
of a session no agent runs — a person's own Claude Code session in a repo). The
analytics tables are one aggregation (`analytics._breakdown`) over agents,
repositories and people.

## Recovering token history

Sessions streamed before runners sent usage hold the rows but not the tokens.
`manage.py request_usage_backfill [--days 90]` asks each bound session's box to
re-ship its transcript (the ordinary backfill both runners drain);
`persist_transcript_rows` adds the usage to rows it already holds (`_fill_usage`)
and credits it. Safe to repeat. Needs the boxes on the runner code that sends
usage (Health → Runners → Code: current).

## Shipping a change to the shared transcript core

`packages/canopy_transcript` (and `canopy_cron`, `runner/canopy_acp`) run on the
boxes. They are in the runner code-sha paths (`provenance.CODE_PATHS`, the
installers, `update_runner.sh`, `up.sh`, `deploy-labs.yml`), so a change marks
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
