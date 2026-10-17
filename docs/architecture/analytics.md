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
| `TurnUsage` | `apps/events/models.py`, `apps/events/usage.py` | Tokens + cost per turn, summed from every `{"type":"result"}` record in the turn's uploaded `claude -p` transcript. Derived on commit of a transcript batch carrying a result (`ledger.append_transcript`) — no runner change. Keeps its own workspace/agent/person so it outlives the turn and the transcript. A turn with no uploaded transcript has no row: "not reported", never zero (`totals.turns_with_usage`). |
| `Event` | `apps/events/models.py` | The OPERATIONAL log (faults, actions). It coalesces repeats into a count, so it is never counted from. |

## Kinds and producers

- `turn.enqueued` — `harness.services.enqueue_turn` (new rows only).
- `turn.finished` — `harness.services.finish_turn` and `harness.claim.sweep_expired_leases` (lost/cancelled); `meta.status`.
- `chat.session.created` — `canopy_sessions.services` session create.
- `chat.message` — `canopy_sessions.services.send_message` (both paths), `ref` = the turn.
- `setting.changed` — `events.audit.audited`, alongside the `SettingChange` row (`ref` `sc<pk>`).
- `mcp.call` — `mcp.audit`, alongside `MCPAuditLog` when it has a workspace (`ref` `mcp<pk>`).

Active people = distinct `actor` with `actor_kind=user` that day. An agent acting with
its own login is `agent`, not a person.

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
