# Runners: flags, host requirements and scheduled turns

Host-required runner flags (sensitive-data runners), why a turn on a site's page withholds the agent's own login there, and scheduled turns.

**See also:** [runners.md](runners.md), [runner-routing.md](runner-routing.md), [runner-credentials.md](runner-credentials.md), [runner-fleets.md](runner-fleets.md).

## Runner flags and host requirements (sensitive-data runners)

A **sensitive-data runner** (flag `sensitive_data`) is a box set up for a host's
sensitive data. It is not "zero data retention" itself: it runs Claude only on a ZDR API
key, runs every turn under a locked harness, limits egress to an allowlist of hosts
(`runner/ec2/README.md` § Egress lock), keeps nothing after a turn, and holds only agents
that handle sensitive data. The box runs in that mode with `RUNNER_DATA_MODE=sensitive_data`
and reports it on every heartbeat; any other non-empty value takes no turn. The report
is believed only from a **cloud** runner's beat signed in with its own device key
(`RunnerAccessToken`, `runner_protocol.data_mode_trusted`); a laptop's, or any beat sent
with a person's token, is recorded as no data mode. So the declaration (a person) and the
report (the box's stack) never come from the same hand. `tests/test_data_mode_trust.py`.

Nothing of a sensitive-data box's transcripts reaches canopy-web except a confined
turn's sealed turn-end rows. Its usage report keeps no title, a session canopy has no
record of is neither kept nor listed as missing (`events.usage`), and sending one up,
asking a backfill (`transcript_read.backfill_refused`), resetting one, filing a
reported session (`session_sync.session_home`) or posting `session-backfill` is refused,
with 409 on the routes. `tests/test_sensitive_box_uploads.py`.

On the box (`runner/ec2/README.md` § Sensitive-data mode): every turn's read tools are
scoped to its own working directory, its caller envelope and its OWN sessions' tool output
(the settings are rewritten as each session starts, so a fork never reads its source's),
and every transcript, the kept-conversation records and the other per-turn state under
`~/.canopy` are denied (`sensitive_data_settings`; a confined session elsewhere gets the
same denies from `native_permissions`, and a read tool with no `read_paths` reaches only
its worktree). Every other session's directory and every other turn's caller envelope are
denied by PATTERN — every name but the turn's own (`native_permissions.complement_globs`),
never a snapshot — as are `/proc` and browser profiles.

**A caller's words never reach a confined prompt with a live `@`.** Claude Code expands
`@/path` into the file's contents before the model runs, with no tool call: only deny
rules stop it, not the allow list or `profile_guard`. Every confined turn, cloud and
laptop, turns each `@` that could start a mention into `＠` (Claude Code's own escape;
`caller.neutralise_mentions`, cloud `_neutralise_mentions`); an email address keeps its
`@`, and server-side attribution folds `＠` back (`transcript_ingest._squash`).

A sensitive conversation's kept session is forgotten when it idles, closes, the box's
ask is refused (401/403/404, a retired box) or the box shuts down — after its turns drain,
and no turn finishing then keeps one; the stop handler only sets flags (`_finish_stop`).
The sweep never deletes a record a finishing turn rewrote meanwhile. A resumed turn ships
the records after what canopy-web says it holds (the claim's `transcript_held`,
`session_sync.turn_end_held`), so a failed earlier ship is sent again — chunked, with
retries, and its live tail ticks once more as it stops.
`runner/ec2/tests/test_sensitive_data_box.py`.

- A **host** requires flags of its visitors' conversations with
  `CANOPY_HOST["RUNNER_REQUIREMENTS"] = ["sensitive_data"]` (SDK `dimagi-canopy`); the SDK signs
  them into the visitor assertion (`canopy_runner_requirements`), and canopy stamps
  them on the session's `metadata["runner_requirements"]` — server-owned,
  union-only, never lifted by a later token that omits them. A host grant carries
  its own requirements too (`runner_requirements.requirements_of_grant`).
- A **runner admin** declares a box's flags (`RunnerFlag`,
  `PUT /api/harness/runners/{id}/flags`, audited with `declared_by`). canopy cannot
  verify a flag — it records who vouched. Nothing a runner reports can set one.
- `apps/harness/runner_requirements.py` is the one reader. The claim, placement,
  the unclaimable report, turn status (`requires`) and the `site_call` gateway
  (`apps/host_grants/host_gateway.py`) all refuse a runner missing a flag — above pins,
  and with no fallback: a requirement is a floor applied to the composed list after
  strict truncation, so a turn nothing can serve says so and waits. Guarded end to
  end by `tests/test_sdk_round_trip.py` and `tests/test_runner_requirements*.py`.
  Spec: `docs/superpowers/specs/2026-09-30-zdr-runners-design.md`.

## A turn on a site's page withholds the agent's own login there

A turn whose conversation is held on a Connected site's page that its agent serves
carries `page_site: {site, withheld}` in its claim (`interface.page_turn`; the
names come from `own_login:` in the agent's interface). There the agent reaches the
site only through canopy's gateway, as the person who sent (`access-confinement-and-act-as.md` § Acting as
the person on a connected site), so:

- **Cloud runner** (`runner/ec2/cloud_runner.py`, `page_site`): the withheld names
  are dropped from the turn's environment (`_agent_env`) and its per-turn env file
  (`write_turn_env`, `CANOPY_AGENT_ENV_FILE`), and `CANOPY_PAGE_SITE=<site>` is set —
  confined turns too. The box still holds the values for the agent's other turns
  (the in-memory resolve is per agent), so the boundary is the turn's own process.
- **Laptop runner** (`runner/canopy_runner/canopy_runner/page_site.py`): emdash, not
  the runner, starts the session, so it writes `~/.canopy/page/task/<task>.json` for
  the agent's MCP headers helper (found from `EMDASH_TASK_NAME`). The agent's own
  `.env` stays on that disk: on a laptop the helper and the caller envelope's `site`
  block are the boundary, not the environment.
- Keep the site login in the agent's Canopy vault for the withholding to bind: a value
  kept anywhere else is outside it.

## Scheduled turns

The scheduler is a producer of turns, not an execution engine (no celery/beat).
`AgentSchedule` (`apps/harness/models.py`) holds the cron server-side, and
**canopy-web fires it itself**: `apps/harness/schedule_clock.py`, a job on the
off-request clock (`apps/common/ticks.py`, in the web process's ASGI lifespan), evaluates every
enabled schedule each `CANOPY_SCHEDULE_TICK_S` (60 s; a cache key keeps N web tasks
to about one tick) and calls `schedule_turns.fire_schedule` for each due slot. So a
workspace whose agents run only on cloud boxes, or whose laptop is shut, still fires
its schedules. A laptop runner still evaluates them too
(`runner/canopy_runner/canopy_runner/schedules.py`, `POST …/schedules/{id}/fire`);
both reach the same slot key, so whichever fires second collapses onto the first's
turn. Firing only queues a turn — which box runs it is decided on the claim.

A schedule is an agent **trigger** of kind `cron` (access.md § Triggers); the server
clock, the runner's sync and fire, and the week calendar read `kind=cron` only. Its
turn is accountable to whoever set it up: a slot whose creator has since lost the
right to it (`triggers.refusal`) is not fired — it is consumed (`last_slot`), logged
(`harness.triggers` / `trigger.refused`), answered 409 to a runner, and the
`trigger_stopped` alert stays open until it is fixed.

- **Anchor on `fire_after`, never `last_slot`.** `schedule_turns.fire_after_of`
  (`last_slot or created_at`, pinned just before a one-off's instant; served to
  runners as `ScheduleOut.fire_after`) is what the server clock and the runner pass
  to `due_slot(after=...)`; `last_slot` is NULL
  until the first fire, and an unbounded backward look fires a slot that predates
  the schedule.
- **Concurrent fires are safe:** the slot-derived `idempotency_key`
  (`sched:<id>:<slot>`) collapses duplicates inside `enqueue_turn`. Firing a new
  slot supersedes the schedule's still-open occurrences as MISSED — you only owe the
  newest.
- **No occurrence table:** the `Turn` is the occurrence;
  `latest_occurrence_turn` selects by `origin_ref__schedule_id`, scheduled or
  "Run now", any status.
- **One-offs** (`run_once_at`) fire only their instant and disable themselves in the
  same transaction (`OneOffSlotMismatch` otherwise).
- **Grace release:** an EXECUTING occurrence held past `grace_minutes` (anchored on
  `claimed_at` — held time, not owed time) is finished MISSED, because an abandoned
  session would occupy a runner forever (its heartbeat keeps renewing the lease). Runs on the off-request queue sweeps (every 15 s), not the fire tick
  (a weekly fire tick could never honour a short grace).
- **Late slots:** a queued slot turn not started within `LATE_SLOT_WINDOW_MINUTES`
  (30) of its slot is skipped as MISSED with no nag; `always_run` and one-offs are
  exempt, manual runs untouched.
- **Nag:** a grace-released occurrence raises a `review` ask on a task
  (`_raise_schedule_nag`, only when the schedule's `notify` includes `inbox`);
  approving re-runs the prompt, and a later DONE occurrence closes open nags
  (`resolve_schedule_nags`). It reaches **Waiting on you** as an ordinary ask.
- An occurrence runs as `system` with the schedule's creator as the accountable
  person (`turn_mode._accountable_cap`, `access.md` → "The rule").

Specs: `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md`,
`2026-07-21-supervisor-inbox-items-only-design.md`.
