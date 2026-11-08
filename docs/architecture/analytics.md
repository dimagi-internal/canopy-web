# Analytics: the fact log, turn usage, and the health/analytics dashboard

## Rule

**Anything worth counting is emitted as a fact through `apps.events.activity.emit`
(or `activity.for_turn`), and analytics reads only `Activity` and `SessionUsage`.**
Nothing else creates an `Activity` row (`apps/workspaces/tests/test_workspace_health_and_analytics.py::test_activity_has_one_writer`). A new
metric means: add a kind to `activity.KINDS`, emit it where the thing happens, read
it in `apps/events/analytics.py`.

## The pieces

- `Activity` (`apps/events/models.py`): an append-only fact with the workspace
  (NOT NULL), who (`actor_kind`: user / agent / contact / runner / system), what
  (`kind`), where (`agent`, `surface`, `ref`) and a small `meta`. It never holds
  content; retention and `prune_events` do not touch it.
- `emit` / `for_turn` (`apps/events/activity.py`): the one writer. A failure is logged
  and swallowed, never undoes the action, and a fact with no workspace is dropped.
- `SessionUsage` (`apps/events/models.py`, `apps/harness/usage.py`): THE token record.
  A session canopy has no record of is dropped, not kept: canopy counts only the
  sessions a person's machine sent it and never reaches down for the others. No cost.
- `Event` (`apps/events/models.py`): the OPERATIONAL log (faults, actions). It
  coalesces repeats into a count, so it is never counted from.

## Gotchas in the producers

The kinds are the constants in `activity.py` (with a comment on each); read the
producer by grepping its constant. What the code does not make obvious:

- Every turn fact's `meta` names, when it has them, its `thread` (`session:<uuid>`, or
  `mail:<uuid>` for an email turn whose session could not be made), the `contact` who
  asked and the agent that asked (`by_agent`) — `activity.who_and_where`. Ids only,
  never content.
- `turn.finished` is written by `harness.turn_close.close_turn`, the one terminal
  transition every path goes through. A turn written already cancelled at enqueue
  makes no transition, so only `backfill_activity` writes its `turn.finished`.
  `meta.status`: `failed` and `lost` count as failures; `cancelled` is a stop, never
  one.
- `turn.enqueued` and `chat.session.created` come from the rows themselves
  (`harness/signals.py`, post-save receivers), so every creation path counts.
- `coaching.*` facts come from a coach's own scripts through `POST /api/coaching/facts/`
  (`apps/canopy_sessions/api_coaching.py`, whose docstring is the rule): only from the
  per-turn login of a running, unconfined turn of the agent (`turn_asker.serving_turn`),
  with a closed kind, closed words and ids and no field that can hold text; a person
  must hold a live review grant this turn may use (`access.review_grantors`);
  `finding_routed` refuses `person_id`, because a friction pattern has no one in it.
  `actor` is the agent's login, so the person is in `meta` and never counts as
  active. Facts are deduped on `(kind, ref)`. Nothing reads them yet (`TODOS.md`).
- Active people = distinct `actor` with `actor_kind=user` that day. An agent acting with
  its own login is `agent`, not a person (`activity._actor_kind`).

## Dimensions

Every fact and usage row names an agent (`agent_slug`) or, for work no agent ran,
a repository (`project`, `activity.repo_of`). The analytics tables are one
aggregation (`analytics._breakdown`) over agents, repositories and people.

## Network

`analytics.network` (`GET /api/workspaces/{slug}/network`, page `/w/:ws/network`, a tab of
Health) draws who works with whom from `turn.enqueued` facts alone: each thread links to
whoever wrote in it — a person (`actor_kind=user`, even when they wrote by email), else
the contact, else the asking agent — and to the agent that answered it. A turn in no
thread (a schedule, a bare API dispatch) is not drawn. At most `MAX_THREADS` threads,
busiest first. A chat's title is shown, and the thread opens, only where
`canopy_sessions.access.visible_session_q` lets the viewer read it.

A contact canopy can name is drawn as who it is (`analytics._contacts_who_are_known`):
an agent when its address is an agent's, else a person when the contact is linked to an
account or its address is exactly one account's verified email. Writing to an agent of
a workspace you are not a member of is recorded as a contact, which is right for
access, but it is still that person or agent. The side panel is computed client-side
from the graph the page holds (`networkGraph.profile`).

## Shipping a change to the shared transcript core

`packages/canopy_transcript` (and `canopy_cron`, `runner/canopy_acp`) run on the boxes.
They must stay in the runner code-sha paths, so a change marks every box stale and the
auto-updater ships it after the next deploy: `provenance.CODE_PATHS` and the installers
(laptop runner; `tests/test_runner_code_paths.py` keeps them equal), and
`runner/ec2/update_runner.sh` (cloud runner), plus `.github/workflows/deploy-labs.yml`.

## History

`manage.py backfill_activity [--days 365] [--no-usage]`
(`apps/mcp/management/commands/backfill_activity.py`) seeds facts from turns, sessions,
setting changes and MCP calls, and usage from transcripts retention has not purged yet.
Idempotent on (kind, ref) — the same refs the live producers write — so it is safe to
run after deploy and again later.

## Health

`apps/agents/health.py` composes existing answers — never a second opinion: each
agent's steps are `setup.setup_steps`, its vault state is
`vault_health.vault_health` (also what the checklist's vault step reads).

It asks every (runner, agent) pair which machines may open each vault, so it reads
the fleet once: `device_keys.serving_facts(agents)` is passed down through
`vault_health`, `setup_steps` and `keys_for_agent`, so more runners cost no more
queries (`test_health_reads_the_fleet_once_…`); each agent still costs a fixed few
dozen queries for its own checklist steps.

Because of that per-agent cost the page never waits for every checklist: it reads
`/health?setup=false` (the table, with `steps`, `vault`, `left` and the totals
counted from them null), then `/health/setup?agents=…` (`health.setup_of`, at most
`HEALTH_SETUP_MAX` slugs, in `api_workspace_health.py`) in batches
(`pages/health/useAgentSetup.ts`), each row filling as its read lands. A plain
`/health` still answers everything in one read, for MCP callers.
